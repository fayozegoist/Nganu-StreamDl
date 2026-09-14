#!/usr/bin/env python3
"""Telethon bot (MTProto polling) — port fitur penuh CF Worker bypass-bot.

Platform: UC Share, Videy, TeraBox, Subtitle. Tanpa webhook.
State disimpan di SQLite (db.py) agar survive restart multi-proses.
"""

import asyncio
import os
import re
import sys
import time
import traceback
import urllib.parse

import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.errors import UserNotParticipantError

import app as backend
import db
import slicedrive
import subsrt
import ucshare

API_ID = int(os.environ.get("API_ID", "") or 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID") or os.environ.get("OWNER_IDS") or "0")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "") or 0)
CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "") or "https://t.me/MediafairyCh"


def _detect_base_url() -> str:
    b = (os.environ.get("BASE_URL") or "").rstrip("/")
    if b:
        return b
    domain = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    if domain:
        return f"https://{domain}"
    return ""


BASE_URL = _detect_base_url()

SESS_FILE = os.environ.get("TELETHON_SESSION_FILE", "/app/data/session.txt")
SESS_STRING = os.environ.get("TELETHON_SESSION", "").strip()

ITEMS_PER_PAGE = 7
NAME_MAX = 60
INPUT_TTL = 5 * 60 * 1000
PAGE_TTL = 10 * 60 * 1000
TERA_COOLDOWN_MS = 60 * 1000

ICONS = {"video": "\U0001F3AC", "audio": "\U0001F3B5", "photo": "\U0001F5BC\uFE0F", "file": "\U0001F4C4"}
PLATFORM_LABEL = {
    "ucshare": "\u2601\uFE0F UC Share",
    "slicedrive": "\U0001F3AC Videy",
    "terabox": "\U0001F4E6 TeraBox",
    "subtitle": "\U0001F4DD Subtitle",
}

START_TEXT = (
    "<b>Selamat datang!</b> \U0001F44B\n\n"
    "Gunakan bot ini untuk download video dan subtitle.\n\n"
    "Ketik <b>/menu</b> untuk melihat menu fitur."
)
JOIN_TEXT = "<b>\u26A0\uFE0F Join channel dulu:</b>"


def esc(s) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def fmt_size(size) -> str:
    if size is None or size == 0:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(size)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.0f} {units[i]}"


def _stripped_title(title: str, maxlen: int = NAME_MAX) -> str:
    title = re.sub(r"\s+", " ", str(title or "").strip())
    return title if len(title) <= maxlen else title[:maxlen] + "\u2026"


def tera_cooldown_remaining(chat: int) -> int | None:
    last = db.kv_get(f"cd:tb:{chat}")
    if isinstance(last, (int, float)):
        elapsed = int(time.time() * 1000) - int(last)
        if elapsed < TERA_COOLDOWN_MS:
            return max(1, (TERA_COOLDOWN_MS - elapsed) // 1000)
    return None


def tera_cooldown_set(chat: int) -> None:
    db.kv_set(f"cd:tb:{chat}", int(time.time() * 1000), ttl=TERA_COOLDOWN_MS)


_STYLES = {"primary", "success", "danger"}


def _b(text: str, **kw) -> dict:
    b = {"text": text}
    if kw.get("url"):
        b["url"] = kw["url"]
    else:
        b["callback_data"] = kw.get("data") or "noop"
    if kw.get("style") in _STYLES:
        b["style"] = kw["style"]
    return b


def menu_keyboard(is_admin: bool) -> list:
    kb = [
        [_b("\u2601\uFE0F UC Share", data="menu:ucshare", style="success"),
         _b("\U0001F3AC Videy", data="menu:slicedrive", style="primary")],
        [_b("\U0001F4E6 TeraBox", data="menu:terabox", style="success"),
         _b("\U0001F4DD Subtitle", data="menu:subtitle", style="primary")],
    ]
    if is_admin:
        kb.append([_b("\u2699\uFE0F Admin", data="menu:admin", style="danger")])
    return kb


def join_keyboard() -> list:
    return [
        [_b("\U0001F517 Join Channel", url=CHANNEL_LINK, style="primary"),
         _b("\U0001F6E1\uFE0F Verifikasi", data="menu:verify", style="success")],
    ]


def cancel_keyboard() -> list:
    return [[_b("\u274C Batal", data="cancel", style="danger")]]


def pager(platform: str, page: int, total: int) -> list:
    pages = max(1, -(-total // ITEMS_PER_PAGE))
    row = []
    if page > 1:
        row.append(_b("\u2B05 Prev", data=f"page:{platform}:{page - 1}", style="primary"))
    row.append(_b(f"{page}/{pages}", data="noop"))
    if page < pages:
        row.append(_b("Next \u27A1", data=f"page:{platform}:{page + 1}", style="success"))
    return [row] if len(row) > 1 else []


# ------------------- Telegram HTTP (Bot API) transport -------------------
# Kirim/edit pesan via HTTP Bot API agar InlineKeyboardButton.style
# (primary=blue, success=green, danger=red) dipakai — MTProto/Telethon
# tidak mendukung field style. Update tetap dibaca via Telethon.

TG_URL = "https://api.telegram.org"


def _tg(method: str, payload: dict) -> dict:
    try:
        r = requests.post(f"{TG_URL}/bot{BOT_TOKEN}/{method}", json=payload, timeout=30)
    except requests.RequestException as e:
        raise RuntimeError(f"[TG] {method} gagal: {e}") from e
    try:
        j = r.json()
    except ValueError as e:
        raise RuntimeError(f"[TG] {method}: respons bukan JSON ({r.status_code})") from e
    if not j.get("ok"):
        raise RuntimeError(f"[TG] {method}: {(j.get('description') or j.get('error_code') or 'error')}")
    return j["result"]


def _mk(text: str, buttons, parse_mode: str = "html", link_preview: bool = False) -> dict:
    payload: dict = {"text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if not link_preview:
        payload["disable_web_page_preview"] = True
    if buttons is not None:
        rows = []
        for row in buttons:
            rr = []
            for b in row:
                o = {"text": b["text"]}
                if b.get("url"):
                    o["url"] = b["url"]
                else:
                    o["callback_data"] = b.get("callback_data") or b.get("data") or "noop"
                if b.get("style") in _STYLES:
                    o["style"] = b["style"]
                rr.append(o)
            rows.append(rr)
        payload["reply_markup"] = {"inline_keyboard": rows}
    return payload


async def send_msg(chat_id: int, text: str, buttons=None, parse_mode: str = "html",
                   link_preview: bool = False) -> dict:
    payload = _mk(text, buttons, parse_mode, link_preview)
    payload["chat_id"] = chat_id
    return await asyncio.to_thread(_tg, "sendMessage", payload)


async def edit_msg(chat_id: int, message_id: int, text: str, buttons=None,
                   parse_mode: str = "html") -> None:
    payload = _mk(text, buttons, parse_mode, link_preview=True)
    payload["chat_id"] = chat_id
    payload["message_id"] = message_id
    await asyncio.to_thread(_tg, "editMessageText", payload)


async def del_msg(chat_id: int, message_id: int) -> None:
    await asyncio.to_thread(_tg, "deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def _tg_upload(method: str, payload: dict, files: dict) -> dict:
    try:
        r = requests.post(f"{TG_URL}/bot{BOT_TOKEN}/{method}", data=payload, files=files, timeout=120)
    except requests.RequestException as e:
        raise RuntimeError(f"[TG] {method} gagal: {e}") from e
    try:
        j = r.json()
    except ValueError as e:
        raise RuntimeError(f"[TG] {method}: respons bukan JSON ({r.status_code})") from e
    if not j.get("ok"):
        raise RuntimeError(f"[TG] {method}: {(j.get('description') or j.get('error_code') or 'error')}")
    return j["result"]


async def send_doc(chat_id: int, data: bytes, filename: str, caption: str | None = None) -> dict:
    payload = {"chat_id": chat_id}
    if caption:
        payload["caption"] = caption
    files = {"document": (filename, data)}
    return await asyncio.to_thread(_tg_upload, "sendDocument", payload, files)


async def answer_cb(query_id: int, text: str = None, alert: bool = False) -> None:
    p = {"callback_query_id": query_id}
    if text:
        p["text"] = text
    if alert:
        p["show_alert"] = True
    await asyncio.to_thread(_tg, "answerCallbackQuery", p)


async def is_member(client, user_id: int) -> bool:
    if not CHANNEL_ID:
        return True
    try:
        await client(GetParticipantRequest(CHANNEL_ID, user_id))
        return True
    except UserNotParticipantError:
        return False
    except Exception:
        return True


def _referer_for(platform: str, media_url: str) -> str | None:
    try:
        host = urllib.parse.urlparse(media_url or "").hostname or ""
    except Exception:
        return None
    if "aliyuncs.com" in host:
        return "https://uc-share.com/"
    if "overfetch." in host or "vidmonstr.com" in host:
        return "https://vidmonstr.com/"
    return None


def make_media_token(platform: str, item: dict) -> tuple[str, str]:
    """Buat token dl:{token} untuk stream/download. Return (stream_url, dl_url)."""
    raw = item.get("url") or ""
    stream = item.get("stream_url") or raw
    token = backend.create_media_token(
        raw, stream,
        fname=(item.get("title") or "video").replace("/", "_")[:80] + ".mp4",
        referer=_referer_for(platform, stream or raw),
    )
    return f"{BASE_URL}/m/{token}", f"{BASE_URL}/m/{token}?dl=1"


def render_media_page(items: list, platform: str, page: int) -> tuple[str, list | None]:
    n = len(items)
    start = (page - 1) * ITEMS_PER_PAGE
    chunk = items[start:start + ITEMS_PER_PAGE]
    blocks = []
    for idx, it in enumerate(chunk, start=start + 1):
        title = esc(_stripped_title(it.get("title") or "(tanpa judul)"))
        line = f"<b>{idx}.</b> \U0001F3AC Title  : <b>{title}</b>\n"
        size = it.get("size")
        if size is not None:
            sz = esc(fmt_size(size)) if isinstance(size, (int, float)) else esc(size)
            line += f"\u3000\u3000\U0001F4BE Size  : {sz}\n"
        else:
            line += f"\u3000\u3000\U0001F4BE Size  : -\n"
        stream = it.get("stream")
        download = it.get("download")
        if stream:
            line += f"\u3000\u3000\U000025B6\uFE0F <a href=\"{esc(stream)}\">Stream</a>\n"
        if download:
            line += f"\u3000\u3000\U00002B07\uFE0F <a href=\"{esc(download)}\">Download</a>"
        blocks.append(line)
    body = "\n\n".join(blocks)
    text = f"\u2705 <b>Berhasil di ekstrak</b>\n\n\U0001F4CA Total file: <b>{n}</b>\n\n{body}"
    buttons = pager(platform, page, n) or None
    return text, buttons


def render_subtitle_page(results: list, platform: str, page: int) -> tuple[str, list | None]:
    n = len(results)
    start = (page - 1) * ITEMS_PER_PAGE
    chunk = results[start:start + ITEMS_PER_PAGE]
    blocks = []
    rows: list[list[dict]] = []
    for idx, r in enumerate(chunk, start=start + 1):
        title = esc(_stripped_title(r.get("title") or "-"))
        line = f"<b>{idx}.</b> \U0001F4DD Judul: <b>{title}</b>\n"
        m = re.search(r"(\d+)", r.get("downloads") or "")
        dl_count = m.group(1) if m else "-"
        line += f"\u3000\u3000\U0001F4E5 Total Download: <b>{dl_count}</b>\n"
        if r.get("detailUrl"):
            rows.append([_b(f"\u2B07\uFE0F {idx}. Download", data=f"sub:{idx}", style="success")])
        blocks.append(line)
    body = "\n\n".join(blocks)
    text = f"\u2705 <b>Berhasil ditemukan</b>\n\n\U0001F4CA Total hasil: <b>{n}</b>\n\n{body}"
    pg = pager(platform, page, n)
    if pg:
        rows.extend(pg)
    return text, (rows or None)


# ------------------- subtitle scraper (subtitlecat.com) -------------------

_SUB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36")


def _strip_html(s: str = "") -> str:
    s = re.sub(r"<[^>]+>", " ", str(s))
    return re.sub(r"\s+", " ", s).strip()


def _unescape(s: str = "") -> str:
    s = (str(s).replace("&amp;", "&").replace("&quot;", '"')
         .replace("&#39;", "'").replace("&#x27;", "'")
         .replace("&lt;", "<").replace("&gt;", ">"))
    return re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)


def search_subtitle(q: str) -> list[dict]:
    r = requests.get(
        f"https://www.subtitlecat.com/index.php?search={urllib.parse.quote(q)}",
        headers={"User-Agent": _SUB_UA,
                 "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        timeout=20,
    )
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}")
    html = r.text
    table = (re.search(r'<table[^>]*class="[^"]*\bsub-table\b[^"]*"[\s\S]*?</table>', html, re.I)
             or ["", html])[0]
    results = []
    for row in re.findall(r"<tr\b[^>]*>([\s\S]*?)</tr>", table, re.I):
        tds = re.findall(r"<td\b[^>]*>([\s\S]*?)</td>", row, re.I)
        if not tds:
            continue
        a = re.search(r'<a\b[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>', tds[0], re.I)
        if not a:
            continue
        title = _unescape(_strip_html(a.group(2)))
        if not title:
            continue
        detail = a.group(1)
        rest = re.sub(r"<a\b[\s\S]*?</a>", "", tds[0])
        note = re.sub(r"^\(|\)$", "", _unescape(_strip_html(rest))).strip()
        size = _unescape(_strip_html((re.search(r"sub-table__metric-value[^>]*>([\s\S]*?)<", tds[2])
                                       or [None, tds[2]])[1])) if len(tds) > 2 else ""
        downloads = _unescape(_strip_html(tds[3])) if len(tds) > 3 else ""
        results.append({
            "title": title,
            "note": note,
            "detailUrl": urllib.parse.urljoin("https://www.subtitlecat.com", detail),
            "size": size,
            "downloads": downloads,
        })
    return results


# ------------------- admin helpers -------------------

def admin_panel(section: str) -> dict:
    if section in ("stats", "/stats"):
        st = db.get_stats()
        text = (f"<b>\U0001F4CA Statistik</b>\n\n"
                f"\U0001F464 Total user: <b>{st['total_users']}</b>\n"
                f"\U0001F4E5 Request hari ini: <b>{st['today_requests']}</b>\n"
                f"\U0001F4E5 Request 7 hari: <b>{st['week_requests']}</b>\n"
                f"\u2705 Success rate: <b>{st['success_rate']}%</b>")
    elif section in ("users", "/users"):
        users = db.list_users(30)
        if users:
            lines = []
            for u in users:
                name = u.get("first_name") or "-"
                uname = u.get("username") or "no-username"
                lines.append(f"\U0001F464 {esc(name)} (@{esc(uname)}) \u2014 {u.get('request_count')} req")
            text = f"<b>\U0001F4CB User aktif (top 30)</b>\n\n" + "\n".join(lines)
        else:
            text = "Belum ada user."
    elif section == "menu":
        text = ("<b>\U0001F44B Admin Panel</b>\n\n"
                "/stats \u2014 Statistik\n"
                "/users \u2014 Daftar user\n"
                "/broadcast &lt;text&gt; \u2014 Kirim pesan ke semua user")
    else:
        text = "<b>\U0001F44B Admin Panel</b>\n\n/stats \u2014 Statistik\n/users \u2014 Daftar user\n/broadcast &lt;text&gt; \u2014 Pesan massal"
    return {"text": text, "buttons": None}


def fmt_last_active(ms: int) -> str:
    if not ms:
        return "-"
    t = time.localtime(int(ms) / 1000 + 7 * 3600)
    return time.strftime("%d/%m %H:%M", t)


# ------------------- link processing -------------------

def _platform_items(platform: str, payload: str, base_url: str):
    """Resolve link sesuai platform; return list item dict {title,size,stream,download}."""
    if platform == "ucshare":
        data = ucshare.resolve(payload)
        items = []
        for m in data.get("medias", []):
            stream, dl = make_media_token("ucshare", m)
            items.append({"title": m.get("title") or "(tanpa judul)",
                          "size": m.get("size"), "stream": stream if m.get("url") else None,
                          "download": dl if m.get("url") else None})
        return items, data.get("url", payload)
    if platform == "slicedrive":
        data = slicedrive.resolve(payload)
        items = []
        for m in data.get("medias", []):
            stream, dl = make_media_token("slicedrive", m)
            items.append({"title": m.get("title") or "(tanpa judul)",
                          "size": m.get("size"), "stream": stream if m.get("url") else None,
                          "download": dl if m.get("url") else None})
        return items, data.get("url", payload)
    if platform == "terabox":
        data = backend.resolve_terabox(payload)
        items = []
        for f in data.get("files", []):
            p = backend._file_payload(payload, f.get("index", 1), base_url)
            items.append({"title": p.get("name") or f"TeraBox_{f.get('index')}",
                          "size": p.get("size_label"), "stream": p.get("stream"),
                          "download": p.get("download")})
        return items, data.get("url", payload)
    raise ValueError("Platform tidak valid")


def process_platform(platform: str, payload: str) -> dict:
    """Return {"items":[...], "src": str, "platform": str}."""
    items, src = _platform_items(platform, payload, BASE_URL)
    if not items:
        raise ValueError("Tidak ada file ditemukan.")
    return {"items": items, "src": src, "platform": platform}


async def handle_command(client, event, text: str):
    chat = event.chat_id
    uid = event.sender_id or 0
    lower = text.split()[0].lower()

    if lower == "/cancel":
        db.kv_del(f"input:{chat}")
        waiting = db.kv_get(f"res:{chat}")
        if waiting is not None:
            db.kv_del(f"res:{chat}")
            try:
                await del_msg(chat, int(waiting))
            except Exception:
                pass
        await send_msg(chat, "\u2705 Dibatalkan.")
        return

    if lower == "/start":
        db.kv_del(f"input:{chat}")
        db.upsert_user(uid, getattr(event.sender, "username", None), getattr(event.sender, "first_name", None))
        if await is_member(client, uid):
            await send_msg(chat, START_TEXT)
        else:
            await send_msg(chat, JOIN_TEXT, buttons=join_keyboard())
        return

    if lower == "/menu":
        db.kv_del(f"input:{chat}")
        if not await is_member(client, uid):
            await send_msg(chat, JOIN_TEXT, buttons=join_keyboard())
            return
        db.upsert_user(uid, getattr(event.sender, "username", None), getattr(event.sender, "first_name", None))
        await send_msg(chat, "<b>\U0001F4CB Menu</b>\n\nPilih fitur:", buttons=menu_keyboard(uid == OWNER_ID))
        return

    if lower in ("/admin", "/stats", "/users", "/broadcast"):
        if uid != OWNER_ID:
            await send_msg(chat, "\u274C Hanya owner.")
            return
        db.upsert_user(uid, getattr(event.sender, "username", None), getattr(event.sender, "first_name", None))
        if lower == "/broadcast":
            msg = text.split(" ", 1)[1].strip() if " " in text else ""
            await send_msg(chat, admin_panel("usage")["text"])
            if msg:
                sent = 0
                for target in db.all_user_ids():
                    try:
                        await send_msg(target, f"\U0001F4E2 <b>Broadcast</b>\n\n{esc(msg)}")
                        sent += 1
                    except Exception:
                        continue
                await send_msg(chat, f"\u2705 Broadcast terkirim ke {sent} user.")
            return
        await send_msg(chat, admin_panel(lower)["text"])
        return

    if lower == "/help":
        await send_msg(chat, START_TEXT)
        return

    if lower == "/id":
        await send_msg(chat, f"ID: <code>{uid}</code>")
        return

    # mode aktif? proses sebagai link
    mode = db.kv_get(f"input:{chat}", INPUT_TTL)
    if mode:
        db.kv_del(f"input:{chat}")
        if not await is_member(client, uid):
            await send_msg(chat, JOIN_TEXT, buttons=join_keyboard())
            return
        db.upsert_user(uid, getattr(event.sender, "username", None), getattr(event.sender, "first_name", None))
        await process_and_reply(event, mode, text)
        return

    await send_msg(chat, "Perintah tidak dikenal. Gunakan /menu untuk melihat fitur.")


async def process_and_reply(event, platform: str, payload: str):
    chat = event.chat_id
    uid = event.sender_id or 0
    if platform == "terabox" and uid != OWNER_ID:
        remain = tera_cooldown_remaining(chat)
        if remain:
            await send_msg(chat,
                           f"\u23F3 Jeda 60 detik... Tunggu {remain} detik lagi sebelum ekstrak link TeraBox.")
            return
    txt = f"\u23F3 Processing..."
    db.log_request(event.sender_id or 0, platform, platform, payload, "ok")
    processing = await send_msg(chat, txt)
    try:
        db.kv_set(f"res:{chat}", processing["message_id"], ttl=INPUT_TTL)
    except Exception:
        pass
    try:
        if platform == "subtitle":
            results = await asyncio.to_thread(search_subtitle, payload)
            if not results:
                raise ValueError("Subtitle tidak ditemukan.")
            key = f"page:{chat}:subtitle"
            db.kv_set(key, {"items": results}, ttl=PAGE_TTL)
            text, buttons = render_subtitle_page(results, "subtitle", 1)
        else:
            data = await asyncio.to_thread(process_platform, platform, payload)
            items = data["items"]
            key = f"page:{chat}:{platform}"
            db.kv_set(key, {"items": items}, ttl=PAGE_TTL)
            text, buttons = render_media_page(items, platform, 1)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await edit_msg(chat, processing["message_id"], f"\u274C {esc(str(e)[:300])}")
        db.log_request(event.sender_id or 0, platform, platform, payload, "error")
        return
    try:
        await edit_msg(chat, processing["message_id"], text, buttons)
        if platform == "terabox" and uid != OWNER_ID:
            tera_cooldown_set(chat)
    except Exception:
        pass


# ------------------- handlers -------------------

async def handle_callback(client, event):
    raw = (event.data or b"").decode("utf-8", "replace")
    chat = event.chat_id
    uid = event.sender_id or 0
    msg_id = event.message_id

    if not raw:
        await answer_cb(event.id)
        return
    if raw == "noop":
        await answer_cb(event.id)
        return
    if raw == "cancel":
        db.kv_del(f"input:{chat}")
        await answer_cb(event.id, "\u274C Dibatalkan")
        await edit_msg(chat, msg_id, "<b>\U0001F4CB Menu</b>\n\nPilih fitur:",
                       menu_keyboard(uid == OWNER_ID))
        return

    if raw.startswith("menu:"):
        mode = raw[5:]
        if mode == "verify":
            await answer_cb(event.id, "\u23F3 Memeriksa keanggotaan...")
            if await is_member(client, uid):
                await edit_msg(chat, msg_id, "<b>\u2705 Verifikasi berhasil!</b>\n\nAnda sudah join channel. Silakan pilih fitur:",
                               menu_keyboard(uid == OWNER_ID))
            else:
                await edit_msg(chat, msg_id, "<b>\u274C Anda belum join channel!</b>\n\nSilakan join dulu, lalu klik Verifikasi:",
                               join_keyboard())
            return
        if mode == "admin":
            if uid != OWNER_ID:
                await answer_cb(event.id, "Akses ditolak", alert=True)
                return
            res = admin_panel("menu")
            await edit_msg(chat, msg_id, res["text"], [])
            await answer_cb(event.id)
            return
        if mode not in ("ucshare", "slicedrive", "terabox", "subtitle"):
            await answer_cb(event.id, "Mode tidak valid", alert=True)
            return
        db.kv_set(f"input:{chat}", mode, ttl=INPUT_TTL)
        hints = {
            "ucshare": "Kirim link UC Share:",
            "slicedrive": "Kirim link Videy:",
            "terabox": "Kirim link TeraBox:",
            "subtitle": "Kirim judul subtitle:",
        }
        await answer_cb(event.id)
        label = PLATFORM_LABEL[mode]
        await edit_msg(chat, msg_id, f"\U0001F9ED <b>{label}</b>\n\n{hints[mode]}\n\n(Jika ingin mengganti mode, ketik /menu)",
                       cancel_keyboard())
        return

    if raw.startswith("page:"):
        parts = raw.split(":")
        platform = parts[1]
        page_raw = parts[2] if len(parts) > 2 else "1"
        page = int(page_raw) if page_raw.isdigit() else 1
        data = db.kv_get(f"page:{chat}:{platform}", PAGE_TTL)
        if not data:
            await answer_cb(event.id, "Sesi berakhir. Kirim ulang dari menu.", alert=True)
            return
        items = data["items"]
        pages = max(1, -(-len(items) // ITEMS_PER_PAGE))
        if page < 1 or page > pages:
            await answer_cb(event.id, "Halaman tidak valid.", alert=True)
            return
        if platform == "subtitle":
            text, buttons = render_subtitle_page(items, platform, page)
        else:
            text, buttons = render_media_page(items, platform, page)
        await edit_msg(chat, msg_id, text, buttons)
        await answer_cb(event.id)
        return

    if raw.startswith("sub:"):
        try:
            idx = int(raw.split(":", 1)[1])
        except (TypeError, ValueError, IndexError):
            await answer_cb(event.id, "Item tidak valid.", alert=True)
            return
        data = db.kv_get(f"page:{chat}:subtitle", PAGE_TTL)
        if not data:
            await answer_cb(event.id, "Sesi berakhir. Cari ulang dari menu.", alert=True)
            return
        items = data["items"]
        if idx < 1 or idx > len(items):
            await answer_cb(event.id, "Item tidak valid.", alert=True)
            return
        detail = items[idx - 1].get("detailUrl")
        if not detail:
            await answer_cb(event.id, "Subtitel tanpa file.", alert=True)
            return
        await answer_cb(event.id, "\u23F3 Mengambil subtitle...")
        try:
            srt, fname = await asyncio.to_thread(subsrt.fetch_subtitle_srt, detail)
        except Exception as e:
            print(f"[BOT] sub fetch error: {e!r}", file=sys.stderr)
            await send_msg(chat, f"\u274C Gagal ambil subtitle: {esc(str(e))[:200]}")
            return
        try:
            await send_doc(chat, srt, fname, caption=items[idx - 1].get("title") or "")
        except Exception as e:
            print(f"[BOT] sub send error: {e!r}", file=sys.stderr)
            await send_msg(chat, f"\u274C Gagal kirim subtitle: {esc(str(e))[:200]}")
        return


def _load_session() -> StringSession | None:
    if SESS_STRING:
        return StringSession(SESS_STRING)
    if os.path.exists(SESS_FILE):
        try:
            return StringSession(open(SESS_FILE, encoding="utf-8").read().strip())
        except Exception:
            pass
    return None


def _save_session(client) -> None:
    try:
        s = client.session.save()
        if s:
            os.makedirs(os.path.dirname(SESS_FILE), exist_ok=True)
            with open(SESS_FILE, "w", encoding="utf-8") as fh:
                fh.write(s)
    except Exception as e:
        print(f"[BOT] Gagal simpan session: {e}", file=sys.stderr)


async def main() -> None:
    if not (API_ID and API_HASH and BOT_TOKEN):
        print("[BOT] API_ID/API_HASH/BOT_TOKEN belum diisi. Bot dimatikan.", file=sys.stderr)
        await asyncio.Event().wait()
        return
    if not BASE_URL:
        print("[BOT] BASE_URL belum diisi.", file=sys.stderr)

    client = TelegramClient(_load_session() or "bot_session", API_ID, API_HASH,
                            connection_retries=10, flood_sleep_threshold=60)

    @client.on(events.NewMessage)
    async def on_message(event):
        text = (event.raw_text or "").strip()
        if not text or event.out:
            return
        try:
            await handle_command(client, event, text)
        except Exception as e:
            print(f"[BOT] on_message error: {e!r}", file=sys.stderr)
            traceback.print_exc()
            try:
                await send_msg(event.chat_id, f"\u274C {esc(str(e))[:200]}")
            except Exception:
                pass

    @client.on(events.CallbackQuery)
    async def on_callback(event):
        try:
            await handle_callback(client, event)
        except Exception as e:
            print(f"[BOT] on_callback error: {e!r}", file=sys.stderr)
            traceback.print_exc()
            try:
                await answer_cb(event.id, "Terjadi kesalahan.", alert=True)
            except Exception:
                pass

    try:
        await client.start(bot_token=BOT_TOKEN)
        me = await client.get_me()
        _save_session(client)
        print(f"[BOT] Telegram bot aktif (MTProto) sebagai @{me.username} (id: {me.id})", flush=True)
    except Exception as e:
        print(f"[BOT] start gagal: {e!r}", file=sys.stderr)
        traceback.print_exc()
        await asyncio.Event().wait()
        return

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())