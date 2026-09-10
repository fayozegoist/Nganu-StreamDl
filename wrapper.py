#!/usr/bin/env python3
"""Wrapper pemula bot: pastikan traceback tercatat dan container tak ikut mati
kalau bot.py error (import/runtime). gunicorn tetap melayani /health."""

import asyncio
import sys
import traceback


async def _idle():
    await asyncio.Event().wait()


def main() -> None:
    try:
        import bot
    except Exception:
        print("[BOT] IMPORT GAGAL:", file=sys.stderr, flush=True)
        traceback.print_exc()
        asyncio.run(_idle())
        return
    try:
        asyncio.run(bot.main())
    except BaseException:
        print("[BOT] KRASH:", file=sys.stderr, flush=True)
        traceback.print_exc()
    asyncio.run(_idle())


if __name__ == "__main__":
    main()