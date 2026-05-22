"""
Crypto Signal Digest — reads posts from a curated set of high-signal X handles
via Grok's Live Search (x_search), synthesizes a 5-section digest, posts to
Telegram via a bot.

Phase 2 of the crypto digest project. Mirrors Phase 1's patterns:
- BOM cleaning on all env vars
- Chunking at 4000 chars for Telegram
- Duplicate guard with SKIP_DUPLICATE_CHECK escape hatch
- 4x daily clock-based 6-hour windows: UTC 1,7,13,19 = MYT/SGT 9am, 3pm, 9pm, 3am
"""

import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
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
TELEGRAM_BOT_TOKEN = _clean(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
TELEGRAM_CHAT_ID = _clean(os.environ.get("TELEGRAM_CHAT_ID", ""))
SKIP_DUPLICATE_CHECK = os.environ.get("SKIP_DUPLICATE_CHECK", "").strip() in ("1", "true", "True")

if not GROK_API_KEY:
    sys.exit("ERROR: GROK_API_KEY env var missing")
if not TELEGRAM_BOT_TOKEN:
    sys.exit("ERROR: TELEGRAM_BOT_TOKEN env var missing")
if not TELEGRAM_CHAT_ID:
    sys.exit("ERROR: TELEGRAM_CHAT_ID env var missing")

# ------- Constants -------
GROK_BASE_URL = "https://api.x.ai/v1"
GROK_MODEL = os.environ.get("GROK_MODEL", "grok-4").strip() or "grok-4"
HANDLES_CSV = Path(__file__).parent / "handles.csv"
SUB_LISTS = ["DeFi", "Trading", "Macro", "Other", "Infra/Builders"]
HANDLE_BATCH_SIZE = 10  # xAI Live Search constraint
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
DIGEST_HEADER = "🪙 CRYPTO SIGNAL DIGEST"


# ------- Time window (Phase 1 pattern: clock-based 6-hour windows) -------
def time_window():
    schedule_hours = [1, 7, 13, 19]  # UTC
    now = datetime.now(timezone.utc)
    today_hours = [now.replace(hour=h, minute=0, second=0, microsecond=0) for h in schedule_hours]
    past = [h for h in today_hours if h <= now]
    if past:
        start = past[-1]
    else:
        # fallback: yesterday's last slot
        start = (now - timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)
    # SGT label (UTC+8)
    sgt_start = start + timedelta(hours=8)
    sgt_end = now + timedelta(hours=8)
    label = f"{sgt_start.strftime('%H:%M')} - {sgt_end.strftime('%H:%M')} SGT"
    return start, now, label


# ------- Load handles -------
def load_handles() -> dict[str, list[str]]:
    by_list: dict[str, list[str]] = {sl: [] for sl in SUB_LISTS}
    with HANDLES_CSV.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            handle = row["handle"].strip().lstrip("@")
            sub = row["sub_list"].strip()
            if sub in by_list:
                by_list[sub].append(handle)
    return by_list


def batch(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


# ------- Grok client -------
client = OpenAI(api_key=GROK_API_KEY, base_url=GROK_BASE_URL)


def grok_call(prompt: str, handles_batch: list[str], from_dt: datetime, to_dt: datetime) -> str:
    """Single Grok call with Live Search restricted to a batch of <=10 X handles."""
    search_params = {
        "mode": "on",
        "sources": [{"type": "x", "x_handles": handles_batch}],
        "from_date": from_dt.strftime("%Y-%m-%d"),
        "to_date": to_dt.strftime("%Y-%m-%d"),
        "return_citations": True,
    }
    resp = client.chat.completions.create(
        model=GROK_MODEL,
        messages=[{"role": "user", "content": prompt}],
        extra_body={"search_parameters": search_params},
    )
    return resp.choices[0].message.content or ""


# ------- Per-batch summary prompt -------
PER_BATCH_PROMPT = """You are reading posts from a curated set of {n} high-signal crypto X handles ({sub_list} sub-list) over the last {hours} hours (window: {from_dt} to {to_dt} UTC).

Handles: {handles}

Pull the most-discussed topics, specific calls/trades mentioned, and notable disagreements. Cite each point with the handle that posted it.

OUTPUT FORMAT (concise, bullets only — no fluff):

## {sub_list} — top signals
- @handle: <claim/observation/call> [1-line context]
- @handle: <claim/observation/call> [1-line context]
...

Focus on substance: specific tickers, levels, theses, on-chain events, regulatory developments. Skip generic market commentary unless multiple handles converge on it. If a handle posted nothing relevant, omit. If multiple handles say the same thing, mark with "(consensus across N handles)".

Skip macro topics (rates, Fed, equities) unless they DIRECTLY drove a crypto move — the user has separate macro bots."""


# ------- Final synthesis prompt -------
FINAL_SYNTH_PROMPT = """You are synthesizing a Crypto Signal Digest from per-sub-list summaries of {total_handles} high-signal X handles.

Window: {label}

Below are the raw per-sub-list signals. Synthesize into the following digest format. Be concrete, cite handles, avoid filler.

--- RAW SUB-LIST SIGNALS ---
{raw_signals}
--- END RAW ---

OUTPUT FORMAT (use these exact section headers):

🪙 CRYPTO SIGNAL DIGEST — {label}

## 1. TOP STORIES
3-5 most-discussed narratives across all 5 sub-lists. Each: 1-line headline + 1-2 sentence why-it-matters + handles citing.

## 2. MARKET SNAPSHOT
BTC / ETH / notable alts: price action observations from handles + dominant sentiment lean (bullish/bearish/mixed). Pull specific levels mentioned.

## 3. TRADE IDEAS
Specific actionable setups mentioned by handles. Each: $TICKER, direction (long/short), entry/target/stop where mentioned, conviction, source handle. If no concrete setups appeared, say "No concrete setups posted this window" — don't manufacture.

## 4. NARRATIVE SUSTAINABILITY
Which themes are gaining traction (multi-handle convergence) vs fading (declining mentions, contradicting calls). Brief.

## 5. BY SUB-LIST
- **DeFi:** 1-2 most important signals
- **Trading:** 1-2 most important signals
- **Macro:** 1-2 most important signals (skip if commoditized)
- **Other:** 1-2 most important signals
- **Infra/Builders:** 1-2 most important signals

End with ---SOURCES--- then a numbered list of handles cited.

Reader profile: experienced macro/equity fundamental investor (yield curves, P/E, DCF, credit spreads) with NO crypto background. Include parenthetical TradFi analogies for crypto-native jargon when first introduced (e.g. "funding rate (analogous to overnight repo rate)")."""


# ------- Run all batches -------
def collect_per_sub_signals(handles_by_list, from_dt, to_dt) -> str:
    hours = round((to_dt - from_dt).total_seconds() / 3600, 1)
    chunks = []
    for sub_list, handles in handles_by_list.items():
        if not handles:
            continue
        print(f"[{sub_list}] {len(handles)} handles → {len(list(batch(handles, HANDLE_BATCH_SIZE)))} batches")
        sub_chunks = []
        for i, hb in enumerate(batch(handles, HANDLE_BATCH_SIZE), 1):
            prompt = PER_BATCH_PROMPT.format(
                n=len(hb),
                sub_list=sub_list,
                hours=hours,
                from_dt=from_dt.isoformat(timespec="minutes"),
                to_dt=to_dt.isoformat(timespec="minutes"),
                handles=", ".join(f"@{h}" for h in hb),
            )
            try:
                result = grok_call(prompt, hb, from_dt, to_dt)
                sub_chunks.append(result)
                print(f"  batch {i}/{(len(handles) + HANDLE_BATCH_SIZE - 1) // HANDLE_BATCH_SIZE} ok ({len(result)} chars)")
            except Exception as e:
                print(f"  batch {i} FAILED: {e}")
                sub_chunks.append(f"## {sub_list} batch {i} — ERROR: {e}")
            time.sleep(1)  # be gentle
        chunks.append("\n\n".join(sub_chunks))
    return "\n\n".join(chunks)


def synthesize_digest(raw_signals: str, label: str, total_handles: int) -> str:
    prompt = FINAL_SYNTH_PROMPT.format(
        total_handles=total_handles, label=label, raw_signals=raw_signals
    )
    # final synth call — no live search needed, raw signals are already gathered
    resp = client.chat.completions.create(
        model=GROK_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.choices[0].message.content or ""


# ------- Telegram posting (bot API) -------
def tg_send_message(text: str, disable_notification: bool = False) -> dict:
    r = requests.post(
        f"{TG_API}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
            "disable_notification": disable_notification,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def tg_pin_message(message_id: int):
    r = requests.post(
        f"{TG_API}/pinChatMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id, "disable_notification": True},
        timeout=30,
    )
    if not r.ok:
        print(f"WARN: pin failed: {r.text}")


def tg_get_recent_messages(limit: int = 5) -> list[dict]:
    # Bot API can't read history; we use getUpdates as a weak fallback
    # but for the duplicate guard we rely on a local timestamp file instead.
    return []


# ------- Duplicate guard (file-based since bots can't read history) -------
LAST_RUN_FILE = Path(__file__).parent / ".last_run_marker"


def is_duplicate_run(window_label: str) -> bool:
    if SKIP_DUPLICATE_CHECK:
        return False
    if not LAST_RUN_FILE.exists():
        return False
    last = LAST_RUN_FILE.read_text().strip()
    return last == window_label


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


# ------- Main -------
def main():
    from_dt, to_dt, label = time_window()
    print(f"Window: {label} ({from_dt.isoformat()} → {to_dt.isoformat()})")

    if is_duplicate_run(label):
        print(f"Duplicate run guard: window '{label}' already processed. Set SKIP_DUPLICATE_CHECK=1 to override.")
        return

    handles_by_list = load_handles()
    total = sum(len(v) for v in handles_by_list.values())
    print(f"Loaded {total} handles across {len(handles_by_list)} sub-lists")

    print("\n=== Stage 1: per-sub-list Grok calls ===")
    raw_signals = collect_per_sub_signals(handles_by_list, from_dt, to_dt)
    print(f"\nRaw signals: {len(raw_signals)} chars")

    print("\n=== Stage 2: final synthesis ===")
    digest = synthesize_digest(raw_signals, label, total)
    print(f"Digest: {len(digest)} chars")

    print("\n=== Stage 3: post to Telegram ===")
    # Split body and sources
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
