#!/usr/bin/env python3
"""TeraBox Resolver API — key-protected JSON API + signed stream/download links.

Tidak mengekspos CDN upstream (getindevice.com / v4.freeterabox.com) ke klien.
Semua URL stream/download memakai HMAC sig+exp (8 jam) tanpa membocorkan key.
"""

import html
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, Response, jsonify, request, send_file

import db
import security
import subsrt
import terabox as tb
import official

app = Flask(__name__)

CHUNK = 256 * 1024
UA = tb.UA
ALLOWED_CDN_HOSTS = ("dl.getindevice.com", "v4.freeterabox.com")

TEMP_DIR = os.environ.get("TELETHON_TEMP_DIR", "/app/data/uploads")
DL_WORKERS = int(os.environ.get("DL_WORKERS", "12"))
TEMP_MAX_AGE = 30 * 60

JOIN_MSG = "Join Channel: https://t.me/MediafairyCH."
API_VERSION = "1.0"

_RESOLVE_CACHE: dict = {}
_RESOLVE_CACHE_TTL = 10 * 60
_RESOLVE_LOCK = threading.Lock()

_SINGLE_CACHE: dict = {}
_SINGLE_CACHE_TTL = 10 * 60
_SINGLE_LOCK = threading.Lock()


def _resolve_cached(url: str) -> dict:
    now = time.time()
    hit = _RESOLVE_CACHE.get(url)
    if hit and now - hit[0] < _RESOLVE_CACHE_TTL:
        return hit[1]
    data = _resolve_with_retry(url)
    with _RESOLVE_LOCK:
        _RESOLVE_CACHE[url] = (time.time(), data)
    return data


_STARTED = threading.Event()


def _start_janitor() -> None:
    if _STARTED.is_set():
        return
    _STARTED.set()

    def run():
        while True:
            time.sleep(900)
            try:
                now = time.time()
                if os.path.isdir(TEMP_DIR):
                    for entry in os.listdir(TEMP_DIR):
                        p = os.path.join(TEMP_DIR, entry)
                        try:
                            if now - os.stat(p).st_mtime > TEMP_MAX_AGE:
                                if os.path.isdir(p):
                                    shutil.rmtree(p, ignore_errors=True)
                                else:
                                    os.remove(p)
                        except OSError:
                            pass
            except Exception:
                pass

    threading.Thread(target=run, daemon=True).start()


def _collect_items(data: dict) -> list[dict]:
    items = []
    for kind in ("videos", "photos", "audios"):
        for item in data.get(kind, []):
            item["kind"] = kind.rstrip("s")
            items.append(item)
    items.sort(key=lambda x: x.get("order", 0))
    for idx, item in enumerate(items, 1):
        item["index"] = idx
    return items


def _resolve_with_retry(url: str, attempts: int = 3) -> dict:
    last = None
    for attempt in range(1, attempts + 1):
        try:
            token = tb.get_token()
            return tb.resolve(url, token)
        except Exception as e:
            last = e
            if attempt == attempts:
                break
            time.sleep(BACKOFF[min(attempt - 1, len(BACKOFF) - 1)])
    raise RuntimeError(f"API getindevice gagal setelah {attempts}x: {last}")


def resolve_terabox(url: str) -> dict:
    data = _resolve_cached(url)
    items = _collect_items(data)

    if not items:
        raise ValueError("Link tidak mengandung file apa pun.")

    files = []
    for item in items:
        entry = {
            "index": item["index"],
            "kind": item["kind"],
            "quality": item.get("quality", "N/A"),
            "format": item.get("format", "bin"),
            "name": None,
            "full_cdn_url": None,
            "m3u8": None,
            "fid": item.get("fid", item.get("id")),
        }
        if item.get("is_hls") and item.get("hlsStreamUrl"):
            hls = tb.fix_hls_url(item["hlsStreamUrl"])
            entry["m3u8"] = hls
            try:
                fid = urllib.parse.parse_qs(urllib.parse.urlparse(hls).query).get("fid")
                if fid:
                    entry["fid"] = fid[0]
            except Exception:
                pass
            try:
                m3u8_text = tb._fetch_text(hls)
                segs = tb.parse_m3u8_segments(m3u8_text)
                if segs:
                    entry["segments"] = segs
                    entry["full_cdn_url"] = tb.build_full_cdn_url(segs[0])
                    fn = tb.filename_from_segment(segs[0])
                    if fn:
                        entry["name"] = tb.sanitize_filename(fn)
            except Exception:
                pass
        elif item.get("url"):
            entry["direct_url"] = item["url"]
        files.append(entry)

    return {
        "title": data.get("title", "Tanpa judul"),
        "source": data.get("source", "unknown"),
        "url": url,
        "file_count": len(files),
        "files": files,
    }


def resolve_single(url: str, n: int) -> dict:
    """Resolve + detail file ke-n (cache 10 mnt)."""
    key = (url, n)
    now = time.time()
    hit = _SINGLE_CACHE.get(key)
    if hit and now - hit[0] < _SINGLE_CACHE_TTL:
        return hit[1]
    data = _resolve_cached(url)
    item = next((i for i in _collect_items(data) if i["index"] == n), None)
    if item is None:
        raise ValueError(f"File #{n} tidak ditemukan di link tersebut.")

    out = {
        "index": item["index"],
        "kind": item["kind"],
        "quality": item.get("quality", "N/A"),
        "format": item.get("format", "mp4"),
        "name": None,
        "full_cdn_url": None,
        "m3u8": None,
        "m3u8_text": None,
        "segments": None,
        "direct_url": None,
    }

    if item.get("is_hls") and item.get("hlsStreamUrl"):
        hls = tb.fix_hls_url(item["hlsStreamUrl"])
        out["m3u8"] = hls
        m3u8_text = tb._fetch_text(hls)
        out["m3u8_text"] = m3u8_text
        segs = tb.parse_m3u8_segments(m3u8_text)
        out["segments"] = segs
        if segs:
            out["full_cdn_url"] = tb.build_full_cdn_url(segs[0])
            fn = tb.filename_from_segment(segs[0])
            if fn:
                out["name"] = tb.sanitize_filename(fn)
    elif item.get("url"):
        out["direct_url"] = item["url"]

    if not out["name"]:
        out["name"] = f"TeraBox_{n}.{out['format']}"
    with _SINGLE_LOCK:
        _SINGLE_CACHE[key] = (time.time(), out)
    return out


def _rfc5987(name: str) -> str:
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", name).replace('"', "'").strip() or "file"
    quoted_utf8 = urllib.parse.quote(name, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted_utf8}"


def _spawn_ffmpeg() -> subprocess.Popen:
    return subprocess.Popen(
        [
            "ffmpeg", "-v", "error", "-i", "pipe:0", "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-movflags", "+frag_keyframe+empty_moov", "-f", "mp4", "pipe:1",
        ],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


BACKOFF = [3, 8, 15, 25, 40]
SEG_RETRY = 3


def _feed_segments(segments: list[str], proc: subprocess.Popen, stop_evt: threading.Event) -> None:
    prefetch = 2

    def fetch_stream(i: int):
        raw = segments[i]
        inner = tb.inner_segment_url(raw)
        cands = [(inner, tb.TERABOX_HEADERS)] if (inner and inner != raw) else []
        cands.append((raw, tb.NO_REFERER))
        last = None
        for cand_url, headers in cands:
            try:
                resp = tb._SESSION.get(cand_url, headers=headers, timeout=45, stream=True)
                resp.raise_for_status()
                return resp
            except Exception as e:
                last = e
        raise last

    n = len(segments)
    idx = 0
    inflight: dict[int, tuple] = {}
    with ThreadPoolExecutor(max_workers=prefetch + 1) as ex:
        while idx < n:
            if stop_evt.is_set() or proc.poll() is not None:
                return
            for j in range(idx, min(n, idx + 1 + prefetch)):
                if j not in inflight:
                    inflight[j] = ex.submit(fetch_stream, j), None
            fut, _buf = inflight[idx]
            try:
                resp = fut.result(timeout=60)
            except Exception:
                return
            try:
                for c in resp.iter_content(CHUNK):
                    if not c:
                        continue
                    if stop_evt.is_set() or proc.poll() is not None:
                        resp.close()
                        return
                    try:
                        proc.stdin.write(c)
                    except Exception:
                        resp.close()
                        return
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            inflight.pop(idx, None)
            idx += 1
    try:
        proc.stdin.close()
    except Exception:
        pass


def _stream_ffmpeg_mp4(segments: list[str]) -> Response:
    attempts = len(BACKOFF) + 1
    first_chunk = None
    proc = None
    feeder = None

    for attempt in range(1, attempts + 1):
        stop_evt = threading.Event()
        p = _spawn_ffmpeg()
        f = threading.Thread(target=_feed_segments, args=(segments, p, stop_evt), daemon=True)
        f.start()

        first = p.stdout.read(CHUNK)
        if first:
            proc, feeder = p, f
            first_chunk = first
            break

        stop_evt.set()
        f.join(timeout=5)
        try:
            p.kill()
        except Exception:
            pass
        rc = p.wait(timeout=10)

        if attempt == attempts:
            raise RuntimeError(f"CDN/ffmpeg gagal setelah {attempts}x (rc={rc})")
        time.sleep(BACKOFF[min(attempt - 1, len(BACKOFF) - 1)])

    def generate():
        yield first_chunk
        try:
            for chunk in iter(lambda: proc.stdout.read(CHUNK), b""):
                yield chunk
        finally:
            stop_evt.set()
            feeder.join(timeout=10)
            proc.stdout.close()
            try:
                proc.stderr.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    return Response(generate(), content_type="video/mp4")


def is_valid_terabox(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url.strip())
        host = (parsed.hostname or "").lower().replace("www.", "")
        return any(host == d or host.endswith(f".{d}") for d in tb.VALID_DOMAINS)
    except Exception:
        return False


def _client_ip() -> str:
    return request.headers.get("CF-Connecting-IP") or request.remote_addr or "0.0.0.0"


# ---------- helpers JSON ----------

def _json(data: dict, code: int = 200):
    text = json.dumps(data, indent=2, ensure_ascii=False)
    return app.response_class(text, status=code, mimetype="application/json")


def _err(code: int, message: str):
    body = {"status": "error", "code": code, "message": message,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    return _json(body, code)


def _base_url() -> str:
    """Base URL publik dari request ini (relative path disusun di sini)."""
    host = request.headers.get("X-Forwarded-Host") or request.host
    scheme = request.headers.get("X-Forwarded-Proto") or request.scheme
    return f"{scheme}://{host}"


def _sign_url(base: str, path: str, url: str, n: int) -> str:
    exp = security.exp_later()
    qurl = urllib.parse.quote(url, safe="")
    sig = security.sign(url, n, exp)
    return f"{base}{path}?url={qurl}&n={n}&exp={exp}&sig={sig}"


def _file_payload(url: str, n: int, base: str, official_files: list | None = None,
                  kind_ordinal: int = 1) -> dict:
    info = resolve_single(url, n)
    kind = info.get("kind", "file")
    name = info.get("name") or f"TeraBox_{n}.{info.get('format', 'mp4')}"
    size = info.get("quality", "N/A")
    size_bytes = 0
    try:
        m = re.match(r"([\d.]+)\s*([A-Z]+)", size)
        if m:
            mult = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}
            size_bytes = int(float(m.group(1)) * mult.get(m.group(2).upper(), 1))
    except Exception:
        pass
    segs = info.get("segments") or []
    seg_count = len(segs)
    total_bytes = tb.total_size_from_segments(segs) or size_bytes
    payload = {
        "index": n,
        "name": name,
        "type": kind,
        "format": info.get("format", "mp4"),
        "quality": info.get("quality", "N/A"),
        "size_bytes": total_bytes,
        "size_label": _fmt_bytes(total_bytes) if total_bytes else size,
        "hls": bool(segs),
        "segments_count": seg_count,
        "stream": _sign_url(base, "/play", url, n) if kind in ("video", "audio") else None,
        "download": _sign_url(base, "/dl", url, n),
        "thumb": None,
    }
    if official_files:
        m = official.match(official_files, info.get("format"), kind_ordinal, total_bytes)
        if m:
            payload["thumb"] = m.get("thumb")
            payload["name"] = name if (name and not name.startswith("TeraBox_")) else m.get("name") or name
    return payload


def _fmt_bytes(b) -> str:
    b = float(b or 0)
    if not b:
        return "0 Bytes"
    k = 1024
    sizes = ["Bytes", "KB", "MB", "GB", "TB"]
    i = 0
    while b >= k and i < len(sizes) - 1:
        b /= k
        i += 1
    return f"{b:.2f} {sizes[i]}"


def _parse_size_label(label: str) -> int:
    m = re.match(r"([\d.]+)\s*([A-Z]+)", (label or "").strip(), re.IGNORECASE)
    if not m:
        return 0
    mult = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
    try:
        return int(float(m.group(1)) * mult.get(m.group(2).upper(), 1))
    except (ValueError, KeyError):
        return 0


def _kind_ordinal_map(data: dict) -> dict:
    """index -> urutan (1-based) di antara item dengan kind yang sama."""
    counts: dict = {}
    ordinal: dict = {}
    for f in data["files"]:
        k = f["kind"]
        counts[k] = counts.get(k, 0) + 1
        ordinal[f["index"]] = counts[k]
    return ordinal


# ---------- routes ----------

@app.get("/")
def index():
    return _json({"message": JOIN_MSG})


@app.get("/health")
def health():
    return _json({"status": "ok"})


@app.errorhandler(404)
def not_found(e):
    return _err(404, "Endpoint tidak ditemukan")


@app.get("/api/terabox")
def api_terabox():
    key = security.extract_key()
    if not security.has_keys() or not security.key_is_valid(key):
        return _err(401, "Invalid or missing API key")

    rl = security.rate_limit(_client_ip())
    if rl:
        return _err(429, f"Rate limit exceeded, coba lagi dalam {rl} detik")

    url = (request.args.get("url") or "").strip()
    if not url:
        return _err(400, "Parameter 'url' wajib diisi.")
    if not is_valid_terabox(url):
        return _err(400, "URL bukan TeraBox valid. Domain: " + ", ".join(tb.VALID_DOMAINS))

    try:
        n_raw = request.args.get("n")
        n = int(n_raw) if n_raw else None
    except (TypeError, ValueError):
        return _err(400, "Parameter 'n' harus bilangan bulat.")

    t0 = time.time()
    try:
        data = resolve_terabox(url)
    except Exception as e:
        return _err(502, str(e)[:300])

    total = data["file_count"]
    base = _base_url()

    # file tanpa sumber (foto/file tanpa url & tanpa HLS) -> data resmi utk thumb+fallback
    need_official = any(
        not f.get("segments") and not f.get("direct_url") for f in data["files"]
    )
    official_files = None
    if need_official and official.available():
        try:
            official_files = official.list_files(url)
        except Exception:
            official_files = None
    ordinal_map = _kind_ordinal_map(data)

    files = []
    try:
        if n is not None:
            if n < 1 or n > total:
                return _err(400, f"File #{n} tidak ditemukan (total {total} file).")
            files = [_file_payload(url, n, base, official_files, ordinal_map.get(n, 1))]
        else:
            for i in range(1, total + 1):
                files.append(_file_payload(url, i, base, official_files, ordinal_map.get(i, 1)))
    except Exception as e:
        return _err(502, str(e)[:300])

    resp = {
        "status": "success",
        "message": JOIN_MSG,
        "response_time_ms": int((time.time() - t0) * 1000),
        "title": data.get("title"),
        "source": data.get("source"),
        "url": url,
        "expires_in": f"{security.LINK_TTL // 3600}h",
        "total_files": total,
        "files": files,
    }
    return _json(resp)


# ---------- stream / download (autentikasi sig+exp atau key) ----------

def _check_stream_auth() -> bool:
    """Allow jika key valid ATAU sig+exp cocok dengan (url,n)."""
    if security.has_keys() and security.key_is_valid(security.extract_key()):
        return True
    url = (request.args.get("url") or "")
    n_raw = request.args.get("n", "1")
    try:
        n = int(n_raw)
    except (TypeError, ValueError):
        n = 1
    exp_raw = request.args.get("exp")
    sig = request.args.get("sig")
    if not url or not exp_raw or not sig:
        return False
    try:
        exp = int(exp_raw)
    except (TypeError, ValueError):
        return False
    return security.verify(url, n, exp, sig)


def parse_url_n() -> tuple[str, int]:
    url = (request.args.get("url") or "").strip()
    try:
        n = int(request.args.get("n", "1"))
    except (TypeError, ValueError):
        n = 1
    if not is_valid_terabox(url):
        raise ValueError("URL bukan TeraBox valid.")
    if n < 1:
        raise ValueError("Parameter 'n' harus >= 1.")
    return url, n


def _schedule_cleanup(resp: Response, tmpdir: str) -> Response:
    close = getattr(resp, "call_on_close", None)
    if close:
        close(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
    return resp


def _official_match_for(url: str, n: int, info: dict) -> dict | None:
    """Cocokkan item getindevice (tanpa sumber) ke file resmi by format+ordinal+size."""
    if not official.available():
        return None
    of = official.list_files(url)
    if not of:
        return None
    try:
        data = resolve_terabox(url)
        ordinal = _kind_ordinal_map(data).get(n, 1)
    except Exception:
        ordinal = 1
    size_hint = _parse_size_label(info.get("quality", ""))
    return official.match(of, info.get("format"), ordinal, size_hint)


def _download_response(url: str, n: int) -> Response:
    info = resolve_single(url, n)

    fmt = (info.get("format") or "").lower()
    name = info.get("name") or (f"TeraBox_{n}.{fmt}" if fmt else f"TeraBox_{n}")
    segments = info.get("segments") or []
    if not segments and info.get("full_cdn_url"):
        segments = [info["full_cdn_url"]]
    if segments and not name.lower().endswith(".mp4"):
        name += ".mp4"

    if not segments and info.get("direct_url"):
        upstream = None
        attempts = len(BACKOFF) + 1
        for attempt in range(1, attempts + 1):
            try:
                req = urllib.request.Request(info["direct_url"], headers=tb.NO_REFERER)
                upstream = urllib.request.urlopen(req, timeout=60)
                break
            except Exception as e:
                if attempt == attempts:
                    raise RuntimeError(f"CDN menolak setelah {attempts}x: {e}")
                time.sleep(BACKOFF[min(attempt - 1, len(BACKOFF) - 1)])
        assert upstream is not None

        def gen():
            try:
                for chunk in iter(lambda: upstream.read(CHUNK), b""):
                    yield chunk
            finally:
                try:
                    upstream.close()
                except Exception:
                    pass

        resp = Response(gen(), mimetype=mime_for(name))
        resp.headers["Content-Disposition"] = _rfc5987(name)
        resp.headers["Accept-Ranges"] = "bytes"
        return resp

    if not segments:
        # Fallback resmi: hanya untuk file yang getindevice tidak sediakan (foto/dll).
        m = _official_match_for(url, n, info)
        if m and m.get("dlink"):
            upstream = None
            attempts = len(BACKOFF) + 1
            for attempt in range(1, attempts + 1):
                try:
                    req = urllib.request.Request(m["dlink"], headers=official.stream_headers())
                    upstream = urllib.request.urlopen(req, timeout=120)
                    break
                except Exception as e:
                    if attempt == attempts:
                        raise RuntimeError(f"CDN resmi menolak setelah {attempts}x: {e}")
                    time.sleep(BACKOFF[min(attempt - 1, len(BACKOFF) - 1)])
            real_name = m.get("name") or name

            def gen():
                try:
                    for chunk in iter(lambda: upstream.read(CHUNK), b""):
                        yield chunk
                finally:
                    try:
                        upstream.close()
                    except Exception:
                        pass

            resp = Response(gen(), mimetype=mime_for(real_name))
            resp.headers["Content-Disposition"] = _rfc5987(real_name)
            resp.headers["Accept-Ranges"] = "bytes"
            return resp
        raise ValueError("File ini tidak disediakan oleh sumber API (tidak ada dlink).")

    try:
        resp = _stream_ffmpeg_mp4(segments)
        resp.headers["Content-Disposition"] = _rfc5987(name)
        resp.headers["Accept-Ranges"] = "bytes"
        return resp
    except Exception:
        print("[DL] stream langsung gagal, fallback paralel:", file=sys.stderr)
        traceback.print_exc()

    _start_janitor()
    os.makedirs(TEMP_DIR, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="dl_", dir=TEMP_DIR)
    safe = tb.sanitize_filename(name) or f"TeraBox_{n}.mp4"
    mp4_path = os.path.join(tmpdir, safe)

    try:
        try:
            ts_size = tb.fetch_ts_size(segments[0])
        except Exception:
            ts_size = None
        ok = tb.download_and_remux_parallel(
            segments, tmpdir, mp4_path, ts_size=ts_size, workers=DL_WORKERS,
        )
        if ok and os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0:
            resp = send_file(
                mp4_path,
                as_attachment=True,
                download_name=name,
                mimetype="video/mp4",
                conditional=True,
            )
            resp.headers["Cache-Control"] = "no-store"
            return _schedule_cleanup(resp, tmpdir)
        print(f"[DL] paralel gagal ok={ok} exists={os.path.exists(mp4_path)} size={os.path.getsize(mp4_path) if os.path.exists(mp4_path) else 0}", file=sys.stderr)
    except Exception:
        print("[DL] paralel exception:", file=sys.stderr)
        traceback.print_exc()

    raise RuntimeError("Gagal menghasilkan file unduhan (stream & paralel).")


@app.get("/api/download")
def api_download():
    if not _check_stream_auth():
        return _err(401, "Invalid or missing API key / signature")
    try:
        url, n = parse_url_n()
        resp = _download_response(url, n)
    except ValueError as e:
        return _err(400, str(e))
    except Exception as e:
        return _err(502, str(e))
    return resp


@app.get("/dl")
def dl_short():
    if not _check_stream_auth():
        return _err(401, "Invalid or missing API key / signature")
    try:
        url, n = parse_url_n()
        resp = _download_response(url, n)
    except ValueError as e:
        return _err(400, str(e))
    except Exception as e:
        return _err(502, str(e))
    return resp


@app.get("/play")
def play_page():
    if not _check_stream_auth():
        return _err(401, "Invalid or missing API key / signature")
    try:
        url, n = parse_url_n()
        info = resolve_single(url, n)
    except ValueError as e:
        return _err(400, str(e))
    except Exception as e:
        return _err(502, str(e))

    name = info.get("name") or f"TeraBox_{n}.mp4"
    kind = info.get("kind", "file")
    segments = info.get("segments") or []
    direct = info.get("direct_url")

    # Foto: tampilkan gambar langsung (inline) — via direct_url atau fallback resmi.
    if kind == "photo":
        src = direct
        if not src:
            m = _official_match_for(url, n, info)
            src = (m or {}).get("dlink")
            hdrs = official.stream_headers() if src else tb.TERABOX_HEADERS
        else:
            hdrs = tb.TERABOX_HEADERS
        if not src:
            return _err(404, "Gambar tidak tersedia dari sumber API.")
        upstream = None
        attempts = len(BACKOFF) + 1
        for attempt in range(1, attempts + 1):
            try:
                upstream = urllib.request.urlopen(
                    urllib.request.Request(src, headers=hdrs), timeout=60)
                break
            except Exception as e:
                if attempt == attempts:
                    return _err(502, f"CDN menolak ({e}).")
                time.sleep(BACKOFF[min(attempt - 1, len(BACKOFF) - 1)])

        def gen():
            try:
                for chunk in iter(lambda: upstream.read(CHUNK), b""):
                    yield chunk
            finally:
                try:
                    upstream.close()
                except Exception:
                    pass

        resp = Response(gen(), mimetype=mime_for(name))
        resp.headers["Content-Disposition"] = "inline"
        return resp

    # Stream media langsung tanpa UI: kalau ada segmen -> halaman HTML minimal berisi player
    # yang memuat jalur /stream/manifest (semua tetap di domain kita, tanpa CDN tercantum).
    if segments:
        qurl = urllib.parse.quote(url, safe="")
        exp = security.exp_later()
        sig = security.sign(url, n, exp)
        manifest = f"/stream/manifest?url={qurl}&n={n}&exp={exp}&sig={sig}"
        tag = "video" if kind == "video" else "audio"
        html = (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>Stream</title>"
            "<script src=\"https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js\"></script>"
            "<style>"
            "html,body{margin:0;padding:0;height:100%;background:#000}"
            "body{display:flex;align-items:center;justify-content:center;overflow:hidden}"
            f"{tag}{{width:min(640px,92vw);max-height:70vh}}"
            "</style>"
            "</head><body>"
            f"<{tag} id=\"m\" controls autoplay muted playsinline style=\"width:min(640px,92vw);max-height:70vh\"></{tag}>"
            "<script>const src=" + json.dumps(manifest) + ";"
            "const m=document.getElementById('m');"
            "function start(){" +
            ("" if kind == "video" else "")
            + "if(m.canPlayType('application/vnd.apple.mpegurl')&&!window.Hls){m.src=src;}"
            + "else if(window.Hls&&window.Hls.isSupported()){const h=new Hls({maxBufferLength:40});h.loadSource(src);h.attachMedia(m);}"
            + "else{m.src=src;}"
            + "m.play&&m.play().catch(()=>{});}"
            + "if(document.readyState==='complete'){start();}else{window.addEventListener('load',start);}"
            + "document.addEventListener('click',()=>{if(m.muted){m.muted=false;}});"
            + "</script>"
            "</body></html>"
        )
        resp = Response(html, mimetype="text/html")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    if direct:
        attempts = len(BACKOFF) + 1
        req = None
        for attempt in range(1, attempts + 1):
            try:
                req = urllib.request.Request(direct, headers=tb.TERABOX_HEADERS)
                upstream = urllib.request.urlopen(req, timeout=60)
                break
            except Exception as e:
                if attempt == attempts:
                    return _err(502, f"CDN menolak ({e}).")
                time.sleep(BACKOFF[min(attempt - 1, len(BACKOFF) - 1)])
        if req is None:
            return _err(502, "CDN menolak.")

        def gen():
            try:
                for chunk in iter(lambda: upstream.read(CHUNK), b""):
                    yield chunk
            finally:
                try:
                    upstream.close()
                except Exception:
                    pass

        resp = Response(gen(), mimetype=mime_for(name))
        resp.headers["Content-Disposition"] = "inline"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    return _err(400, "File ini tidak mendukung streaming.")


def mime_for(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    table = {
        "mp4": "video/mp4", "m4v": "video/mp4", "mkv": "video/x-matroska",
        "webm": "video/webm", "mov": "video/quicktime", "avi": "video/x-msvideo",
        "ts": "video/mp2t", "3gp": "video/3gpp", "flv": "video/x-flv",
        "mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac",
        "flac": "audio/flac", "wav": "audio/wav", "ogg": "audio/ogg",
        "opus": "audio/opus", "wma": "audio/x-ms-wma",
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
        "heic": "image/heic", "svg": "image/svg+xml",
        "pdf": "application/pdf", "zip": "application/zip",
        "rar": "application/vnd.rar", "7z": "application/x-7z-compressed",
        "apk": "application/vnd.android.package-archive",
        "txt": "text/plain", "srt": "application/x-subrip",
    }
    return table.get(ext, "application/octet-stream")


@app.get("/stream/manifest")
def stream_manifest():
    if not _check_stream_auth():
        return _err(401, "Invalid or missing API key / signature")
    try:
        url, n = parse_url_n()
        info = resolve_single(url, n)
        m3u8_text = info.get("m3u8_text")
        if not m3u8_text:
            return _err(400, "File ini tidak mendukung streaming HLS.")
    except Exception as e:
        return _err(400, str(e))

    exp = security.exp_later()
    sig = security.sign(url, n, exp)
    lines = []
    for line in m3u8_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("http://") or line.startswith("https://"):
            u = urllib.parse.quote(line, safe="")
            seg_sig = security.sign_value(line, exp)
            lines.append(f"/stream/seg?u={u}&exp={exp}&sig={seg_sig}")
        else:
            lines.append(line)

    resp = Response("\n".join(lines) + "\n", content_type="application/vnd.apple.mpegurl")
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/stream/seg")
def stream_seg():
    u = (request.args.get("u") or "").strip()
    if not u:
        return _err(400, "Parameter 'u' wajib diisi.")
    if security.has_keys() and security.key_is_valid(security.extract_key()):
        pass
    else:
        exp_raw = request.args.get("exp")
        sig = request.args.get("sig")
        if not exp_raw or not sig:
            return _err(401, "Invalid or missing API key / signature")
        try:
            exp = int(exp_raw)
        except (TypeError, ValueError):
            return _err(401, "Invalid or missing API key / signature")
        if not security.verify_value(u, exp, sig):
            return _err(401, "Invalid or missing API key / signature")

    try:
        host = (urllib.parse.urlparse(u).hostname or "").lower()
    except Exception:
        host = ""
    if host not in ALLOWED_CDN_HOSTS:
        return _err(403, "Host tidak diizinkan.")

    candidates = []
    inner = tb.inner_segment_url(u)
    if inner and inner != u and urllib.parse.urlparse(inner).hostname in ALLOWED_CDN_HOSTS:
        candidates = [(inner, tb.TERABOX_HEADERS), (u, tb.NO_REFERER)]
    elif "freeterabox.com" in host:
        candidates = [(u, tb.TERABOX_HEADERS)]
    else:
        candidates = [(u, tb.NO_REFERER)]

    upstream = None
    last_err = None
    for cand_url, cand_headers in candidates:
        try:
            req = urllib.request.Request(cand_url, headers=cand_headers)
            upstream = urllib.request.urlopen(req, timeout=60)
            break
        except urllib.error.HTTPError as e:
            last_err = f"CDN menolak ({e.code})."
        except Exception as e:
            last_err = str(e)
    if upstream is None:
        return _err(502, last_err or "CDN gagal.")

    def gen():
        try:
            for chunk in iter(lambda: upstream.read(CHUNK), b""):
                yield chunk
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    resp = Response(gen(), content_type="video/mp2t")
    length = upstream.headers.get("Content-Length")
    if length:
        resp.headers["Content-Length"] = length
    resp.headers["Cache-Control"] = "public, max-age=3600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


MEDIA_TTL_MS = 3 * 3600 * 1000


def create_media_token(url: str, stream: str | None = None, fname: str = "video.mp4",
                       exp: int | None = None, referer: str | None = None) -> str:
    """Buat token media (UC Share / Videy) untuk /m/{token}."""
    token = secrets.token_urlsafe(14)
    db.kv_set(f"dl:{token}", {
        "url": url,
        "stream": stream or url,
        "f": fname,
        "exp": exp if exp is not None else int(time.time() * 1000) + MEDIA_TTL_MS,
        "referer": referer,
    })
    return token


def _media_info(token: str) -> dict | None:
    info = db.kv_get(f"dl:{token}")
    if not info:
        return None
    exp = info.get("exp")
    try:
        if exp and int(time.time() * 1000) > int(exp):
            db.kv_del(f"dl:{token}")
            return None
    except (TypeError, ValueError):
        pass
    return info


_PLAYER_HTML = """<!doctype html>
<html lang="id"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b1220">
<title>{title}</title>
<style>
  * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
  html, body {{ margin: 0; background: #000; color: #fff;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
  video {{ display: block; width: 100%; max-height: 86vh; background: #000; }}
  .bar {{ display: flex; align-items: center; gap: 12px; padding: 12px;
    background: #0b1220; }}
  .bar .name {{ flex: 1; min-width: 0; font-size: 14px; font-weight: 700;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .dl {{ padding: 10px 22px; border-radius: 10px; text-decoration: none;
    font-size: 14px; font-weight: 700; color: #fff; flex-shrink: 0;
    background: linear-gradient(135deg, #2a7dff, #00c2ff);
    box-shadow: 0 2px 10px rgba(42,125,255,.4); }}
  .err {{ color: #ffd6d6; padding: 18px; font-size: 14px; line-height: 1.5; }}
</style>
</head><body>
<video id="v" src="{src}" controls autoplay muted playsinline preload="auto"></video>
<div class="bar">
  <span class="name" id="name">{title}</span>
  <a class="dl" href="{dl}">&#8681; Download</a>
</div>
<script>
  var v = document.getElementById('v');
  var name = document.getElementById('name');
  document.addEventListener('click', function () {{ if (v.muted) v.muted = false; }});
  v.addEventListener('error', function () {{
    name.textContent = 'Gagal memuat video. Link mungkin sudah kedaluwarsa atau format tidak didukung. Gunakan tombol Download.';
  }});
</script>
</body></html>
"""


@app.get("/m/<token>")
def m_player(token: str):
    info = _media_info(token)
    if not info:
        return _err(404, "Link tidak ditemukan atau sudah kedaluwarsa. Buat ulang dari Telegram.")
    dl = request.args.get("dl") == "1"
    raw = request.args.get("raw") == "1"
    if not raw and not dl:
        fname = html.escape(info.get("f") or "video.mp4")
        html_page = _PLAYER_HTML.format(title=fname, src=f"/m/{token}?raw=1",
                                        dl=f"/m/{token}?dl=1")
        resp = Response(html_page, mimetype="text/html")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    target = info.get("url") if dl else (info.get("stream") or info.get("url"))
    if not target:
        return _err(404, "Tidak ada sumber stream/download.")

    headers = {"User-Agent": UA, "Accept": "*/*"}
    referer = info.get("referer")
    if referer:
        headers["Referer"] = referer
    rng = request.headers.get("Range")
    if rng:
        headers["Range"] = rng

    upstream = None
    last_err = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(target, headers=headers)
            upstream = urllib.request.urlopen(req, timeout=60)
            break
        except urllib.error.HTTPError as e:
            last_err = f"CDN menolak (HTTP {e.code})."
        except Exception as e:
            last_err = str(e)
        time.sleep(1.5 * attempt)
    if upstream is None:
        return _err(502, last_err or "CDN gagal.")

    def gen():
        try:
            while True:
                chunk = upstream.read(CHUNK)
                if not chunk:
                    break
                yield chunk
        except Exception:
            pass
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    mime = upstream.headers.get("Content-Type") or mime_for(info.get("f") or "video.mp4")
    if not mime.startswith(("video/", "audio/", "image/")):
        mime = mime_for(info.get("f") or "video.mp4")
    resp = Response(gen(), status=upstream.status, content_type=mime)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "no-store"
    cl = upstream.headers.get("Content-Length")
    if cl:
        resp.headers["Content-Length"] = str(cl)
    cr = upstream.headers.get("Content-Range")
    if cr:
        resp.headers["Content-Range"] = cr
    if dl:
        safe = (info.get("f") or "video.mp4").replace('"', "_")
        resp.headers["Content-Disposition"] = f'attachment; filename="{safe}"'
    else:
        resp.headers["Content-Disposition"] = "inline"
    return resp


@app.get("/s/<token>")
def s_subtitle(token: str):
    info = db.kv_get(f"srt:{token}")
    if not info or not info.get("url"):
        return _err(404, "Link tidak ditemukan atau sudah kedaluwarsa.")
    try:
        data, fname = subsrt.fetch_subtitle_srt(info["url"])
    except Exception as e:
        return _err(502, str(e)[:200])
    resp = Response(data, mimetype="application/x-subrip")
    safe = fname.replace('"', "_")
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe}"'
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5001")))