"""Security: API key (sk-fairy-...) + HMAC signed streaming links (exp/sig) + rate limit.

- Key dipakai hanya untuk resolve (/api/terabox): validasi constant-time, format ketat.
- Stream/download links memakai HMAC(sig+exp) sehingga aman di-share tanpa membocorkan key.
"""

import hashlib
import hmac
import os
import re
import threading
import time

_KEY_PATTERN = re.compile(r"^sk-fairy-[A-Za-z0-9]{16}$")
LINK_TTL = int(os.environ.get("LINK_TTL", str(8 * 60 * 60)))  # 8 jam default
RL_PER_MIN = int(os.environ.get("RL_PER_MIN", "3"))


def _keys() -> list[str]:
    return [k.strip() for k in os.environ.get("API_KEYS", "").split(",") if k.strip()]


def _secret() -> str:
    return os.environ.get("TOKEN_SECRET", "")


def has_keys() -> bool:
    return bool(_keys()) and bool(_secret())


def key_is_valid(candidate: str | None) -> bool:
    if not candidate or not _KEY_PATTERN.match(candidate):
        return False
    for k in _keys():
        if hmac.compare_digest(candidate, k):
            return True
    return False


def extract_key() -> str | None:
    from flask import request
    q = request.args.get("key")
    if q:
        return q.strip()
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


def _mac(msg: str) -> str:
    return hmac.new(_secret().encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]


def sign(url: str, n: int, exp: int) -> str:
    return _mac(f"{url}|{n}|{exp}")


def sign_value(value: str, exp: int) -> str:
    return _mac(f"{value}|{exp}")


def verify(url: str, n: int, exp: int, sig: str) -> bool:
    if not sig or not _secret():
        return False
    if exp <= int(time.time()):
        return False
    return hmac.compare_digest(sign(url, n, exp), sig)


def verify_value(value: str, exp: int, sig: str) -> bool:
    if not sig or not _secret():
        return False
    if exp <= int(time.time()):
        return False
    return hmac.compare_digest(sign_value(value, exp), sig)


def exp_later() -> int:
    return int(time.time()) + LINK_TTL


# ---- rate limiting (per IP) ----
_rl: dict[str, list[float]] = {}
_rl_lock = threading.Lock()


def rate_limit(ip: str) -> int:
    """Return 0 jika diizinkan; selain itu detik tersisa sebelum boleh request lagi."""
    now = time.time()
    with _rl_lock:
        window = [t for t in _rl.get(ip, []) if now - t < 60]
        if len(window) < RL_PER_MIN:
            window.append(now)
            _rl[ip] = window
            return 0
        _rl[ip] = window
        return int(60 - (now - window[0]))