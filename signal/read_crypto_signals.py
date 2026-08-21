"""
Crypto Signal Digest — Grok + Sonnet edition (GitHub Actions)
=============================================================

One-shot pipeline:
  Stage 1: single Grok call with x_search over all 100 handles for the 24h window
  Stage 2: Claude Sonnet synthesizes into the standard digest format
  Stage 3: Telegram bot posts to supergroup

Runs once daily at 00:00 UTC (08:00 SGT) via GitHub Actions cron,
plus manual workflow_dispatch trigger.

Secrets (env vars; provided by GH Actions):
  - XAI_API_KEY
  - ANTHROPIC_API_KEY
  - TELEGRAM_BOT_TOKEN
  - TELEGRAM_CHAT_ID
"""

import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import anthropic


# ------- stdout encoding fix -------
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ------- Paths -------
SCRIPT_DIR = Path(__file__).resolve().parent
HANDLES_CSV = SCRIPT_DIR / "handles.csv"


# ------- BOM cleaner -------
def _clean(val: str) -> str:
    if val is None:
        return ""
    return val.strip().replace("\ufeff", "")


XAI_API_KEY = _clean(os.environ.get("XAI_API_KEY", ""))
ANTHROPIC_API_KEY = _clean(os.environ.get("ANTHROPIC_API_KEY", ""))
TELEGRAM_BOT_TOKEN = _clean(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
TELEGRAM_CHAT_ID = _clean(os.environ.get("TELEGRAM_CHAT_ID", ""))
# Normalize: supergroup IDs must start with -100. Some users paste without the leading minus.
if TELEGRAM_CHAT_ID and not TELEGRAM_CHAT_ID.startswith("-") and TELEGRAM_CHAT_ID.isdigit() and len(TELEGRAM_CHAT_ID) >= 10:
    TELEGRAM_CHAT_ID = "-" + TELEGRAM_CHAT_ID
print(f"[startup] TELEGRAM_CHAT_ID prefix={TELEGRAM_CHAT_ID[:5]!r} len={len(TELEGRAM_CHAT_ID)}")

# Telethon (user-account) for reading existing digest group history

for name, val in [
    ("XAI_API_KEY", XAI_API_KEY),
    ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
    ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
    ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
]:
    if not val:
        sys.exit(f"ERROR: {name} env var missing")


# ------- Config -------
GROK_MODEL = os.environ.get("GROK_MODEL", "grok-4-fast").strip() or "grok-4-fast"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5").strip() or "claude-sonnet-4-5"
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


# ------- Time window: last 24h (08:00 SGT yesterday -> now) -------
def time_window():
    now = datetime.now(timezone.utc)
    from_dt = now - timedelta(hours=24)
    sgt_start = from_dt + timedelta(hours=8)
    sgt_end = now + timedelta(hours=8)
    label = f"{sgt_start.strftime('%a %H:%M')} - {sgt_end.strftime('%a %H:%M')} SGT (24h)"
    return from_dt, now, label


# ------- Load handles -------
def load_handles() -> list[tuple[str, str]]:
    out = []
    with HANDLES_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            handle = row["handle"].strip().lstrip("@")
            sub = row["sub_list"].strip()
            out.append((handle, sub))
    return out


# ============================================================
# STAGE 1 — Grok x_search (batched, max 20 handles per call)
# ============================================================
GROK_HANDLES_PER_CALL = 20  # xAI hard limit


import re
from concurrent.futures import ThreadPoolExecutor, as_completed

# Strict X status URL: https://x.com/<handle>/status/<digits>
X_STATUS_RE = re.compile(r"^https?://(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})/status/(\d+)")


def _grok_one_batch(handle_batch: list[tuple[str, str]], from_date: str, to_date: str, batch_label: str) -> list[dict]:
    handle_list = [h for h, _ in handle_batch]
    sub_map = {h: s for h, s in handle_batch}

    instructions = (
        "You are a strict data extractor. Use the x_search tool to retrieve real posts from the supplied X handles "
        "in the date window. You MUST NOT paraphrase, summarize, or invent.\n\n"
        "OUTPUT FORMAT: a single JSON array (and NOTHING else — no prose, no markdown, no code fences). "
        "Each element must have EXACTLY these keys:\n"
        '  - "handle": the X username, no @ prefix, MUST be one of the allowed handles\n'
        '  - "sub_list": the label from the mapping below\n'
        '  - "timestamp": ISO 8601 with timezone (the post\'s actual creation time)\n'
        '  - "text": the EXACT verbatim post text — do not summarize, do not truncate, do not edit\n'
        '  - "url": the canonical post URL in the form https://x.com/<handle>/status/<id> — must be a real URL returned by x_search\n\n'
        "RULES:\n"
        "- If x_search returns nothing for a handle, OMIT that handle. Do not invent posts.\n"
        "- If you cannot retrieve the verbatim text, OMIT that post.\n"
        "- If the URL is not in the form https://x.com/<handle>/status/<id>, OMIT that post.\n"
        "- Skip pure shitposts, low-effort memes, and pure replies. Keep market calls, trade ideas, "
        "narrative shifts, protocol news, regulatory events, macro pivots.\n"
        "- If x_search returns zero posts for the entire batch, output: []\n\n"
        f"Allowed handles (sub_list mapping): {json.dumps(sub_map)}"
    )

    body = {
        "model": GROK_MODEL,
        "instructions": instructions,
        "input": f"Retrieve all signal-worthy posts from these handles between {from_date} and {to_date} (exclusive). Return only the JSON array.",
        "tools": [
            {
                "type": "x_search",
                "allowed_x_handles": handle_list,
                "from_date": from_date,
                "to_date": to_date,
            }
        ],
    }

    r = requests.post(
        "https://api.x.ai/v1/responses",
        headers={
            "Authorization": f"Bearer {XAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=600,
    )
    if not r.ok:
        print(f"  Grok HTTP {r.status_code} ({batch_label}): {r.text[:600]}")
        r.raise_for_status()

    data = r.json()
    text_chunks = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    text_chunks.append(c.get("text", ""))
    out = "\n".join(text_chunks).strip()

    usage = data.get("usage", {})
    posts = _parse_json_array(out)
    print(f"  {batch_label}: parsed {len(posts)} posts, usage={usage}")
    return posts


def _parse_json_array(s: str) -> list[dict]:
    """Parse Grok's JSON-array output, tolerating stray markdown fences."""
    s = s.strip()
    # Strip ```json ... ``` fences if present
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    # Find the outermost JSON array if there's prose around it
    start = s.find("[")
    end = s.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        arr = json.loads(s[start : end + 1])
        return [p for p in arr if isinstance(p, dict)]
    except json.JSONDecodeError as e:
        print(f"    JSON parse error: {e}; raw snippet: {s[:200]!r}")
        return []


def _verify_post(post: dict, allowed_handles: set[str]) -> tuple[bool, str]:
    """
    Verify a single post:
      1. URL matches https://x.com/<handle>/status/<id> AND <handle> equals post['handle']
      2. handle is in the allowed set
      3. URL HEAD request returns 2xx or 3xx (not 404)
    Returns (ok, reason).
    """
    url = (post.get("url") or "").strip()
    handle = (post.get("handle") or "").lstrip("@").strip()
    text = (post.get("text") or "").strip()

    if not url or not handle or not text:
        return False, "missing required field"
    if handle not in allowed_handles:
        return False, f"handle '{handle}' not in allowed list (Grok hallucinated)"

    m = X_STATUS_RE.match(url)
    if not m:
        return False, f"url shape invalid: {url}"
    url_handle = m.group(1)
    if url_handle.lower() != handle.lower():
        return False, f"url handle '{url_handle}' ≠ claimed handle '{handle}'"

    # Verify URL resolves. Use GET with allow_redirects since x.com sometimes 405s HEAD.
    try:
        r = requests.get(
            url,
            allow_redirects=True,
            timeout=12,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            },
        )
    except requests.RequestException as e:
        return False, f"network error: {type(e).__name__}"

    # X returns 200 for both real posts and the generic interstitial.
    # A deleted/nonexistent status redirects to /<handle> or returns the suspended page.
    final = r.url.lower()
    if "/status/" not in final:
        return False, f"final url has no /status/: {r.url}"
    # accept any 2xx/3xx
    if r.status_code >= 400:
        return False, f"http {r.status_code}"
    return True, "ok"


def verify_posts(posts: list[dict], allowed_handles: set[str]) -> tuple[list[dict], list[dict]]:
    """Returns (verified, rejected). Parallelizes URL checks."""
    if not posts:
        return [], []
    verified: list[dict] = []
    rejected: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_verify_post, p, allowed_handles): p for p in posts}
        for fut in as_completed(futures):
            p = futures[fut]
            ok, reason = fut.result()
            if ok:
                verified.append(p)
            else:
                rejected.append({**p, "_reject_reason": reason})
    return verified, rejected


def fetch_via_grok(handles: list[tuple[str, str]], from_dt: datetime, to_dt: datetime) -> list[dict]:
    """
    Returns a list of post dicts. xAI x_search caps at 20 handles/call, so we batch.
    NOTE: to_date is EXCLUSIVE — add +1 day to include 'today'.
    """
    from_date = from_dt.date().isoformat()
    to_date = (to_dt + timedelta(days=1)).date().isoformat()

    batches = [
        handles[i : i + GROK_HANDLES_PER_CALL]
        for i in range(0, len(handles), GROK_HANDLES_PER_CALL)
    ]
    print(f"  Grok model: {GROK_MODEL}")
    print(f"  Handles: {len(handles)} → {len(batches)} batches of ≤{GROK_HANDLES_PER_CALL}")
    print(f"  Window: {from_date} -> {to_date} (exclusive)")

    all_posts: list[dict] = []
    for i, batch in enumerate(batches, 1):
        label = f"batch {i}/{len(batches)} ({len(batch)} handles)"
        try:
            posts = _grok_one_batch(batch, from_date, to_date, label)
            all_posts.extend(posts)
        except Exception as e:
            print(f"  {label} FAILED: {type(e).__name__}: {str(e)[:200]}")

    print(f"  Grok returned {len(all_posts)} candidate posts")
    return all_posts


# ============================================================
# STAGE 2 — Claude Sonnet synthesizes
# ============================================================
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYNTHESIS_PROMPT = """You are synthesizing a {category_title} Signal Digest from VERIFIED X posts.

Window: {label}
Category: {category_title} ({category_desc})
Verified posts: {n_posts}

VERIFIED POSTS (JSON array, each post has handle/sub_list/timestamp/text/url — ALL URLs have been HTTP-checked to resolve to real X status pages):
---
{raw_posts}
---

CRITICAL ANTI-HALLUCINATION RULES:
- You may ONLY cite handles and quote text that appear in the verified posts array above.
- Do NOT include any URLs in the output. Handles cited inline (@handle) are the attribution.
- If a topic is not represented in the verified posts, you may NOT include it.
- If you quote a post, the quote must be a literal substring of that post's "text" field.
- If verified posts are too few or off-topic, write "Low signal in this window — only N verified posts retrieved" and produce a thin digest with only what's supported.

HARD FORMAT RULES (high-signal, no fluff):
- The ENTIRE output MUST be under 3,400 characters — it is sent as ONE Telegram message. Never exceed this.
- Every point MUST carry clear evidence: a specific number, level, quote fragment (literal substring of a post), or named event — plus the handles behind it. No vibes, no generic commentary.
- Attribute every move or claim to its DRIVER (catalyst, flows, positioning, event) — never report a move without its cause.
- No preamble, no filler, no restating the obvious. If a bullet doesn't teach the reader something concrete, cut it.

Synthesize into this EXACT structure:

{header_emoji} {category_title_upper} SIGNAL — {label}

## 🤝 COMMON GROUND
3-5 themes where MULTIPLE handles independently converge. One single-line bullet each:
- **Theme** — the shared point + the strongest concrete evidence (levels, data, short quote) (@x, @y, @z)

## 💡 BEST IDEAS
1-3 standout differentiated ideas — non-consensus, clearly argued, well-evidenced. One single-line bullet each:
- **Idea** (@handle) — the thesis + the specific evidence or reasoning given

## 👀 WATCH
One line: the most important upcoming catalyst, level, or event cited by handles.

Do NOT append a sources or links section — the inline @handle attributions are the sourcing. Cite each handle at most once per bullet, and if a handle pushed the same idea across multiple posts/threads, treat it as ONE data point (it is not extra convergence).

READER PROFILE: experienced macro/equity fundamental investor (yield curves, P/E, DCF, credit spreads, options Greeks, duration, carry trades, 13F filings) with NO crypto background. When using crypto-native jargon, add a brief parenthetical TradFi analogy on first mention — e.g. "funding rate (~ overnight repo rate for perps)".

If the raw output is mostly empty or noise, say "Low signal in this window" instead of inventing content."""


# Crypto-themed keywords for auto-classifying sub_lists
CRYPTO_KEYWORDS = ("crypto", "defi", "trading", "infra", "builder", "chain", "l1", "l2", "eth", "btc", "sol", "nft", "web3", "dex", "perp")

def categorize_sublist(sub_list: str) -> str:
    """Map a sub_list label to one of the three digest categories."""
    s = sub_list.lower()
    if any(k in s for k in CRYPTO_KEYWORDS):
        return "crypto"
    if "equity" in s:
        return "equity"
    return "macro"

CATEGORY_CONFIG = {
    "equity": {
        "title": "Equity",
        "title_upper": "EQUITY",
        "emoji": "📊",
        "desc": "Single stocks, sectors, earnings, valuation and positioning from equity-focused handles",
    },
    "macro": {
        "title": "Macro",
        "title_upper": "MACRO",
        "emoji": "📈",
        "desc": "TradFi macro: rates, FX, commodities, central banks, credit, geopolitics",
    },
    "crypto": {
        "title": "Crypto",
        "title_upper": "CRYPTO",
        "emoji": "🪙",
        "desc": "DeFi, on-chain trading, infrastructure, L1/L2s",
    },
}

def synthesize(verified_posts: list[dict], label: str, category: str = "crypto") -> str:
    cfg = CATEGORY_CONFIG[category]
    if not verified_posts:
        return (
            f"{cfg['emoji']} {cfg['title_upper']} SIGNAL DIGEST — {label}\n\n"
            "Low signal in this window — zero verified posts in this category. "
            "No digest content can be produced without verified sources."
        )

    raw = json.dumps(verified_posts, ensure_ascii=False, indent=2)
    sub_lists_present = sorted({p.get("sub_list", "Unknown") for p in verified_posts})
    print(f"  [{category}] Sub-lists in verified posts: {sub_lists_present}")
    prompt = SYNTHESIS_PROMPT.format(
        label=label,
        n_posts=len(verified_posts),
        raw_posts=raw,
        category_title=cfg["title"],
        category_title_upper=cfg["title_upper"],
        category_desc=cfg["desc"],
        header_emoji=cfg["emoji"],
    )
    msg = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    text_parts = [b.text for b in msg.content if b.type == "text"]
    print(f"  Claude usage: input={msg.usage.input_tokens} output={msg.usage.output_tokens}")
    return "\n".join(text_parts)


# ============================================================
# STAGE 3 — Telegram delivery
# ============================================================
def tg_send_message(text: str, chat_id: str = "", disable_notification: bool = False) -> dict:
    r = requests.post(
        f"{TG_API}/sendMessage",
        json={
            "chat_id": chat_id or TELEGRAM_CHAT_ID,
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


def tg_send_file(file_path: str, chat_id: str = "", caption: str = "") -> dict:
    with open(file_path, "rb") as f:
        r = requests.post(
            f"{TG_API}/sendDocument",
            data={
                "chat_id": chat_id or TELEGRAM_CHAT_ID,
                "caption": caption,
            },
            files={"document": f},
            timeout=60,
        )
    if not r.ok:
        print(f"TG file send error {r.status_code}: {r.text[:500]}")
    r.raise_for_status()
    return r.json()


def tg_pin_message(message_id: int, chat_id: str = ""):
    r = requests.post(
        f"{TG_API}/pinChatMessage",
        json={
            "chat_id": chat_id or TELEGRAM_CHAT_ID,
            "message_id": message_id,
            "disable_notification": True,
        },
        timeout=30,
    )
    if not r.ok:
        print(f"WARN: pin failed: {r.text[:300]}")


def chunk_for_tg(text: str, max_chars: int = 4000) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    cur = ""
    for line in text.splitlines(keepends=True):
        # Hard-split any single line longer than max_chars (Telegram caps
        # messages at 4096 chars; an oversized line previously produced an
        # oversized chunk -> 400 "message is too long").
        while len(line) > max_chars:
            space = max_chars - len(cur)
            if space > 200:
                cur += line[:space]
                line = line[space:]
            chunks.append(cur)
            cur = ""
            if len(line) <= max_chars:
                break
        if len(cur) + len(line) > max_chars:
            if cur:
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
    print(f"Window: {label}")
    print(f"  UTC: {from_dt.isoformat()} -> {to_dt.isoformat()}\n")

    handles = load_handles()
    print(f"Loaded {len(handles)} handles\n")

    print("=== Stage 1: Grok x_search (strict JSON extraction) ===")
    t0 = time.time()
    candidate_posts = fetch_via_grok(handles, from_dt, to_dt)
    print(f"Stage 1 done in {time.time() - t0:.1f}s\n")

    print("=== Stage 1b: Verify candidate posts ===")
    t_v = time.time()
    allowed_handles = {h for h, _ in handles}
    verified, rejected = verify_posts(candidate_posts, allowed_handles)
    print(f"  Verified: {len(verified)} / Rejected: {len(rejected)} in {time.time() - t_v:.1f}s")
    if rejected:
        # Tally reject reasons for visibility
        from collections import Counter

        reasons = Counter(r.get("_reject_reason", "?") for r in rejected)
        for reason, n in reasons.most_common(10):
            print(f"    ✖ {n}× {reason}")

    if not verified:
        try:
            tg_send_message(
                f"⚠️ Crypto Signal Digest — {label}: 0 posts passed verification "
                f"({len(candidate_posts)} candidates from Grok, all rejected). "
                "No digest sent to avoid hallucinated content."
            )
        except Exception as e:
            print(f"Failed to send health alert: {e}")
        print("Aborting: nothing verified. Health alert sent.")
        return

    # Split verified posts into equity / macro / crypto buckets
    buckets: dict[str, list] = {"equity": [], "macro": [], "crypto": []}
    for p in verified:
        buckets[categorize_sublist(p.get("sub_list", ""))].append(p)
    print("\nBucket split: " + ", ".join(f"{k}={len(v)}" for k, v in buckets.items()))

    pin_first = True  # only pin the very first digest message of the run

    for category, bucket in buckets.items():
        if not bucket:
            print(f"\n--- Skipping {category}: 0 verified posts in bucket ---")
            continue

        cfg = CATEGORY_CONFIG[category]
        header_emoji = cfg["emoji"]
        title_upper = cfg["title_upper"]

        print(f"\n=== Stage 2 [{category}]: Claude synthesizes ===")
        t1 = time.time()
        digest = synthesize(bucket, label, category=category)
        print(f"  done in {time.time() - t1:.1f}s, digest = {len(digest)} chars")

        if "---SOURCES---" in digest:
            body, sources = digest.split("---SOURCES---", 1)
        else:
            body, sources = digest, ""

        print(f"=== Stage 3 [{category}]: Telegram digest ===")
        first_msg_id = None
        for chunk in chunk_for_tg(body.strip()):
            resp = tg_send_message(chunk)
            if first_msg_id is None:
                first_msg_id = resp["result"]["message_id"]
            time.sleep(0.5)

        if sources.strip():
            # Sources are no longer sent — inline @handle attribution only.
            print(f"  (discarded {len(sources.strip())} chars of stray sources output)")

        if pin_first and first_msg_id:
            tg_pin_message(first_msg_id)
            print(f"  Pinned digest message {first_msg_id}")
            pin_first = False

    # Verification stats logged only (no Telegram footer)
    print(f"Verified {len(verified)} / rejected {len(rejected)} candidates.")

    print("\nDone.")


if __name__ == "__main__":
    main()
