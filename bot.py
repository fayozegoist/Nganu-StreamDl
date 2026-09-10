#!/usr/bin/env python3
"""Telethon bot — daftar file TeraBox + tombol Stream (web player) & Download (auto unduh).

Menjalankan di proses terpisah (python bot.py) bareng gunicorn di container yang sama.
Tidak mengirim media langsung ke Telegram; semua pengiriman lewat web service (app.py).
"""

import asyncio
import hashlib
import os
import re
import sys
import time
import traceback
import urllib.parse

from telethon import TelegramClient, Button, events
from telethon.sessions import StringSession

import app as backend
import security

API_ID = int(os.environ.get("API_ID", "") or 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_IDS = {x.strip() for x in os.environ.get("OWNER_IDS", "").split(",") if x.strip()}

SESSION_FILE = os.environ.get("TELETHON_SESSION_FILE", "/app/data/session.txt")
BASE_URL = os.environ.get("BASE_URL", "https://terabox-resolver-production-99be.up.railway.app").rstrip("/")

ITEMS_PER_PAGE = 7
NAME_MAX = 60
RESOLVE_CACHE_TTL = 10 * 60

ICONS = {"video": "\U0001F3AC", "audio": "\U0001F3B5", "photo": "\U0001F5BC\uFE0F", "file": "\U0001F4C4"}

START_TEXT = (
    "\u26A1 <b>TeraBox Bot</b>\n\n"
    "Kirim link share TeraBox (<code>https://terabox.com/s/...</code>) dan saya akan berikan "
    "daftar file beserta tombolnya:\n\n"
    "\u25B6\uFE0F <b>Stream</b> \u2014 putar langsung di browser (klik, langsung jalan)\n"
    "\u2B07\uFE0F <b>Download</b> \u2014 unduh otomatis dengan nama file asli\n\n"
    "Stream & download bebas tanpa limit. Ekstrak link baru: "
    "<b>1x per 3 menit</b> (admin bebas).\n"
    "Link tidak kedaluwarsa selama share TeraBox masih aktif.\n\n"
    "Perintah:\n/help \u2014 bantuan\n/id \u2014 lihat ID kamu"
)

EXTRACT_COOLDOWN = 3 * 60
_extract_last: dict = {}

view_map: dict = {}
resolve_cache: dict = {}


def is_owner(uid) -> bool:
    if not OWNER_IDS:
        return True
    return str(uid) in OWNER_IDS


def _extract_cooldown_left(uid) -> int:
    if is_owner(uid):
        return 0
    last = _extract_last.get(uid)
    if not last:
        return 0
    return max(0, int(EXTRACT_COOLDOWN - (time.time() - last)))


def extract_share_url(text: str) -> str | None:
    for m in re.finditer(r"https?://[^\s<>\"']+", text or ""):
        if backend.is_valid_terabox(m.group(0)):
            return m.group(0)
    return None


def _resolve_cached(url: str) -> dict:
    now = time.time()
    hit = resolve_cache.get(url)
    if hit and now - hit[1] < RESOLVE_CACHE_TTL:
        return hit[0]
    data = backend.resolve_terabox(url)
    resolve_cache[url] = (data, now)
    return data


def build_pending_files(data: dict) -> list[dict]:
    out = []
    for f in data["files"]:
        out.append({
            "filename": f.get("name") or f"TeraBox_{f['index'] or 0}.{f.get('format', 'mp4')}",
            "size": f.get("quality", "N/A"),
            "kind": f.get("kind", "file"),
            "url": data["url"],
            "index": f.get("index", 0),
        })
    return out


def file_links(item: dict) -> tuple[str, str] | None:
    kind = item["kind"]
    if not security.has_keys():
        q = urllib.parse.quote(item["url"], safe="")
        dl = f"{BASE_URL}/dl?url={q}&n={item['index']}"
        stream = f"{BASE_URL}/play?url={q}&n={item['index']}" if kind in ("video", "audio") else None
        return (stream, dl)
    q = urllib.parse.quote(item["url"], safe="")
    exp = security.exp_later()
    dl = f"{BASE_URL}/dl?url={q}&n={item['index']}&exp={exp}&sig={security.sign(item['url'], item['index'], exp)}"
    stream = None
    if kind in ("video", "audio"):
        stream = f"{BASE_URL}/play?url={q}&n={item['index']}&exp={exp}&sig={security.sign(item['url'], item['index'], exp)}"
    return (stream, dl)


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def render_page(items: list[dict], page: int) -> str:
    start = (page - 1) * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    blocks = []
    for i, item in enumerate(items[start:end], start=start + 1):
        name = item["filename"]
        if len(name) > NAME_MAX:
            name = name[:NAME_MAX] + "\u2026"
        stream, dl = file_links(item)
        line = (
            f"<b>{i}.</b> {ICONS.get(item['kind'], '\U0001F4C4')} Title  : <b>{_esc(name)}</b>\n"
            f"\u3000\u3000\U0001F4BE Size  : {_esc(item['size'])}\n"
        )
        if stream:
            line += f"\u3000\u3000<a href=\"{_esc(stream)}\">\u25B6\uFE0F Stream</a>\n"
        if dl:
            line += f"\u3000\u3000<a href=\"{_esc(dl)}\">\u2B07\uFE0F Download</a>"
        blocks.append(line)
    body = "\n\n".join(blocks)
    return f"\u2705 <b>Berhasil di ekstrak</b>\n\n\U0001F4CA Total file: <b>{len(items)}</b>\n\n{body}"


def page_buttons(items: list[dict], page: int, view_token: str) -> list[list] | None:
    pages = max(1, -(-len(items) // ITEMS_PER_PAGE))
    if pages <= 1:
        return None
    nav: list = []
    if page > 1:
        nav.append(Button.inline("\u2B05\uFE0F Kembali", data=f"p:{view_token}:{page - 1}".encode()))
    nav.insert(len(nav) // 2, Button.inline(f"<{page}/{pages}>", data=f"p:{view_token}:{page}".encode()))
    if page < pages:
        nav.append(Button.inline("Lanjut \u27A1\uFE0F", data=f"p:{view_token}:{page + 1}".encode()))
    return [nav]


def _new_view_token() -> str:
    slug = hashlib.sha1(os.urandom(16)).hexdigest()[:10]
    view_map[slug] = None
    return slug


def _load_session() -> str | None:
    s = os.environ.get("TELETHON_SESSION", "").strip()
    if s:
        return s
    try:
        if os.path.exists(SESSION_FILE):
            s = open(SESSION_FILE, encoding="utf-8").read().strip()
            if s:
                return s
    except Exception:
        pass
    return None


def _save_session(sess: str) -> None:
    try:
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        with open(SESSION_FILE, "w", encoding="utf-8") as fh:
            fh.write(sess)
    except Exception as e:
        print(f"[BOT] Gagal simpan session: {e}", file=sys.stderr)


async def run_bot() -> None:
    client = TelegramClient(
        StringSession(_load_session()), API_ID, API_HASH,
        connection_retries=10,
        flood_sleep_threshold=60,
    )

    @client.on(events.NewMessage)
    async def on_message(event):
        text = (event.raw_text or "").strip()
        if not text:
            return

        peer = event.chat_id if hasattr(event, "chat_id") else event.sender_id

        if text.startswith("/start"):
            payload = text.split()[1] if len(text.split()) > 1 else None
            if payload and payload.startswith("dl_"):
                await event.reply(
                    "\u26A0\uFE0F Link unduh lama sudah tidak berlaku. Kirim ulang link TeraBox-nya."
                )
            else:
                await event.reply(START_TEXT, parse_mode="html")
            return
        if text.startswith("/help"):
            await event.reply(START_TEXT, parse_mode="html")
            return
        if text.startswith("/id"):
            await event.reply(f"ID: <code>{event.sender_id}</code>", parse_mode="html")
            return

        url = extract_share_url(text)
        if not url:
            return

        # Limit ekstrak: non-admin 1x per 3 menit (dicatat saat permintaan, anti-spam).
        left = _extract_cooldown_left(event.sender_id)
        if left > 0:
            mm, ss = divmod(left, 60)
            sisa = f"{mm} menit {ss} detik" if mm else f"{ss} detik"
            await event.reply(
                f"\u23F3 Kamu sudah ekstrak link baru-baru ini. Coba lagi dalam {sisa}."
            )
            return
        if not is_owner(event.sender_id):
            _extract_last[event.sender_id] = time.time()

        wait = await event.reply("\u23F3 Me-resolve link TeraBox\u2026")
        try:
            data = await asyncio.to_thread(_resolve_cached, url)
        except Exception as e:
            await wait.edit(f"\u274C Gagal resolve link: {str(e)[:300]}")
            return

        items = build_pending_files(data)
        view_token = _new_view_token()
        view_map[view_token] = items
        await wait.edit(render_page(items, 1), parse_mode="html", buttons=page_buttons(items, 1, view_token))

    @client.on(events.CallbackQuery)
    async def on_callback(event):
        try:
            raw = bytes(event.data or b"").decode("utf-8", "replace")
        except Exception:
            raw = ""
        if not raw.startswith("p:"):
            await event.answer(alert=True, message="\u26A0\uFE0F Tombol tidak dikenal.")
            return
        _, view_token, page_raw = raw.split(":", 2)
        items = view_map.get(view_token)
        if items is None:
            await event.answer(alert=True, message="\u26A0\uFE0F Sesi kedaluwarsa, kirim ulang link-nya.")
            return
        if not page_raw.isdigit():
            return
        page = int(page_raw)
        pages_max = max(1, -(-len(items) // ITEMS_PER_PAGE))
        if page < 1 or page > pages_max:
            await event.answer(alert=True, message="\u26A0\uFE0F Halaman tidak valid.")
            return
        await event.edit(render_page(items, page), parse_mode="html", buttons=page_buttons(items, page, view_token))
        await event.answer(message="")

    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    saved = client.session.save()
    if saved and not _load_session():
        _save_session(saved)
    print(f"[BOT] Telegram bot aktif (MTProto) sebagai @{me.username} (id: {me.id})", flush=True)
    await client.run_until_disconnected()


async def keep_alive() -> None:
    await asyncio.Event().wait()


async def main() -> None:
    if not (API_ID and API_HASH and BOT_TOKEN):
        print(
            "[BOT] API_ID/API_HASH/BOT_TOKEN belum diisi. Bot dimatikan, cek variabel Railway.",
            file=sys.stderr,
            flush=True,
        )
        await keep_alive()
        return
    try:
        await run_bot()
    except Exception as e:
        print(f"[BOT] FATAL: {e!r}", file=sys.stderr, flush=True)
        traceback.print_exc()
        await keep_alive()


if __name__ == "__main__":
    asyncio.run(main())