"""Fallback API resmi Terabox (dm.terabox.app) — HANYA untuk file yang tidak
disediakan getindevice (foto/file tanpa sumber). Butuh cookie ndus di env TERA_NDUS.

Catatan: dlink resmi di-throttle akun free (~0.2 MB/s), jadi jalur ini tidak
pernah dipakai untuk video/streaming — hanya file kecil tanpa sumber lain.
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request

UA_OFFICIAL = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/145.0.0.0 Safari/537.36"
TTL = 10 * 60
MAX_FILES = 200
MAX_DEPTH = 4

_cache: dict = {}


def _ndus() -> str:
    return (os.environ.get("TERA_NDUS") or "").strip()


def available() -> bool:
    return bool(_ndus())


def stream_headers() -> dict:
    """Header untuk fetch dlink resmi (butuh cookie)."""
    return {"User-Agent": UA_OFFICIAL, "Cookie": f"ndus={_ndus()}",
            "Referer": "https://www.terabox.app/"}


def _headers(extra: dict | None = None) -> dict:
    h = {"User-Agent": UA_OFFICIAL, "Cookie": f"ndus={_ndus()}"}
    if extra:
        h.update(extra)
    return h


def _get(url: str, headers: dict, timeout: int = 30) -> str:
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=timeout
    ).read().decode("utf-8", "replace")


def extract_surl(share_url: str) -> str | None:
    m = re.search(r"/s/([a-zA-Z0-9_-]+)", share_url or "")
    if m:
        return m.group(1)
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(share_url).query)
        return (q.get("surl") or [None])[0]
    except Exception:
        return None


def _norm(it: dict) -> dict:
    th = it.get("thumbs") or {}
    name = it.get("server_filename") or it.get("filename") or "file"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return {
        "name": name,
        "ext": ext,
        "size_bytes": int(it.get("size") or 0),
        "dlink": it.get("dlink"),
        "thumb": th.get("url3") or th.get("url2") or th.get("url1") or None,
        "path": it.get("path"),
    }


def list_files(share_url: str) -> list[dict] | None:
    """Daftar file share via API resmi. Return None jika tak tersedia/gagal."""
    ndus = _ndus()
    if not ndus:
        return None
    surl = extract_surl(share_url)
    if not surl:
        return None
    short = surl[1:] if surl.startswith("1") else surl

    hit = _cache.get(surl)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]

    try:
        text = _get(f"https://dm.terabox.app/sharing/link?surl={surl}", _headers())
        m = re.search(r"fn%28%22(.*?)%22%29", text)
        if not m:
            return None
        js = m.group(1)
        ah = {
            "User-Agent": UA_OFFICIAL,
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://dm.terabox.app/sharing/link?surl={short}&clearCache=1",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://dm.terabox.app",
            "Cookie": f"ndus={ndus}",
        }

        def share_list(dir_path: str | None) -> dict:
            q = ("https://dm.terabox.app/share/list?app_id=250528&jsToken="
                 + urllib.parse.quote(js)
                 + "&shorturl=" + urllib.parse.quote(short)
                 + "&site_referer=" + urllib.parse.quote("https://www.terabox.app/")
                 + ("&dir=" + urllib.parse.quote(dir_path) if dir_path else "&root=1"))
            return json.loads(_get(q, ah))

        root = share_list(None)
        if root.get("errno") != 0:
            return None

        files: list[dict] = []
        queue: list[tuple[str, int]] = []
        for it in root.get("list", []):
            if str(it.get("isdir")) == "1":
                if it.get("path"):
                    queue.append((it["path"], 0))
            elif it.get("dlink"):
                files.append(_norm(it))

        while queue and len(files) < MAX_FILES:
            path, dep = queue.pop(0)
            if dep >= MAX_DEPTH:
                continue
            d = share_list(path)
            if d.get("errno") != 0:
                continue
            for it in d.get("list", []):
                if str(it.get("isdir")) == "1":
                    if it.get("path"):
                        queue.append((it["path"], dep + 1))
                elif it.get("dlink"):
                    files.append(_norm(it))

        _cache[surl] = (time.time(), files)
        return files or None
    except Exception:
        return None


def match(files: list[dict], ext: str, ordinal: int, size_hint: int = 0) -> dict | None:
    """Cocokkan item getindevice ke file resmi: by ekstensi, urutan ke-`ordinal`
    (1-based) di antara kandidat, dan size terdekat bila size_hint > 0."""
    if not files:
        return None
    cands = [f for f in files if f.get("dlink") and (not ext or f.get("ext") == (ext or "").lower())]
    if not cands:
        cands = [f for f in files if f.get("dlink")]
    if not cands:
        return None
    if size_hint:
        tol = max(size_hint * 0.3, 2048)
        close = [f for f in cands if abs((f.get("size_bytes") or 0) - size_hint) <= tol]
        if close:
            cands = close
    idx = max(0, min(int(ordinal or 1) - 1, len(cands) - 1))
    return cands[idx]