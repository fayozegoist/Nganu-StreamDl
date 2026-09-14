"""Ambil file .srt dari subtitlecat.com (untuk fitur Subtitle)."""

import os
import re
import urllib.parse

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36")
HDRS = {"User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def _prefer(links: list[str]) -> str:
    for pref in (r"-id\.srt$", r"-en\.srt$"):
        for l in links:
            if re.search(pref, l, re.I):
                return l
    return links[0]


def fetch_subtitle_srt(detail_url: str) -> tuple[bytes, str]:
    """Unduh .srt dari halaman detail subtitlecat: prefer Indonesian (-id), lalu English (-en)."""
    r = requests.get(detail_url, headers=HDRS, timeout=20)
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}")
    links = re.findall(r'href="([^"]+\.srt)"', r.text, re.I)
    if not links:
        raise RuntimeError("Link SRT tidak ditemukan.")
    srt_url = urllib.parse.urljoin(detail_url, _prefer(links))
    rd = requests.get(srt_url, headers=HDRS, timeout=30)
    if not rd.ok:
        raise RuntimeError(f"HTTP {rd.status_code}")
    if not rd.content.strip():
        raise RuntimeError("SRT kosong.")
    fname = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(srt_url).path) or "subtitle.srt")
    return rd.content, fname