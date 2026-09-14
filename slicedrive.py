"""Resolver SliceDrive / Videy / Vidmonstr / Vid3y — port dari CF Worker bypass-bot (JS)."""

import re
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

CDN_HOSTS = [
    "https://cdn2.videy.co/",
    "https://cdn.slicerdrive.online/",
    "https://box.vchod.cc/videos/",
    "https://cdn.aceimg.com/",
    "https://cdn2.aceimg.com/",
]

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": UA})


def _fmt_size(size) -> str:
    if size is None or size == 0:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(size)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.0f} {units[i]}" if i else f"{v:.0f} {units[i]}"


def _get(url: str, **kw):
    kw.setdefault("timeout", 12)
    kw.setdefault("allow_redirects", True)
    kw.setdefault("headers", {"User-Agent": UA})
    return _SESSION.get(url, **kw)


def resolve(url: str) -> dict:
    """Resolve link SliceDrive/Videy/Vidmonstr -> {url, medias:[{title,url,stream_url,size,quality}]}."""
    url = (url or "").strip()
    # 1) file langsung (HEAD)
    try:
        c = _SESSION.head(url, headers={"User-Agent": UA}, timeout=8, allow_redirects=True)
        ctype = c.headers.get("content-type") or ""
        if c.ok and (ctype.startswith("video/") or ctype.startswith("application/octet-stream")
                     or re.search(r"\.(mp4|webm|mkv|avi|mov)$", url, re.I)):
            size = c.headers.get("content-length")
            size = int(size) if size else None
            title = url.split("/")[-1].replace(".mp4", "", 1) or "video"
            return {"url": url, "medias": [{"title": title, "url": url, "stream_url": url,
                                            "size": size, "quality": None}]}
    except Exception:
        pass

    # 2) ekstrak kode video
    m = (re.search(r"slicedirev\.com/([A-Za-z0-9_-]+)", url, re.I)
         or re.search(r"slicerdrive\.online/([A-Za-z0-9_-]+)", url, re.I)
         or re.search(r"slicedrive\.com/([A-Za-z0-9_-]+)", url, re.I)
         or re.search(r"vid3y\.my\.id/\?v=([A-Za-z0-9_-]+)", url, re.I)
         or re.search(r"videy\.co/(?:v/)?([A-Za-z0-9_-]{5,}(?:\.mp4)?)", url, re.I))
    code = m.group(1) if m else None

    try:
        is_cdn = re.match(r"^cdn\.", (url.split("//")[1].split("/")[0] if "//" in url else url), re.I) is not None
    except Exception:
        is_cdn = False

    final_url = url
    if not code or is_cdn:
        try:
            r = _get(url, headers={"User-Agent": UA, "Accept": "*/*"})
            final_url = r.url or url
            code = (re.search(r"/([A-Za-z0-9_-]{6,})$", final_url)
                    or re.search(r"[?&]v=([A-Za-z0-9_-]+)", final_url))
            code = code.group(1) if code else code
        except Exception:
            pass

    if not code:
        raise ValueError("kode video tidak ditemukan pada URL")

    # 3) vidmonstr (folder / file)
    mf = re.search(r"vid(?:3\.de|monstr\.com)/f/([A-Za-z0-9]+)/?$", final_url, re.I)
    if mf:
        items = _folder(mf.group(1))
        if not items:
            raise ValueError("folder kosong / gagal dibaca")
        return {"url": url, "medias": items}
    md = re.search(r"vidmonstr\.com/d/([A-Za-z0-9]+)/?$", final_url, re.I)
    if md:
        item = _file(md.group(1))
        if item:
            return {"url": url, "medias": [item]}
        raise ValueError("gagal membaca halaman file vidmonstr")

    # 4) probe CDN
    code = re.sub(r"\.mp4$", "", code)
    found = []
    for host in CDN_HOSTS:
        cand = host + code + ".mp4"
        try:
            r = _SESSION.get(cand, headers={"User-Agent": UA, "Range": "bytes=0-64"},
                             allow_redirects=True, timeout=10)
            if r.ok or r.status_code == 206:
                found.append(cand)
        except Exception:
            continue
    if not found:
        raise ValueError("video tidak ditemukan di semua CDN")

    final = found[0]
    size = None
    try:
        r = _SESSION.head(final, headers={"User-Agent": UA}, allow_redirects=True, timeout=10)
        if r.ok:
            cl = r.headers.get("content-length")
            if cl:
                size = int(cl)
    except Exception:
        pass
    if size is None:
        try:
            r = _SESSION.get(final, headers={"User-Agent": UA, "Range": "bytes=0-0"},
                             allow_redirects=True, timeout=10)
            m = re.search(r"/(\d+)$", r.headers.get("content-range") or "")
            if m:
                size = int(m.group(1))
            else:
                cl = r.headers.get("content-length")
                if cl:
                    size = int(cl)
        except Exception:
            pass

    title = url.split("/")[-1].replace(".mp4", "", 1) or "video"
    return {"url": url, "medias": [{"title": title, "url": final, "stream_url": final,
                                    "size": size, "quality": None}]}


def _folder(slug: str) -> list[dict]:
    r = _get(f"https://vidmonstr.com/f/{slug}", headers={"User-Agent": UA, "Accept": "*/*"})
    if not r.ok:
        raise ValueError(f"gagal buka folder vidmonstr: HTTP {r.status_code}")
    html = r.text
    links = re.findall(r'<a[^>]+href="/d/([a-z0-9]+)"[^>]*>([^<]+)</a>', html, re.I)
    names = []
    seen = set()
    for slug2, name in links:
        name = name.strip()
        if not re.search(r"\.(mp4|mkv|webm|avi|mov)$", name, re.I):
            continue
        if slug2 in seen:
            continue
        seen.add(slug2)
        names.append((slug2, name))
    if not names:
        raise ValueError("folder vidmonstr tidak berisi file video")

    out: list[dict] = []
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=4) as ex:
        def run(entry):
            item = _file(entry[0], entry[1])
            if item:
                with lock:
                    out.append(item)
        list(ex.map(run, names))
    return out


def _file(slug: str, name: str | None = None) -> dict | None:
    try:
        r = _get(f"https://vidmonstr.com/d/{slug}", headers={"User-Agent": UA, "Accept": "*/*"})
        if not r.ok:
            return None
        html = r.text
        iframe = re.search(r"iframeId\s*=\s*'([^']+)'", html)
        token = re.search(r"embedToken\s*=\s*'([^']+)'", html)
        if not iframe or not token:
            return None
        r2 = _SESSION.get(
            f"https://vidmonstr.com/ip129jk?id={iframe.group(1)}&t={token.group(1)}",
            headers={"User-Agent": UA, "Referer": f"https://vidmonstr.com/d/{slug}"},
            timeout=10,
        )
        if not r2.ok:
            return None
        sp = re.search(r"stream\.php[^\"'\s]*", r2.text)
        if not sp:
            return None
        stream_path = sp.group(0).replace("\\u0026", "&").replace("&amp;", "&")
        durl = "https://vidmonstr.com/" + stream_path
        r3 = _SESSION.get(durl, headers={"User-Agent": UA, "Referer": f"https://vidmonstr.com/d/{slug}"},
                          timeout=10)
        if not r3.ok:
            return None
        title = (name or slug).replace(".mp4", "", 1)
        m = re.search(r"https://mp4-[\w.-]+\.overfetch\.video/[\w-]+", r3.text)
        if m:
            mp4 = m.group(0)
            size = None
            try:
                r4 = _SESSION.head(mp4, headers={"User-Agent": UA, "Referer": "https://vidmonstr.com/"},
                                   timeout=8, allow_redirects=True)
                if r4.ok:
                    cl = r4.headers.get("content-length")
                    if cl:
                        size = int(cl)
            except Exception:
                pass
            return {"title": title, "url": mp4, "stream_url": mp4, "size": size, "quality": None}
        return {"title": title, "url": durl, "stream_url": durl, "size": None, "quality": None}
    except Exception:
        return None