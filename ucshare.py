"""Resolver UC Share (uc-share.com) — port dari CF Worker bypass-bot (JS)."""

import re
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "https://m-intldrive.ucweb.com/1/clouddrive/share/sharepage"
ORIGIN = "https://uc-share.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": UA})


def _api(path: str, payload: dict, headers: dict | None = None) -> dict:
    r = _SESSION.post(
        f"{BASE}/{path}",
        json=payload,
        headers={
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Origin": ORIGIN,
            "Referer": f"{ORIGIN}/",
            "Accept": "application/json, text/plain, */*",
            **(headers or {}),
        },
        timeout=15,
    )
    try:
        data = r.json()
    except ValueError:
        data = None
    if not data or data.get("code") != 0:
        msg = (data or {}).get("message") if isinstance(data, dict) else None
        raise RuntimeError(f"UC request gagal: {msg or ('HTTP ' + str(r.status_code))}")
    return data


def _format_duration(sec) -> str | None:
    if sec is None or sec == "" or sec == "-":
        return None
    try:
        t = int(round(float(sec)))
    except (TypeError, ValueError):
        return str(sec)
    if t <= 0:
        return None
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    pad = lambda v: str(v).zfill(2)
    return f"{h}:{pad(m)}:{pad(s)}" if h else f"{m}:{pad(s)}"


def resolve(url: str) -> dict:
    """Resolve share UC Share -> {title, url, medias:[{title,url,stream_url,size,quality,duration}]}."""
    m = re.search(r"uc-share\.com/s/([a-z0-9]+)", url, re.I) or re.search(
        r"[?&]s=([a-z0-9]+)", url, re.I
    )
    if not m:
        raise ValueError("pwd_id UC Share tidak ditemukan pada URL")
    pwd_id = m.group(1)
    pm = re.search(r"[?&](?:pwd|passcode|pass)=([^&]+)", url, re.I)
    passcode = pm.group(1) if pm else ""

    detail = _api("v2/detail", {
        "pwd_id": pwd_id, "passcode": passcode, "force": 0, "page": 1, "size": 50,
        "fetch_banner": 1, "fetch_share": 1, "fetch_total": 1, "sort": "",
        "banner_platform": "pc", "fetch_error_background": 1, "web_platform": "pc",
        "fetch_follow_status": 1, "ip_limit": 0,
    })
    stoken = ((detail.get("data") or {}).get("token_info") or {}).get("stoken") or ""
    if not stoken:
        raise ValueError("stoken UC Share tidak didapatkan")

    files: list[dict] = []
    seen_dirs: set = set()
    queue: list[str] = ["0"]
    while queue:
        fid = queue.pop(0)
        d = _api("v2/detail", {
            "pwd_id": pwd_id, "passcode": passcode, "stoken": stoken, "pdir_fid": fid,
            "force": 0, "_page": 1, "_size": 50,
            "_fetch_banner": 1 if fid == "0" else 0,
            "_fetch_share": 1 if fid == "0" else 0,
            "_fetch_total": 1, "_sort": "", "ip_limit": 0, "web_platform": "pc",
        })
        lst = ((d.get("data") or {}).get("detail_info") or {}).get("list") or []
        if not lst:
            if fid == "0":
                break
            continue
        for x in lst:
            if x.get("dir"):
                dfid = x.get("fid")
                if dfid and dfid not in seen_dirs:
                    seen_dirs.add(dfid)
                    queue.append(dfid)
            else:
                files.append(x)
    if not files:
        raise ValueError("share kosong / sudah expired")

    medias: list[dict] = []
    lock = threading.Lock()

    def one(x: dict):
        try:
            params = urllib.parse.urlencode({
                "pwd_id": pwd_id, "fid": x.get("fid"),
                "fid_token": x.get("share_fid_token") or "", "stoken": stoken,
            })
            r = _SESSION.get(
                f"{BASE}/video_preview?{params}",
                headers={"User-Agent": UA, "x-clouddrive-st": stoken, "Referer": f"{ORIGIN}/"},
                timeout=15,
            )
            play = ((r.json() or {}).get("data") or {}).get("play_info") if r.headers.get(
                "content-type", "").startswith("application/json") else None
            if not play:
                play = {}
            murl = play.get("url") or ""
            quality = f"{play.get('width')}x{play.get('height')}" if play.get("width") else None
            entry = {
                "title": x.get("file_name") or "",
                "url": murl,
                "stream_url": murl,
                "size": play.get("size") or x.get("size") or None,
                "quality": quality,
                "duration": _format_duration(play.get("duration") or x.get("duration")),
            }
            with lock:
                medias.append(entry)
        except Exception:
            with lock:
                medias.append({
                    "title": x.get("file_name") or "", "url": "", "stream_url": "",
                    "size": x.get("size") or None, "quality": None, "duration": None,
                })

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(one, files))

    return {"title": detail.get("data", {}).get("share_name") or "", "url": url, "medias": medias}