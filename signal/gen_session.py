"""
One-time utility: generate a Telethon StringSession for GitHub Actions.

Usage:
    1. Get your TELEGRAM_API_ID and TELEGRAM_API_HASH from https://my.telegram.org/apps
    2. Set them as env vars (or paste when prompted):
         export TELEGRAM_API_ID=12345
         export TELEGRAM_API_HASH=abcdef...
    3. Run: python gen_session.py
    4. Enter your phone (international format, e.g. +6591234567) when prompted
    5. Enter the login code Telegram sends you
    6. Copy the printed string into GitHub secret TELEGRAM_SESSION

The session string authorizes the script to read messages from your account.
Keep it secret — treat it like a password. Anyone with this string + API_ID/HASH
can act as your Telegram user.

This file is .gitignored — do NOT commit any output from this script.
"""

import asyncio
import os
import sys
from getpass import getpass

from telethon import TelegramClient
from telethon.sessions import StringSession


async def run():
    api_id = os.environ.get("TELEGRAM_API_ID") or input("TELEGRAM_API_ID: ").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH") or getpass("TELEGRAM_API_HASH (hidden): ").strip()

    if not api_id or not api_hash:
        sys.exit("Need TELEGRAM_API_ID and TELEGRAM_API_HASH")

    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.start()
    try:
        session_str = client.session.save()
        print("\n" + "=" * 70)
        print("SUCCESS — copy the string below into GitHub secret TELEGRAM_SESSION:")
        print("=" * 70)
        print(session_str)
        print("=" * 70)
        print("Length:", len(session_str), "chars\n")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(run())
