#!/usr/bin/env python3
"""Pemenggal + unduh link TeraBox via getindevice.com API."""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

BASE = "https://getindevice.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
COMMON_HEADERS = {
    "User-Agent": UA,
    "Referer": f"{BASE}/terabox-downloader/",
    "Origin": BASE,
}
NO_REFERER = {"User-Agent": UA}
TERABOX_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.terabox.app/",
    "Origin": "https://www.terabox.app",
}
VALID_DOMAINS = (
    "terabox.com", "1024terabox.com", "teraboxapp.com", "terasharelink.com",
)
RETRY_COUNT = 3
BATCH_SIZE = 15
CHUNK = 256 * 1024
URL_VERBOSE = False

_SESSION = requests.Session()
_adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=0)
_SESSION.mount("https://", _adapter)
_SESSION.mount("http://", _adapter)


def _get(url: str, extra_headers: dict = None, timeout: int = 15) -> dict:
    h = {**COMMON_HEADERS, **(extra_headers or {})}
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _post(url: str, body: dict, extra_headers: dict = None, timeout: int = 90) -> dict:
    h = {**COMMON_HEADERS, "Content-Type": "application/json", **(extra_headers or {})}
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _fetch_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=COMMON_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def get_token() -> str:
    url = f"{BASE}/api/token/?_t={int(time.time() * 1000)}"
    return _get(url)["token"]


def resolve(url: str, token: str) -> dict:
    return _post(f"{BASE}/api/download/", {"url": url},
                 extra_headers={"x-request-token": token})


def fix_hls_url(hls_url: str) -> str:
    if "/api/hls-stream" in hls_url and "/api/hls-stream/" not in hls_url:
        return hls_url.replace("/api/hls-stream?", "/api/hls-stream/?")
    return hls_url


def parse_m3u8_segments(m3u8_text: str) -> list[str]:
    urls = []
    for line in m3u8_text.splitlines():
        line = line.strip()
        if line.startswith("https://"):
            urls.append(line)
    return urls


def segment_inner_url(segment_url: str) -> str | None:
    """Decode URL CDN TeraBox asli dari segmen media-download."""
    try:
        parsed = urllib.parse.urlparse(segment_url)
        params = urllib.parse.parse_qs(parsed.query)
        if "url" in params:
            return urllib.parse.unquote(params["url"][0])
    except Exception:
        pass
    return None


def fetch_ts_size(inner_url: str) -> int | None:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(inner_url).query, keep_blank_values=True)
    if "ts_size" in q:
        try:
            return int(q["ts_size"][0])
        except ValueError:
            return None
    return None


def inner_segment_url(segment_url: str) -> str | None:
    """URL CDN asli TeraBox (v4.freeterabox.com) dari baris segmen media-download."""
    parsed = urllib.parse.urlparse(segment_url)
    host = (parsed.hostname or "").lower()
    if "freeterabox.com" in host and parsed.path.startswith("/video/"):
        return segment_url
    return segment_inner_url(segment_url)


def build_full_cdn_url(segment_url: str) -> str | None:
    """Buat link CDN full dari segmen: ganti range=0-<ts_size-1>, hapus len."""
    inner = segment_inner_url(segment_url)
    if not inner:
        return None
    p = urllib.parse.urlparse(inner)
    q = urllib.parse.parse_qs(p.query, keep_blank_values=True)
    ts = fetch_ts_size(inner)
    if ts is None:
        return None
    q.pop("len", None)
    q["range"] = [f"0-{ts - 1}"]
    return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(q, doseq=True)))


def filename_from_segment(segment_url: str) -> str | None:
    inner = segment_inner_url(segment_url)
    if not inner:
        return None
    q = urllib.parse.parse_qs(urllib.parse.urlparse(inner).query, keep_blank_values=True)
    if "fn" in q:
        try:
            return urllib.parse.unquote(q["fn"][0])
        except Exception:
            return q["fn"][0]
    return None


def total_size_from_segments(segments: list[str]) -> int:
    """Jumlah byte seluruh segmen (dari param `len=` di inner URL). 0 jika tak diketahui."""
    total = 0
    for raw in segments:
        inner = inner_segment_url(raw) or raw
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(inner).query, keep_blank_values=True)
            ln = q.get("len", q.get("range", [""]))[0]
            if ln and "-" in ln:
                a, b = ln.split("-", 1)
                total += int(b) - int(a) + 1
            elif ln:
                total += int(ln)
        except Exception:
            continue
    return total


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    name = re.sub(r'\s+', ' ', name).strip()
    if len(name) > 200:
        name = name[:200]
    return name


def _stream_to_file(url: str, out_path: str, headers: dict, timeout: int = 600) -> int:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = 0
        with open(out_path, "wb") as f:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
        return total


def download_url_with_retry(url: str, out_path: str, headers: dict = None,
                            timeout: int = 900, attempts: int = 5) -> bool:
    h = headers or NO_REFERER
    backoff = [3, 8, 15, 25, 40]
    for attempt in range(1, attempts + 1):
        try:
            _stream_to_file(url, out_path, h, timeout)
            return True
        except Exception as e:
            if URL_VERBOSE:
                print(f"  (coba {attempt}/{attempts}: {e})", file=sys.stderr)
            if attempt < attempts:
                time.sleep(backoff[min(attempt - 1, len(backoff) - 1)])
            else:
                print(f"  Gagal mengunduh setelah {attempts}x: {e}", file=sys.stderr)
                return False
    return False


def remux_ts_to_mp4(ts_path: str, mp4_path: str) -> bool:
    if not shutil.which("ffmpeg"):
        print("  ffmpeg tidak ditemukan, menyimpan .ts", file=sys.stderr)
        return False
    print(f"  Remux ke .mp4...")
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", ts_path, "-c", "copy", "-movflags", "+faststart", mp4_path],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode == 0 and os.path.exists(mp4_path):
        return True
    print(f"  ffmpeg gagal: {result.stderr[-300:]}", file=sys.stderr)
    return False


SEG_BACKOFF = [2, 4, 8]


def download_segments_parallel(segments: list[str], output_dir: str,
                               ts_size: int = None, workers: int = 6,
                               progress=None) -> str | None:
    """Unduh segmen byte-range secara paralel (retry 3x/segmen), lalu concat
    berurutan ke .ts. Return path .ts jika lengkap, None jika ada yang gagal.
    progress(done_bytes, ts_size) dipanggil dari thread pemanggil."""
    total = len(segments)
    if not total:
        return None
    os.makedirs(output_dir, exist_ok=True)
    ts_path = os.path.join(output_dir, f"dl_{os.getpid()}_{int(time.time())}.ts")
    done = [0]

    def seg_task(args):
        i, url = args
        tmp = os.path.join(output_dir, f".seg_{os.getpid()}_{i:04d}")
        inner = inner_segment_url(url)
        attempts = [url] if inner is None or inner == url else [inner, url]
        for attempt in range(1, RETRY_COUNT + 1):
            cand = attempts[min(attempt - 1, len(attempts) - 1)]
            headers = TERABOX_HEADERS if cand != url else NO_REFERER
            try:
                with _SESSION.get(cand, headers=headers, timeout=60, stream=True) as resp:
                    resp.raise_for_status()
                    with open(tmp, "wb") as f:
                        for c in resp.iter_content(CHUNK):
                            if not c:
                                continue
                            f.write(c)
                if attempt == 2 and URL_VERBOSE:
                    print(f"  Segmen {i} fallback proxy", file=sys.stderr)
                return (i, tmp, True, attempt == 1 and len(attempts) == 2)
            except Exception as e:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                if attempt == RETRY_COUNT:
                    if URL_VERBOSE:
                        print(f"  Segmen {i} gagal: {e}", file=sys.stderr)
                    return (i, tmp, False, False)
                time.sleep(SEG_BACKOFF[min(attempt - 1, len(SEG_BACKOFF) - 1)])
        return (i, tmp, False, False)

    stats = {"fast": 0, "proxy": 0, "fail": 0}
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(seg_task, (i, u)): i for i, u in enumerate(segments)}
            with open(ts_path, "wb") as out:
                for i in range(total):
                    fut = next((k for k, v in futures.items() if v == i), None)
                    if fut is None:
                        return None
                    _, tmp, ok, was_fast = fut.result()
                    if not ok:
                        stats["fail"] += 1
                        print(f"  Segmen {i} gagal — unduhan dibatalkan.", file=sys.stderr)
                        return None
                    stats["fast" if was_fast else "proxy"] += 1
                    with open(tmp, "rb") as sf:
                        while True:
                            c = sf.read(CHUNK)
                            if not c:
                                break
                            out.write(c)
                            done[0] += len(c)
                            if progress and ts_size:
                                progress(done[0], ts_size)
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
    except Exception as e:
        print(f"  Parallel download gagal: {e}", file=sys.stderr)
        return None
    print(f"  Parallel stats: {stats}", file=sys.stderr)
    return ts_path


def download_and_remux_parallel(segments: list[str], output_dir: str, mp4_path: str,
                                ts_size: int = None, workers: int = 6, progress=None) -> bool:
    """Parallel download segmen -> remux ke mp4. Return True jika mp4 utuh."""
    if mp4_path and os.path.exists(mp4_path):
        return True
    ts_path = download_segments_parallel(segments, output_dir, ts_size=ts_size,
                                         workers=workers, progress=progress)
    if not ts_path or not os.path.exists(ts_path):
        return False
    try:
        if remux_ts_to_mp4(ts_path, mp4_path):
            return True
    finally:
        try:
            os.remove(ts_path)
        except OSError:
            pass
    return False


def download_item_segments(item: dict, output_dir: str) -> str | None:
    """Unduh via playlist segmen (fallback). Return path .ts."""
    hls_url = fix_hls_url(item.get("hlsStreamUrl", ""))
    try:
        m3u8_text = _fetch_text(hls_url)
    except Exception as e:
        print(f"  Gagal fetch m3u8: {e}", file=sys.stderr)
        return None

    segments = [s for s in parse_m3u8_segments(m3u8_text)]
    if not segments:
        print(f"  Tidak ada segmen di playlist.", file=sys.stderr)
        return None

    total = len(segments)
    print(f"  Mode segmen: {total} segmen.")

    fn = filename_from_segment(segments[0])
    base = sanitize_filename(Path(fn).stem) if fn else sanitize_filename(item.get("quality", f"video_{item.get('index', 0)}"))
    ts_path = os.path.join(output_dir, f"{base}.ts")

    def seg_task(args):
        i, url = args
        tmp = os.path.join(output_dir, f".seg_{i:04d}")
        req = urllib.request.Request(url, headers=NO_REFERER)
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                with open(tmp, "wb") as f:
                    while True:
                        c = resp.read(CHUNK)
                        if not c:
                            break
                        f.write(c)
            return (i, tmp, True)
        except Exception as e:
            print(f"  Segmen {i} gagal: {e}", file=sys.stderr)
            return (i, tmp, False)

    failed = False
    with open(ts_path, "wb") as ts_file:
        idx = 0
        for start in range(0, total, BATCH_SIZE):
            batch = segments[start:start + BATCH_SIZE]
            tasks = [(start + j, u) for j, u in enumerate(batch)]
            results = {}
            with ThreadPoolExecutor(max_workers=BATCH_SIZE) as pool:
                futures = {pool.submit(seg_task, t): t for t in tasks}
                for f in as_completed(futures):
                    i, tmp, ok = f.result()
                    results[i] = (tmp, ok)
            for j, (tmp, ok) in results.items():
                if ok and os.path.exists(tmp):
                    with open(tmp, "rb") as sf:
                        while True:
                            c = sf.read(CHUNK)
                            if not c:
                                break
                            ts_file.write(c)
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                else:
                    failed = True
            idx += len(batch)
            print(f"\r  Segmen {idx}/{total} ({idx * 100 // total}%)", end="", flush=True)
    print()

    if failed:
        print(f"  Sebagian segmen gagal — file mungkin tidak utuh.", file=sys.stderr)
    return ts_path


def download_item_full(item: dict, output_dir: str) -> str | None:
    """Unduh via link CDN full (1 request). Return path .ts atau None."""
    hls_url = fix_hls_url(item.get("hlsStreamUrl", ""))
    try:
        m3u8_text = _fetch_text(hls_url)
    except Exception as e:
        print(f"  Gagal fetch m3u8: {e}", file=sys.stderr)
        return None

    segments = parse_m3u8_segments(m3u8_text)
    if not segments:
        print(f"  Tidak ada segmen di playlist.", file=sys.stderr)
        return None

    full_url = build_full_cdn_url(segments[0])
    if not full_url:
        print(f"  Gagal membangun link full (ts_size tidak ada).", file=sys.stderr)
        return None

    fn = filename_from_segment(segments[0])
    if fn:
        base = sanitize_filename(Path(fn).stem)
    else:
        base = sanitize_filename(item.get("quality", f"video_{item.get('index', 0)}"))

    ts_path = os.path.join(output_dir, f"{base}.ts")
    mp4_path = os.path.join(output_dir, f"{base}.mp4")

    print(f"  Link full: {full_url[:110]}...")
    ok = download_url_with_retry(full_url, ts_path, headers=NO_REFERER, timeout=900)
    if not ok:
        try:
            os.remove(ts_path)
        except OSError:
            pass
        return None

    ts_size = os.path.getsize(ts_path)
    print(f"  .ts selesai: {ts_size / (1024 * 1024):.1f} MB")

    if remux_ts_to_mp4(ts_path, mp4_path):
        print(f"  .mp4 selesai: {os.path.getsize(mp4_path) / (1024 * 1024):.1f} MB")
        os.remove(ts_path)
        return mp4_path
    return ts_path


def download_and_remux(item: dict, output_dir: str) -> str | None:
    result = download_item_full(item, output_dir)
    if result:
        return result

    fallback_ts = download_item_segments(item, output_dir)
    if not fallback_ts:
        return None

    mp4_candidate = fallback_ts.replace(".ts", ".mp4")
    if remux_ts_to_mp4(fallback_ts, mp4_candidate):
        try:
            os.remove(fallback_ts)
        except OSError:
            pass
        return mp4_candidate
    return fallback_ts


def main():
    parser = argparse.ArgumentParser(description="Pemenggal + unduh link TeraBox via getindevice.com")
    parser.add_argument("url", help="Link TeraBox")
    parser.add_argument("-o", "--output", default="hasil.json", help="File output JSON (default: hasil.json)")
    parser.add_argument("-d", "--download", metavar="N|all",
                        help="Unduh video: 'all' untuk semua, atau indeks (contoh: 1,3,5 atau 2-4)")
    parser.add_argument("--full-links", action="store_true",
                        help="Cetak link CDN full tiap video (bukan link m3u8)")
    parser.add_argument("--fetch-cdn", action="store_true",
                        help="[deprecated, gunakan --full-links] Cetak direct CDN URL")
    parser.add_argument("--out-dir", default="downloads", help="Direktori output unduhan (default: downloads)")
    args = parser.parse_args()

    url = args.url.strip()
    domain = re.search(r"https?://([^/]+)", url)
    if not domain or not any(domain.group(1).endswith(d) for d in VALID_DOMAINS):
        print(f"Error: URL bukan TeraBox valid.\n  Domain harus salah satu dari: {', '.join(VALID_DOMAINS)}",
              file=sys.stderr)
        sys.exit(1)

    print("[1/3] Mengambil token...")
    token = get_token()

    print(f"[2/3] Resolve: {url}")
    data = resolve(url, token)

    all_items = []
    for kind in ("videos", "photos", "audios"):
        for item in data.get(kind, []):
            item["kind"] = kind.rstrip("s")
            all_items.append(item)
    all_items.sort(key=lambda x: x.get("order", 0))
    for idx, item in enumerate(all_items, 1):
        item["index"] = idx

    if not all_items:
        print("Error: Tidak ada file ditemukan dalam link ini.", file=sys.stderr)
        print(json.dumps(data, indent=2, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    print(f"[3/3] {len(all_items)} file ditemukan\n")
    title = data.get("title", "Tanpa judul")
    source = data.get("source", "unknown")

    print(f"{'='*70}")
    print(f"Judul  : {title}")
    print(f"Sumber : {source}")
    print(f"File   : {len(all_items)}")
    print(f"{'='*70}\n")

    files = []
    for item in all_items:
        kind = item["kind"]
        idx = item["index"]
        quality = item.get("quality", "N/A")
        fmt = item.get("format", "bin")
        is_hls = item.get("is_hls", False)
        direct_url = item.get("url", "")
        sig = item.get("sig", "")
        hls_url = item.get("hlsStreamUrl", "")
        cid = item.get("fid", item.get("id", ""))

        full_link = None
        if is_hls and hls_url:
            hls_url_fixed = fix_hls_url(hls_url)
            if args.full_links or args.fetch_cdn:
                try:
                    m3u8_text = _fetch_text(hls_url_fixed)
                    segs = parse_m3u8_segments(m3u8_text)
                    if segs:
                        full_link = build_full_cdn_url(segs[0])
                except Exception:
                    full_link = None

            files.append({
                "kind": kind, "index": idx, "quality": quality, "format": fmt,
                "is_hls": True, "fid": cid,
                "hls_stream_url": hls_url_fixed,
                "full_cdn_url": full_link,
            })
            label = f"[{kind.upper()} {idx}]"
            print(f"  {label} {quality}  .{fmt.upper()}")
            print(f"    m3u8  : {hls_url_fixed}")
            if full_link:
                print(f"    FULL  : {full_link}")
            print()
        elif direct_url:
            dl_url = ""
            if sig:
                encoded = base64.urlsafe_b64encode(direct_url.encode()).decode()
                dl_url = f"https://dl.getindevice.com/api/media-download?url={encoded}&ext={fmt}&platform=terabox&sig={sig}"
            files.append({
                "kind": kind, "index": idx, "quality": quality, "format": fmt,
                "is_hls": False, "direct_url": direct_url, "download_url": dl_url or None,
            })
            label = f"[{kind.upper()} {idx}]"
            print(f"  {label} {quality}  .{fmt.upper()}")
            print(f"    URL   : {direct_url}")
            if dl_url:
                print(f"    DL    : {dl_url}")
            print()

    Path(args.output).write_text(json.dumps({
        "title": title, "source": source, "url": url,
        "file_count": len(files), "files": files,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{'='*70}")
    print(f"Hasil tersimpan: {args.output}")

    if not args.download:
        return

    hls_items = [i for i in all_items if i.get("is_hls") and i.get("hlsStreamUrl")]
    if not hls_items:
        print("\nTidak ada video HLS untuk diunduh.", file=sys.stderr)
        sys.exit(1)

    spec = args.download.strip().lower()
    if spec == "all":
        targets = hls_items
    else:
        selected = set()
        for part in spec.split(","):
            part = part.strip()
            if part.isdigit():
                selected.add(int(part))
            elif "-" in part:
                a, b = part.split("-", 1)
                if a.isdigit() and b.isdigit():
                    selected.update(range(int(a), int(b) + 1))
        targets = [i for i in hls_items if i["index"] in selected]
        if not targets:
            print(f"\nTidak ada video untuk indeks: {spec}", file=sys.stderr)
            print(f"Indeks tersedia: {', '.join(str(i['index']) for i in hls_items)}", file=sys.stderr)
            sys.exit(1)

    os.makedirs(args.out_dir, exist_ok=True)
    print(f"\n{'='*70}")
    print(f"Mengunduh {len(targets)} video ke: {args.out_dir}/")
    print(f"{'='*70}\n")

    results = []
    for i, item in enumerate(targets, 1):
        idx = item["index"]
        quality = item.get("quality", "N/A")
        print(f"[{i}/{len(targets)}] Video #{idx} — {quality}")
        path = download_and_remux(item, args.out_dir)
        if path:
            results.append(path)
            print(f"  Selesai: {path}")
        else:
            print(f"  Gagal mengunduh video #{idx}")
        print()

    print(f"{'='*70}")
    print(f"Selesai! {len(results)}/{len(targets)} video berhasil diunduh")
    print(f"Lokasi: {os.path.abspath(args.out_dir)}/")


if __name__ == "__main__":
    main()