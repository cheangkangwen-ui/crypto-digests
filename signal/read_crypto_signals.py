"""
Crypto Signal Digest — local Windows edition
============================================

Reads .env from script directory for secrets, x_cookies.json for X session.
Runs Playwright + Chromium, scrapes 100 curated X handles, synthesizes via
Claude, posts to Telegram supergroup via bot.

Scheduled by Windows Task Scheduler 4x daily (UTC 1/7/13/19).
"""

import asyncio
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import anthropic
from playwright.async_api import async_playwright

# ------- stdout encoding fix (Phase 1 lesson #4) -------
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ------- Paths -------
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"
COOKIES_FILE = SCRIPT_DIR / "x_cookies.json"
HANDLES_CSV = SCRIPT_DIR / "handles.csv"
LOG_DIR = SCRIPT_DIR / "logs"
LAST_RUN_FILE = SCRIPT_DIR / ".last_run_marker"


# ------- BOM cleaner (Phase 1 lesson #1: PowerShell injects UTF-16 BOM) -------
def _clean(val: str) -> str:
    if val is None:
        return ""
    return val.strip().replace("\ufeff", "").replace(" ", "")


# ------- Load .env -------
def load_env():
    if not ENV_FILE.exists():
        sys.exit(f"ERROR: {ENV_FILE} not found. Run install.ps1 first.")
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        # strip optional quotes
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        os.environ.setdefault(k.strip(), v)


load_env()

ANTHROPIC_API_KEY = _clean(os.environ.get("ANTHROPIC_API_KEY", ""))
TELEGRAM_BOT_TOKEN = _clean(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
TELEGRAM_CHAT_ID = _clean(os.environ.get("TELEGRAM_CHAT_ID", ""))
SKIP_DUPLICATE_CHECK = os.environ.get("SKIP_DUPLICATE_CHECK", "").strip() in ("1", "true", "True")
DEBUG_HANDLE_LIMIT = os.environ.get("DEBUG_HANDLE_LIMIT", "").strip()

for name, val in [
    ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
    ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
    ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
]:
    if not val:
        sys.exit(f"ERROR: {name} missing from {ENV_FILE}")

if not COOKIES_FILE.exists():
    sys.exit(f"ERROR: {COOKIES_FILE} not found. Export from Cookie-Editor extension.")

# ------- Constants -------
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-7").strip() or "claude-opus-4-7"
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
MAX_POSTS_PER_HANDLE = 10
SCRAPE_CONCURRENCY = 4  # higher on local since IP isn't flagged
HANDLE_TIMEOUT_MS = 20_000


# ------- Time window -------
def time_window():
    schedule_hours = [1, 7, 13, 19]  # UTC = SGT 9am, 3pm, 9pm, 3am
    now = datetime.now(timezone.utc)
    today_hours = [now.replace(hour=h, minute=0, second=0, microsecond=0) for h in schedule_hours]
    past = [h for h in today_hours if h <= now]
    if past:
        start = past[-1]
    else:
        start = (now - timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)
    sgt_start = start + timedelta(hours=8)
    sgt_end = now + timedelta(hours=8)
    label = f"{sgt_start.strftime('%H:%M')} - {sgt_end.strftime('%H:%M')} SGT"
    return start, now, label


# ------- Load handles -------
def load_handles() -> list[tuple[str, str]]:
    out = []
    with HANDLES_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            handle = row["handle"].strip().lstrip("@")
            sub = row["sub_list"].strip()
            out.append((handle, sub))
    return out


# ------- Convert Cookie-Editor JSON to Playwright format -------
def load_cookies_for_playwright() -> list[dict]:
    raw = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
    samesite_map = {
        "no_restriction": "None",
        "lax": "Lax",
        "strict": "Strict",
        None: "Lax",
        "unspecified": "Lax",
        "None": "None",
        "Lax": "Lax",
        "Strict": "Strict",
    }
    out = []
    for c in raw:
        # Support both Cookie-Editor format and Playwright format
        ss = c.get("sameSite")
        out.append(
            {
                "name": c["name"],
                "value": c["value"],
                "domain": c["domain"],
                "path": c.get("path", "/"),
                "expires": float(c.get("expirationDate", c.get("expires", -1))),
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", False)),
                "sameSite": samesite_map.get(ss, "Lax"),
            }
        )
    return out


# ============================================================
# STAGE 1 — Playwright scrapes X
# ============================================================
async def scrape_handle(context, handle: str, since_dt: datetime, debug: bool = False) -> list[dict]:
    page = await context.new_page()
    posts = []
    try:
        url = f"https://x.com/{handle}"
        await page.goto(url, timeout=HANDLE_TIMEOUT_MS, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector("article[data-testid='tweet']", timeout=10000)
        except Exception:
            if debug:
                title = await page.title()
                body_text = await page.evaluate("() => document.body.innerText.slice(0, 600)")
                print(f"  DEBUG @{handle}: no tweet selector. title={title!r}")
                print(f"  DEBUG body preview: {body_text!r}")
            return []

        for _ in range(3):
            await page.mouse.wheel(0, 3000)
            await asyncio.sleep(0.8)

        articles = await page.query_selector_all("article[data-testid='tweet']")
        for art in articles[:MAX_POSTS_PER_HANDLE]:
            try:
                time_el = await art.query_selector("time")
                ts = await time_el.get_attribute("datetime") if time_el else None
                if not ts:
                    continue
                post_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if post_dt < since_dt:
                    continue
                text_el = await art.query_selector("div[data-testid='tweetText']")
                text = (await text_el.inner_text()).replace("\n", " ").strip() if text_el else ""
                link_el = await art.query_selector("a[href*='/status/']")
                href = await link_el.get_attribute("href") if link_el else ""
                url_ = f"https://x.com{href}" if href and href.startswith("/") else href
                if text:
                    posts.append(
                        {
                            "handle": handle,
                            "timestamp": post_dt.isoformat(timespec="minutes"),
                            "text": text[:600],
                            "url": url_,
                        }
                    )
            except Exception:
                continue
    except Exception as e:
        print(f"  scrape error @{handle}: {type(e).__name__}: {str(e)[:120]}")
    finally:
        await page.close()
    return posts


async def scrape_all_handles(handles: list[tuple[str, str]], since_dt: datetime) -> list[dict]:
    cookies = load_cookies_for_playwright()
    all_posts: list[dict] = []
    sem = asyncio.Semaphore(SCRAPE_CONCURRENCY)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        await context.add_cookies(cookies)

        async def worker(handle: str, sub: str, debug: bool = False):
            async with sem:
                posts = await scrape_handle(context, handle, since_dt, debug=debug)
                for p_ in posts:
                    p_["sub_list"] = sub
                if posts:
                    print(f"  @{handle} [{sub}] → {len(posts)} posts")
                return posts

        results = await asyncio.gather(*(worker(h, s, debug=(i < 3)) for i, (h, s) in enumerate(handles)))
        for r in results:
            all_posts.extend(r)

        await browser.close()
    return all_posts


# ============================================================
# STAGE 2 — Claude synthesizes
# ============================================================
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYNTHESIS_PROMPT = """You are synthesizing a Crypto Signal Digest from raw X posts pulled from 100 high-signal handles.

Window: {label}
Total posts in window: {n_posts}

RAW POSTS (JSON lines, one per post):
---
{raw_posts}
---

Synthesize into this EXACT structure. Be concrete, cite handles, no fluff.

🪙 CRYPTO SIGNAL DIGEST — {label}

## 1. TOP STORIES
The 3-5 most-discussed narratives across all sub-lists. Each:
- **One-line headline**
- 1-2 sentence why-it-matters
- Cite handles (@x, @y, @z)

## 2. MARKET SNAPSHOT
BTC / ETH / notable alts: price action observations from handles + dominant sentiment lean. Pull specific levels mentioned.

## 3. TRADE IDEAS
Specific actionable setups mentioned by handles. Each: $TICKER, direction (long/short), entry/target/stop where mentioned, conviction, source handle. If no concrete setups: "No concrete setups posted this window" — don't manufacture.

## 4. NARRATIVE SUSTAINABILITY
Which themes are gaining traction (multi-handle convergence) vs fading (declining mentions, contradicting calls). Brief.

## 5. BY SUB-LIST
- **DeFi:** 1-2 most important signals
- **Trading:** 1-2 most important signals
- **Macro:** 1-2 most important signals (skip if commoditized)
- **Other:** 1-2 most important signals
- **Infra/Builders:** 1-2 most important signals

End with:
---SOURCES---
A numbered list of every handle you cited above, with a 1-line reason each. Include post URLs where available.

READER PROFILE: experienced macro/equity fundamental investor (yield curves, P/E, DCF, credit spreads, options Greeks, duration) with NO crypto background. When introducing crypto-native jargon (funding rate, basis trade, LST, restaking, MEV, etc.), include a parenthetical TradFi analogy on first use — e.g. "funding rate (~ overnight repo rate for perpetual futures)".

If raw posts are mostly empty or noise, say "Low signal in this window" instead of inventing content."""


def synthesize_with_claude(posts: list[dict], label: str) -> str:
    if not posts:
        return f"🪙 CRYPTO SIGNAL DIGEST — {label}\n\nLow signal in this window — no posts retrieved from the 100 tracked handles."

    posts_sorted = sorted(posts, key=lambda x: (x.get("sub_list", ""), x["handle"]))
    raw_posts = "\n".join(json.dumps(p, ensure_ascii=False) for p in posts_sorted)

    prompt = SYNTHESIS_PROMPT.format(label=label, n_posts=len(posts), raw_posts=raw_posts)
    msg = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=6000,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    )
    text_parts = [b.text for b in msg.content if b.type == "text"]
    return "\n".join(text_parts)


# ============================================================
# STAGE 3 — Telegram delivery
# ============================================================
def tg_send_message(text: str, disable_notification: bool = False) -> dict:
    r = requests.post(
        f"{TG_API}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
            "disable_notification": disable_notification,
        },
        timeout=30,
    )
    if not r.ok:
        print(f"TG send error {r.status_code}: {r.text[:500]}")
    r.raise_for_status()
    return r.json()


def tg_pin_message(message_id: int):
    r = requests.post(
        f"{TG_API}/pinChatMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id, "disable_notification": True},
        timeout=30,
    )
    if not r.ok:
        print(f"WARN: pin failed: {r.text[:300]}")


# ------- Duplicate guard -------
def is_duplicate_run(window_label: str) -> bool:
    if SKIP_DUPLICATE_CHECK:
        return False
    if not LAST_RUN_FILE.exists():
        return False
    return LAST_RUN_FILE.read_text().strip() == window_label


def mark_run_complete(window_label: str):
    LAST_RUN_FILE.write_text(window_label)


# ------- Chunking -------
def chunk_for_tg(text: str, max_chars: int = 4000) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks = []
    cur = ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > max_chars:
            chunks.append(cur)
            cur = line
        else:
            cur += line
    if cur:
        chunks.append(cur)
    if len(chunks) > 1:
        chunks = [f"[{i + 1}/{len(chunks)}]\n{c}" for i, c in enumerate(chunks)]
    return chunks


# ============================================================
# MAIN
# ============================================================
async def amain():
    from_dt, to_dt, label = time_window()
    print(f"Window: {label} ({from_dt.isoformat()} → {to_dt.isoformat()})")

    if is_duplicate_run(label):
        print(f"Duplicate run guard: window '{label}' already processed. Set SKIP_DUPLICATE_CHECK=1 to override.")
        return

    handles = load_handles()
    if DEBUG_HANDLE_LIMIT.isdigit():
        handles = handles[: int(DEBUG_HANDLE_LIMIT)]
        print(f"DEBUG_HANDLE_LIMIT={DEBUG_HANDLE_LIMIT} — only scraping first {len(handles)} handles")
    print(f"Loaded {len(handles)} handles\n")

    print("=== Stage 1: Playwright scrapes X ===")
    t0 = time.time()
    posts = await scrape_all_handles(handles, from_dt)
    print(f"\nScraped {len(posts)} posts across {len(handles)} handles in {time.time() - t0:.1f}s")

    if len(posts) == 0:
        try:
            tg_send_message(
                f"⚠️ Crypto Signal Digest — window {label}: scraped 0 posts. X cookies may be expired. Re-export via Cookie-Editor → save to x_cookies.json."
            )
        except Exception as e:
            print(f"Failed to send health alert: {e}")

    print("\n=== Stage 2: Claude synthesizes ===")
    digest = synthesize_with_claude(posts, label)
    print(f"Digest: {len(digest)} chars")

    print("\n=== Stage 3: post to Telegram ===")
    if "---SOURCES---" in digest:
        body, sources = digest.split("---SOURCES---", 1)
    else:
        body, sources = digest, ""

    first_msg_id = None
    for chunk in chunk_for_tg(body.strip()):
        resp = tg_send_message(chunk)
        if first_msg_id is None:
            first_msg_id = resp["result"]["message_id"]
        time.sleep(0.5)

    if sources.strip():
        tg_send_message("🔗 SOURCES\n" + sources.strip(), disable_notification=True)

    if first_msg_id:
        tg_pin_message(first_msg_id)

    mark_run_complete(label)
    print(f"\nDone. Pinned message {first_msg_id}.")


def main():
    LOG_DIR.mkdir(exist_ok=True)
    asyncio.run(amain())


if __name__ == "__main__":
    main()
