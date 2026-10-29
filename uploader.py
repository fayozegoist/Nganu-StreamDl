#!/usr/bin/env python3
"""Uploader bot (Telethon MTProto, bot session) — pendamping bot Go.

POST http://127.0.0.1:8399/send
  {"chat_id": 123, "url": "...", "filename": "...", "caption": "...",
   "referer": "...", "max_bytes": 2147483648, "job_id": "abc123"}

GET  http://127.0.0.1:8399/progress?job_id=abc123
  → {"phase": "download|upload|done|error", "done": bytes, "total": bytes,
     "started": epoch}

Unduh url -> kirim ke chat user via MTProto (limit bot 2GB).
Upload file >= 8MB via FastTelethon-style parallel: UPLOAD_WORKERS sesi bot
tambahan mengirim part 512KB bersamaan — mengakali batas throughput per
koneksi pada RTT tinggi (pod EU -> DC5 Singapura).

Env: API_ID, API_HASH, BOT_TOKEN, UPLOAD_WORKERS (default 8).
"""

import asyncio
import glob
import json
import mimetypes
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests
from telethon import TelegramClient, functions, types
from telethon import utils as tg_utils

API_ID = int(os.environ.get("API_ID", "") or 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("UPLOADER_PORT", "8399"))
WORKERS = int(os.environ.get("UPLOAD_WORKERS", "8") or 8)
PIPELINE = int(os.environ.get("UPLOAD_PIPELINE", "3") or 3)
DL_WORKERS = int(os.environ.get("DL_WORKERS", "8") or 8)
PART_SIZE = 512 * 1024
PARALLEL_MIN = 8 << 20  # file lebih kecil dari ini: send_file biasa cukup
DL_CHUNK = 1024 * 1024  # granula unduhan tiap segmen

# slow-segment killer: koneksi beruntung-lotere di relay API pihak ke-3.
# Segmen lambat diputus lalu resume dengan koneksi baru = tiket lotre baru.
SLOW_MIN_BPS = 256 * 1024   # di bawah ini selama SLOW_WINDOW → putus
SLOW_WINDOW = 10.0          # detik pengamatan rolling
SEG_RETRY = 3               # sambung-ulang per segmen sebelum dibiarkan

loop = asyncio.new_event_loop()
client = TelegramClient("uploader_bot", API_ID, API_HASH, loop=loop)
workers: list[TelegramClient] = []

# ---------------- progress registry ----------------

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_cancels: dict[str, threading.Event] = {}
_cancels_lock = threading.Lock()


class JobCancelled(Exception):
    pass


def cancel_event(jid: str | None) -> threading.Event | None:
    if not jid:
        return None
    with _cancels_lock:
        return _cancels.setdefault(jid, threading.Event())


def cancel_raise(jid: str | None):
    if not jid:
        return
    ev = _cancels.get(jid)
    if ev is not None and ev.is_set():
        raise JobCancelled("dibatalkan user")


def job_set(jid: str | None, **fields):
    if not jid:
        return
    with _jobs_lock:
        j = _jobs.setdefault(jid, {
            "phase": "download", "done": 0, "total": 0, "started": time.time()})
        j.update(fields)


def job_get(jid: str) -> dict | None:
    with _jobs_lock:
        j = _jobs.get(jid)
        return dict(j) if j else None


def job_sweep():
    now = time.time()
    with _jobs_lock:
        stale = [k for k, v in _jobs.items()
                 if v.get("finished") and now - v["finished"] > 900]
        for k in stale:
            del _jobs[k]
    with _cancels_lock:
        done = [k for k in _cancels if k not in _jobs]
        for k in done:
            del _cancels[k]


# ---------------- cancel registry ----------------

class JobCancelled(Exception):
    pass


_cancels: dict[str, threading.Event] = {}
_cancels_lock = threading.Lock()


def cancel_event(jid: str | None) -> threading.Event | None:
    if not jid:
        return None
    with _cancels_lock:
        return _cancels.setdefault(jid, threading.Event())


def cancel_raise(jid: str | None):
    if not jid:
        return
    ev = _cancels.get(jid)
    if ev is not None and ev.is_set():
        raise JobCancelled("dibatalkan user")


def _dl_headers(referer: str | None, extra: dict | None = None) -> dict:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if referer:
        headers["Referer"] = referer
    if extra:
        headers.update(extra)
    return headers


def download_to_file(url: str, path: str, referer: str | None, max_bytes: int,
                     jid: str | None) -> tuple[int, str]:
    with requests.get(url, headers=_dl_headers(referer), stream=True,
                      timeout=(15, 60)) as r:
        r.raise_for_status()
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        try:
            clen = int(r.headers.get("Content-Length") or 0)
        except ValueError:
            clen = 0
        job_set(jid, total=clen)
        cancel_raise(jid)
        total = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(512 * 1024):
                if not chunk:
                    continue
                cancel_raise(jid)
                f.write(chunk)
                total += len(chunk)
                job_set(jid, done=total)
                if total > max_bytes:
                    raise ValueError(f"file melebihi batas {max_bytes / 1e9:.1f} GB")
    return total, ctype


class SlowStream(Exception):
    pass


def _dl_range(url: str, start: int, end: int, fno: int, referer: str | None,
              shared: dict, lock: threading.Lock, max_bytes: int) -> None:
    """Unduh satu segmen; resume otomatis bila koneksi lambat (tiket lotre
    buruk di relay API), macet, atau putus. Gagal → tandai untuk fallback."""
    try:
        pos, tries = start, 0
        retryable = (SlowStream, requests.exceptions.ConnectionError,
                     requests.exceptions.ChunkedEncodingError,
                     requests.exceptions.ReadTimeout)
        while pos <= end:
            prev = pos
            try:
                with requests.get(url,
                                  headers=_dl_headers(referer, {"Range": f"bytes={pos}-{end}"}),
                                  stream=True, timeout=(15, 90)) as r:
                    if r.status_code != 206:
                        raise ValueError(f"range ditolak: HTTP {r.status_code}")
                    win_start = time.monotonic()
                    win_bytes = 0
                    for chunk in r.iter_content(DL_CHUNK):
                        if not chunk:
                            continue
                        cancel_raise(shared["jid"])
                        os.pwrite(fno, chunk, pos)
                        pos += len(chunk)
                        win_bytes += len(chunk)
                        now = time.monotonic()
                        if now - win_start >= SLOW_WINDOW:
                            if win_bytes / (now - win_start) < SLOW_MIN_BPS:
                                raise SlowStream(
                                    f"tiket buruk {win_bytes // int(now - win_start)} B/s")
                            win_start, win_bytes = now, 0
                        with lock:
                            shared["done"] += len(chunk)
                            job_set(shared["jid"], done=shared["done"])
                            if shared["done"] > max_bytes:
                                raise ValueError(f"file melebihi batas {max_bytes / 1e9:.1f} GB")
            except retryable as e:
                tries += 1
                if tries > SEG_RETRY:
                    raise
                print(f"[uploader] {type(e).__name__} {str(e)[:100]} — segmen {start} "
                      f"sambung ulang dari byte {pos} ({tries})", flush=True)
                continue
            if pos == prev:  # koneksi bersih tapi nol byte — server aneh
                raise ValueError(f"segmen {start} tanpa kemajuan di byte {pos}")
    except Exception as e:
        shared["error"] = e
        raise


def download_hls(hls_url: str, path: str, referer: str | None,
                 max_bytes: int, jid: str | None) -> tuple[int, str]:
    """Rakit video HLS byte-range (freeterabox dkk): tiap segmen playlist
    adalah jendela byte (param len=a-b) dari satu mp4. Unduh paralel per
    jendela; hasil concat = file utuh, tanpa ffmpeg."""
    seg_retryable = (requests.exceptions.ConnectionError,
                     requests.exceptions.ChunkedEncodingError,
                     requests.exceptions.ReadTimeout)

    def _inner(u: str) -> str:
        q = parse_qs(urlparse(u).query)
        inner = q.get("url")
        if inner:
            return inner[0]
        return u

    def fetch_m3u8() -> str:
        last = None
        for _ in range(3):
            try:
                r = requests.get(hls_url, headers=_dl_headers(referer),
                                 timeout=(15, 30))
                r.raise_for_status()
                return r.text
            except Exception as e:
                last = e
        raise ValueError(f"playlist m3u8 gagal: {last}")

    text = fetch_m3u8()
    segs: list[tuple[str, int, int]] = []  # (inner_url, offset, end)
    cursor = 0
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(("http://", "https://")):
            continue
        inner = _inner(line)
        ln = (parse_qs(urlparse(inner).query).get("len") or [""])[0]
        if "-" in ln:
            try:
                a, b = (int(x) for x in ln.split("-", 1))
            except ValueError:
                raise ValueError(f"param len aneh: {ln}")
        elif ln.isdigit():
            a, b = cursor, cursor + int(ln) - 1
        else:
            raise ValueError("segmen tanpa param len — format tak dikenal")
        segs.append((inner, a, b))
        cursor = b + 1
    if not segs:
        raise ValueError("playlist kosong")
    total = max(b for _, _, b in segs) + 1
    if total > max_bytes:
        raise ValueError(f"file melebihi batas {max_bytes / 1e9:.1f} GB")

    def _hls_seg(u: str, a: int, b: int, fno: int, shared: dict,
                 lock: threading.Lock) -> None:
        cancel_raise(jid)
        expect = b - a + 1
        pos, got, tries = a, 0, 0
        while True:
            try:
                with requests.get(u, headers=_dl_headers(referer), stream=True,
                                  timeout=(15, 60)) as r:
                    r.raise_for_status()
                    for chunk in r.iter_content(256 * 1024):
                        if not chunk:
                            continue
                        cancel_raise(jid)
                        os.pwrite(fno, chunk, pos)
                        pos += len(chunk)
                        got += len(chunk)
                        with lock:
                            shared["done"] += len(chunk)
                            job_set(jid, done=shared["done"])
                            if shared["done"] > max_bytes:
                                raise ValueError(
                                    f"file melebihi batas {max_bytes / 1e9:.1f} GB")
                if got == expect:
                    return
                # stream habis tapi kurang — perlakukan sebagai putus
                raise requests.exceptions.ChunkedEncodingError(
                    f"jendela terpotong {got}/{expect}")
            except seg_retryable:
                # GET penuh tak bisa resume — ulang dari awal jendela
                # (byte identik menimpa offset sama; progress ditarik mundur)
                tries += 1
                if tries > SEG_RETRY:
                    raise ValueError(f"jendela {a}-{b} gagal {SEG_RETRY}x")
                with lock:
                    shared["done"] -= got
                pos, got = a, 0

    with open(path, "wb") as f:
        f.truncate(total)
    fno = os.open(path, os.O_WRONLY)
    shared = {"done": 0}
    lock = threading.Lock()
    job_set(jid, total=total)
    try:
        with ThreadPoolExecutor(max_workers=DL_WORKERS) as ex:
            futs = [ex.submit(_hls_seg, u, a, b, fno, shared, lock)
                    for u, a, b in segs]
            for fut in futs:
                fut.result()
    finally:
        os.close(fno)
    return total, "video/mp4"


def download_parallel(url: str, path: str, size: int, ctype: str,
                      referer: str | None, max_bytes: int, jid: str | None,
                      workers: int) -> tuple[int, str]:
    """Unduh paralel per byte-range (ala aria2). Naikkan bila CDN throttle per-koneksi."""
    if size > max_bytes:
        raise ValueError(f"file melebihi batas {max_bytes / 1e9:.1f} GB")
    # segmen min 4MB agar overhead request tidak dominan
    workers = max(1, min(workers, (size + (4 << 20) - 1) // (4 << 20)))
    step = (size + workers - 1) // workers
    ranges = [(i, i * step, min((i + 1) * step, size) - 1)
              for i in range(workers) if i * step < size]
    shared = {"done": 0, "jid": jid, "error": None}
    lock = threading.Lock()
    with open(path, "wb") as f:
        f.truncate(size)
    fno = os.open(path, os.O_WRONLY)
    try:
        with ThreadPoolExecutor(max_workers=len(ranges)) as ex:
            futs = [ex.submit(_dl_range, url, s, e, fno, referer, shared, lock, max_bytes)
                    for _, s, e in ranges]
            for fut in futs:
                fut.result()
    finally:
        os.close(fno)
    return size, ctype


def download_auto(url: str, path: str, referer: str | None, max_bytes: int,
                  jid: str | None) -> tuple[int, str]:
    """HEAD dulu: bila range didukung & size ada → paralel; selain itu stream tunggal.
    HEAD tanpa Accept-Ranges ≠ Range ditolak — probe aktif bytes=0-0 (CDN seperti
    aliyun/UC sering tak mengiklankan tapi menerima)."""
    try:
        h = requests.head(url, headers=_dl_headers(referer), timeout=15, allow_redirects=True)
        size = int(h.headers.get("Content-Length") or 0)
        ctype = (h.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ranges_ok = "bytes" in (h.headers.get("Accept-Ranges") or "").lower()
        if not ranges_ok and size >= PARALLEL_MIN:
            try:
                p = requests.get(url, headers=_dl_headers(
                    referer, {"Range": "bytes=0-0"}), timeout=15, stream=True)
                ranges_ok = p.status_code == 206
                p.close()
            except Exception:
                pass
    except Exception:
        size, ctype, ranges_ok = 0, "", False
    job_set(jid, total=size)
    if size > max_bytes:
        raise ValueError(f"file melebihi batas {max_bytes / 1e9:.1f} GB")
    if size >= PARALLEL_MIN and ranges_ok:
        try:
            return download_parallel(url, path, size, ctype, referer, max_bytes,
                                     jid, DL_WORKERS)
        except JobCancelled:
            raise
        except Exception as e:
            print(f"[uploader] parallel download gagal ({e}) — fallback stream", flush=True)
            job_set(jid, done=0, total=size, started=time.time())
    return download_to_file(url, path, referer, max_bytes, jid)


def sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name or "file")
    name = re.sub(r"\s+", " ", name).strip()
    return (name or "file")[:180]


def fetch_thumb(url: str) -> str | None:
    """Unduh thumbnail (maks 2MB). None bila gagal — jangan pernah gagalkan upload."""
    tmp = None
    try:
        with requests.get(url, timeout=15, stream=True) as r:
            r.raise_for_status()
            fd, tmp = tempfile.mkstemp(prefix="nganu_t_", suffix=".jpg")
            total = 0
            with os.fdopen(fd, "wb") as f:
                for chunk in r.iter_content(64 * 1024):
                    f.write(chunk)
                    total += len(chunk)
                    if total > 2 * 1024 * 1024:
                        raise ValueError("thumb kebesaran")
        return tmp
    except Exception:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return None


def ffmpeg_bin() -> str | None:
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "ffmpeg")
    return local if os.path.exists(local) else None


def ffmpeg_thumb(video_path: str) -> str | None:
    bin_ = ffmpeg_bin()
    if not bin_:
        return None
    tmp = tempfile.mktemp(prefix="nganu_t_", suffix=".jpg")
    try:
        subprocess.run(
            [bin_, "-y", "-ss", "3", "-i", video_path,
             "-frames:v", "1", "-q:v", "5", tmp],
            capture_output=True, timeout=60)
        return tmp if os.path.exists(tmp) and os.path.getsize(tmp) > 0 else None
    except Exception:
        return None


_EXT_MAP = {"video/mp4": ".mp4", "video/x-matroska": ".mkv", "video/webm": ".webm",
            "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/ogg": ".ogg",
            "image/jpeg": ".jpg", "image/png": ".png"}


def sniff_mime(path: str) -> str | None:
    """Deteksi mime dari magic bytes — fallback saat ekstensi & Content-Type bohong."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "video/mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video/x-matroska"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"ID3") or (head[0:1] == b"\xff" and len(head) > 1 and (head[1] & 0xe0) == 0xe0):
        return "audio/mpeg"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    return None


async def upload_parallel(path: str, file_size: int, fname: str,
                          jid: str | None) -> types.InputFileBig:
    """Kirim part 512KB via banyak sesi bot sekaligus (pola FastTelethon)."""
    part_count = (file_size + PART_SIZE - 1) // PART_SIZE
    if part_count > 4000:  # batas Telegram untuk saveBigFilePart
        raise ValueError("file melebihi batas paralel (1.9GB)")
    file_id = random.getrandbits(63)
    q: asyncio.Queue[int] = asyncio.Queue()
    for i in range(part_count):
        q.put_nowait(i)
    state = {"done": 0}

    async def run(w: TelegramClient):
        """Ambil batch PIPELINE part, kirim bersamaan tanpa tunggu ACK satu-satu.

        Anti botleh RTT: in-flight = workers x PIPELINE part.
        """
        with open(path, "rb") as f:
            while True:
                cancel_raise(jid)
                idxs = []
                while len(idxs) < PIPELINE:
                    try:
                        idxs.append(q.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                if not idxs:
                    return
                datas = []
                for idx in idxs:
                    f.seek(idx * PART_SIZE)
                    datas.append(f.read(PART_SIZE))
                results = await asyncio.gather(
                    *(w(functions.upload.SaveBigFilePartRequest(
                        file_id, idx, part_count, d)) for idx, d in zip(idxs, datas)),
                    return_exceptions=True)
                for idx, d, res in zip(idxs, datas, results):
                    if isinstance(res, BaseException):
                        # satu retry individual
                        await w(functions.upload.SaveBigFilePartRequest(
                            file_id, idx, part_count, d))
                    state["done"] += len(d)
                    job_set(jid, done=state["done"])

    await asyncio.gather(*(run(w) for w in workers))
    return types.InputFileBig(id=file_id, parts=part_count, name=fname)


async def _send(payload: dict) -> dict:
    url = payload.get("url") or ""
    hls_url = (payload.get("hls_url") or "").strip()
    chat_id = int(payload["chat_id"])
    jid = payload.get("job_id") or None
    caption = payload.get("caption") or ""
    max_bytes = int(payload.get("max_bytes") or 2 << 30)
    referer = payload.get("referer") or None
    thumb_url = payload.get("thumb_url") or None
    fname = sanitize(payload.get("filename") or "file")

    fd, tmp = tempfile.mkstemp(prefix="nganu_")
    os.close(fd)
    thumb = None
    tmp2 = None
    try:
        job_set(jid, phase="download", done=0, total=0, started=time.time())
        t0 = time.time()
        if hls_url:  # HLS byte-range: rakit jendela segmen jadi file utuh
            total, ctype = await loop.run_in_executor(
                None, download_hls, hls_url, tmp, referer, max_bytes, jid)
        else:
            total, ctype = await loop.run_in_executor(
                None, download_auto, url, tmp, referer, max_bytes, jid)
        t_dl = time.time() - t0

        sniff = await loop.run_in_executor(None, sniff_mime, tmp)
        mime = (mimetypes.guess_type(fname)[0]
                or sniff
                or (ctype if ctype != "application/octet-stream" else None)
                or sniff
                or "application/octet-stream")
        if not os.path.splitext(fname)[1] or mimetypes.guess_type(fname)[0] is None:
            ext = _EXT_MAP.get(mime) or mimetypes.guess_extension(mime) or ""
            if ext and not fname.lower().endswith(ext.lower()):
                fname += ext

        if thumb_url:
            thumb = await loop.run_in_executor(None, fetch_thumb, thumb_url)
        if thumb is None and mime.startswith("video/"):
            thumb = await loop.run_in_executor(None, ffmpeg_thumb, tmp)

        # hardlink dengan ekstensi — is_video/is_audio Telethon cek ekstensi path
        suff = os.path.splitext(fname)[1]
        tmp2 = tmp + (suff or ".bin")
        os.link(tmp, tmp2)
        attrs, _mm = tg_utils.get_attributes(
            tmp2, supports_streaming=mime.startswith(("video/", "audio/")))
        streamable = mime.startswith(("video", "audio"))

        job_set(jid, phase="upload", done=0, total=total, started=time.time())
        t1 = time.time()
        kwargs = dict(caption=caption, file_name=fname, attributes=attrs,
                      supports_streaming=streamable, mime_type=mime)
        if thumb:
            kwargs["thumb"] = thumb
        part_count = (total + PART_SIZE - 1) // PART_SIZE
        if total >= PARALLEL_MIN and part_count > 4000:
            raise ValueError("file melebihi batas bot Telegram (1.9 GB) — "
                             "unduh manual lewat link")
        cancel_event(jid)  # siapkan sejak awal; /stop bisa datang kapan saja
        cancel_raise(jid)
        if total >= PARALLEL_MIN and workers:
            inpf = await upload_parallel(tmp2, total, fname, jid)
            msg = await client.send_file(chat_id, inpf, **kwargs)
        else:
            def cb(cur, tot):
                job_set(jid, done=cur)
                cancel_raise(jid)
            msg = await client.send_file(chat_id, tmp2, part_size_kb=512,
                                         progress_callback=cb, **kwargs)
        t_up = time.time() - t1
        mbs = total / (1024 * 1024)
        print(f"[uploader] {mbs:.1f}MB download={t_dl:.1f}s ({mbs/max(t_dl,0.1):.1f}MB/s) "
              f"upload={t_up:.1f}s ({mbs/max(t_up,0.1):.1f}MB/s)", flush=True)
        job_set(jid, phase="done", done=total, finished=time.time())
        return {"ok": True, "message_id": msg.id,
                "t_dl": round(t_dl, 1), "t_up": round(t_up, 1), "size": total}
    except Exception as e:
        job_set(jid, phase="error", error=str(e)[:200], finished=time.time())
        raise
    finally:
        for p in (tmp, tmp2, thumb):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, data: bytes, ctype="text/plain"):
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # pemanggil sudah timeout/putus — abaikan

    def do_GET(self):
        if self.path.startswith("/cancel"):
            jid = (parse_qs(urlparse(self.path).query).get("job_id") or [""])[0]
            ev = cancel_event(jid)
            if jid and ev is not None:
                ev.set()
            self._reply(b'{"ok":true}', "application/json")
            return
        if self.path.startswith("/progress"):
            job_sweep()
            jid = (parse_qs(urlparse(self.path).query).get("job_id") or [""])[0]
            self._reply(json.dumps(job_get(jid) or {}).encode(),
                        "application/json")
            return
        self._reply(b"ok")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path != "/send" or not body.get("chat_id") or \
                    not (body.get("url") or body.get("hls_url")):
                raise ValueError("payload tidak valid")

            fut = asyncio.run_coroutine_threadsafe(_send(body), loop)
            result = fut.result(timeout=1800)
        except Exception as e:
            if "dibatalkan" in str(e):
                print(f"[uploader] job dibatalkan: {e}", flush=True)
            else:
                traceback.print_exc()
            result = {"ok": False, "error": str(e)[:300]}

        self._reply(json.dumps(result).encode(), "application/json")


async def amain():
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    print(f"[uploader] bot aktif: @{me.username} (MTProto, limit 2GB)", flush=True)
    for i in range(WORKERS):
        try:
            w = TelegramClient(f"up_worker_{i}", API_ID, API_HASH, loop=loop)
            await w.start(bot_token=BOT_TOKEN)
            workers.append(w)
        except Exception as e:
            print(f"[uploader] worker {i} gagal: {e}", flush=True)
    print(f"[uploader] parallel workers: {len(workers)}/{WORKERS}", flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[uploader] HTTP :{PORT}/send", flush=True)
    await loop.run_in_executor(None, server.serve_forever)


if __name__ == "__main__":
    if not (API_ID and API_HASH and BOT_TOKEN):
        print("[uploader] API_ID/API_HASH/BOT_TOKEN belum diisi. Keluar.", file=sys.stderr)
        sys.exit(1)
    # sweep tempfile korban restart mendadak (SIGKILL melewati finally)
    swept = 0
    for f in glob.glob(tempfile.gettempdir() + "/nganu_*"):
        try:
            if time.time() - os.path.getmtime(f) > 1800:
                os.remove(f)
                swept += 1
        except OSError:
            pass
    if swept:
        print(f"[uploader] sweep {swept} tempfile usang", flush=True)
    loop.run_until_complete(amain())
