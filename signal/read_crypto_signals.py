"""
Crypto Signal Digest (Phase 2)
==============================

Architecture: Grok-as-fetcher + Claude-as-synthesizer

  Stage 1 (Grok-4.3-fast + x_search): pure post extraction from 100 curated X
          handles, batched at <=10 per call. Returns raw post lines.
  Stage 2 (Claude Opus 4.7): reasoning, sub-list grouping, 5-section digest.
  Stage 3 (Telegram bot API): chunked post + pin first message.

Cost ~$3-4/run vs ~$13 if Grok did both stages.
"""

import csv
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import anthropic
from openai import OpenAI

# ------- stdout encoding fix (Phase 1 lesson) -------
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ------- BOM cleaner (Phase 1 lesson #1) -------
def _clean(val: str) -> str:
    if val is None:
        return ""
    return val.strip().replace("\ufeff", "").replace(" ", "")


# ------- Env vars -------
GROK_API_KEY = _clean(os.environ.get("GROK_API_KEY", ""))
ANTHROPIC_API_KEY = _clean(os.environ.get("ANTHROPIC_API_KEY", ""))
TELEGRAM_BOT_TOKEN = _clean(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
TELEGRAM_CHAT_ID = _clean(os.environ.get("TELEGRAM_CHAT_ID", ""))
SKIP_DUPLICATE_CHECK = os.environ.get("SKIP_DUPLICATE_CHECK", "").strip() in ("1", "true", "True")

for name, val in [
    ("GROK_API_KEY", GROK_API_KEY),
    ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
    ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
    ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
]:
    if not val:
        sys.exit(f"ERROR: {name} env var missing")

# ------- Constants -------
GROK_BASE_URL = "https://api.x.ai/v1"
GROK_MODEL = os.environ.get("GROK_MODEL", "grok-4.3-fast").strip() or "grok-4.3-fast"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-7").strip() or "claude-opus-4-7"
HANDLES_CSV = Path(__file__).parent / "handles.csv"
HANDLE_BATCH_SIZE = 10  # xAI x_search constraint: max 10 allowed_x_handles
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


# ------- Time window (Phase 1 pattern: clock-based 6-hour windows) -------
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
    """Returns list of (handle, sub_list) preserving CSV order."""
    out = []
    with HANDLES_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            handle = row["handle"].strip().lstrip("@")
            sub = row["sub_list"].strip()
            out.append((handle, sub))
    return out


def batch(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


# ============================================================
# STAGE 1 — Grok fetches raw posts (no analysis)
# ============================================================
grok_client = OpenAI(api_key=GROK_API_KEY, base_url=GROK_BASE_URL)

FETCH_PROMPT = """Use the x_search tool now to retrieve EVERY post from these {n} X handles between {from_dt} UTC and {to_dt} UTC.

Handles: {handles}

OUTPUT FORMAT — one line per post, no commentary, no analysis, no summarization:

@handle | YYYY-MM-DD HH:MM | <full post text on one line, preserving links/tickers/mentions; truncate after 500 chars with ...>

Rules:
- One line per post. If a post wraps, fit it on one line (replace newlines with spaces).
- Include the post URL at the end if available: ` [https://x.com/...]`
- Skip pure retweets without comment. Include quote-tweets and replies if substantive.
- If a handle posted nothing in the window, omit silently.
- If NO posts at all across all handles, output exactly: "NO_POSTS_IN_WINDOW"

Do NOT add headers, bullets, sections, or any text outside the per-post lines."""


def grok_fetch_posts(handles_batch: list[str], from_dt: datetime, to_dt: datetime) -> str:
    resp = grok_client.responses.create(
        model=GROK_MODEL,
        input=[
            {
                "role": "user",
                "content": FETCH_PROMPT.format(
                    n=len(handles_batch),
                    handles=", ".join(f"@{h}" for h in handles_batch),
                    from_dt=from_dt.isoformat(timespec="minutes"),
                    to_dt=to_dt.isoformat(timespec="minutes"),
                ),
            }
        ],
        tools=[
            {
                "type": "x_search",
                "allowed_x_handles": handles_batch,
                "from_date": from_dt.strftime("%Y-%m-%d"),
                "to_date": to_dt.strftime("%Y-%m-%d"),
            }
        ],
    )
    text = getattr(resp, "output_text", None)
    if text:
        return text
    chunks = []
    for item in getattr(resp, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", []) or []:
            t = getattr(part, "text", None)
            if t:
                chunks.append(t)
    return "\n".join(chunks)


def collect_all_posts(handles: list[tuple[str, str]], from_dt: datetime, to_dt: datetime) -> str:
    """Run Grok x_search across all handles in batches of 10. Return concatenated post lines."""
    all_lines = []
    just_handles = [h for h, _ in handles]
    batches = list(batch(just_handles, HANDLE_BATCH_SIZE))
    for i, hb in enumerate(batches, 1):
        try:
            result = grok_fetch_posts(hb, from_dt, to_dt)
            n_lines = sum(1 for ln in result.splitlines() if ln.strip().startswith("@"))
            print(f"  [Grok batch {i}/{len(batches)}] {len(hb)} handles → {n_lines} post lines")
            if "NO_POSTS_IN_WINDOW" not in result:
                all_lines.append(result.strip())
        except Exception as e:
            print(f"  [Grok batch {i}/{len(batches)}] FAILED: {e}")
            all_lines.append(f"# Batch {i} fetch error: {e}")
        time.sleep(1)
    return "\n".join(all_lines)


# ============================================================
# STAGE 2 — Claude synthesizes
# ============================================================
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SUB_LIST_ORDER = ["DeFi", "Trading", "Macro", "Other", "Infra/Builders"]

SYNTHESIS_PROMPT = """You are synthesizing a Crypto Signal Digest from raw X posts pulled from 100 high-signal handles.

Window: {label}

HANDLE → SUB-LIST MAPPING (for grouping):
{handle_mapping}

RAW POSTS (one per line, format: @handle | timestamp | text):
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
A numbered list of every handle you cited above, with a 1-line reason each.

READER PROFILE: experienced macro/equity fundamental investor (yield curves, P/E, DCF, credit spreads, options Greeks, duration) with NO crypto background. When introducing crypto-native jargon (funding rate, basis trade, LST, restaking, MEV, etc.), include a parenthetical TradFi analogy on first use — e.g. "funding rate (~ overnight repo rate for perpetual futures)".

If raw posts are mostly empty or noise, say "Low signal in this window" instead of inventing content."""


def synthesize_with_claude(raw_posts: str, label: str, handles: list[tuple[str, str]]) -> str:
    handle_mapping = ", ".join(f"@{h}={sl}" for h, sl in handles)
    prompt = SYNTHESIS_PROMPT.format(
        label=label,
        handle_mapping=handle_mapping,
        raw_posts=raw_posts,
    )
    msg = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=6000,
        thinking={"type": "enabled", "budget_tokens": 4000},
        messages=[{"role": "user", "content": prompt}],
    )
    # Extract text content from the response
    text_parts = []
    for block in msg.content:
        if block.type == "text":
            text_parts.append(block.text)
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


# ------- Duplicate guard (file-based, bots can't read history) -------
LAST_RUN_FILE = Path(__file__).parent / ".last_run_marker"


def is_duplicate_run(window_label: str) -> bool:
    if SKIP_DUPLICATE_CHECK:
        return False
    if not LAST_RUN_FILE.exists():
        return False
    return LAST_RUN_FILE.read_text().strip() == window_label


def mark_run_complete(window_label: str):
    LAST_RUN_FILE.write_text(window_label)


# ------- Chunking for Telegram (Phase 1 lesson #7) -------
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
def main():
    from_dt, to_dt, label = time_window()
    print(f"Window: {label} ({from_dt.isoformat()} → {to_dt.isoformat()})")

    if is_duplicate_run(label):
        print(f"Duplicate run guard: window '{label}' already processed. Set SKIP_DUPLICATE_CHECK=1 to override.")
        return

    handles = load_handles()
    print(f"Loaded {len(handles)} handles")

    print("\n=== Stage 1: Grok fetches raw posts ===")
    raw_posts = collect_all_posts(handles, from_dt, to_dt)
    print(f"Total raw posts text: {len(raw_posts)} chars")

    if not raw_posts.strip() or len(raw_posts) < 100:
        digest = f"🪙 CRYPTO SIGNAL DIGEST — {label}\n\nLow signal in this window — no posts retrieved from the 100 tracked handles."
    else:
        print("\n=== Stage 2: Claude synthesizes ===")
        digest = synthesize_with_claude(raw_posts, label, handles)
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


if __name__ == "__main__":
    main()
