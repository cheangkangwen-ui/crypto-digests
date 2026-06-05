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
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import anthropic
from duckduckgo_search import DDGS
from fpdf import FPDF


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
TELEGRAM_API_ID = _clean(os.environ.get("TELEGRAM_API_ID", ""))
TELEGRAM_API_HASH = _clean(os.environ.get("TELEGRAM_API_HASH", ""))
TELEGRAM_SESSION = _clean(os.environ.get("TELEGRAM_SESSION", ""))

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
TRADE_IDEAS_MODEL = os.environ.get("TRADE_IDEAS_MODEL", "claude-opus-4-7").strip() or "claude-opus-4-7"
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


# ------- Load analytical frameworks -------
FRAMEWORKS_DIR = SCRIPT_DIR / "frameworks"

def _load_framework(name: str) -> str:
    path = FRAMEWORKS_DIR / f"{name}.md"
    if not path.exists():
        print(f"WARN: framework file not found: {path}")
        return ""
    return path.read_text(encoding="utf-8")

MACRO_FRAMEWORK = _load_framework("macro")
TRADING_FRAMEWORK = _load_framework("trading")
EQUITY_FRAMEWORK = _load_framework("equity")


# ------- Telegram history reader (Telethon, user-account API) -------
# Reads message history from existing digest groups for use as trade-ideas context.
# Falls back gracefully if Telethon creds are not set.

DIGEST_HISTORY_GROUPS = [
    "📊 Crypto Signal Digest",  # this pipeline's output
    "🪙 Crypto Digest",          # ramp-bot output
]
DIGEST_HISTORY_DAYS = 30
DIGEST_HISTORY_MAX_MESSAGES = 200  # per group
DIGEST_HISTORY_MAX_CHARS = 40000   # truncate per group to keep prompt bounded


async def _fetch_group_history_async(group_name: str, days: int, max_messages: int) -> str:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    if not (TELEGRAM_API_ID and TELEGRAM_API_HASH and TELEGRAM_SESSION):
        return ""

    session = StringSession(TELEGRAM_SESSION) if len(TELEGRAM_SESSION) > 20 else TELEGRAM_SESSION
    tg = TelegramClient(session, int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
    try:
        await tg.connect()
        if not await tg.is_user_authorized():
            return f"[{group_name}] Telethon session not authorized"

        # Find the group by name
        group_entity = None
        async for dialog in tg.iter_dialogs():
            if dialog.name == group_name:
                group_entity = dialog.entity
                break
        if group_entity is None:
            return f"[{group_name}] not found in dialogs"

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        messages = []
        async for msg in tg.iter_messages(group_entity, limit=max_messages):
            if msg.date < cutoff:
                break
            if msg.text and msg.text.strip():
                messages.append(f"[{msg.date.strftime('%Y-%m-%d %H:%M')}] {msg.text.strip()}")

        if not messages:
            return f"[{group_name}] no messages in last {days} days"

        # Reverse so oldest is first (chronological reading order)
        messages.reverse()
        body = "\n\n---\n\n".join(messages)
        if len(body) > DIGEST_HISTORY_MAX_CHARS:
            body = body[-DIGEST_HISTORY_MAX_CHARS:]  # keep most recent on truncation
            body = "[... older messages truncated ...]\n\n" + body
        return f"## {group_name} — last {days} days ({len(messages)} messages)\n\n{body}"
    finally:
        await tg.disconnect()


def gather_telegram_history() -> str:
    """Return concatenated history from all DIGEST_HISTORY_GROUPS, or empty string if creds missing."""
    if not (TELEGRAM_API_ID and TELEGRAM_API_HASH and TELEGRAM_SESSION):
        print("  Telegram history reader: Telethon creds not set — skipping")
        return ""

    import asyncio
    parts = []
    for group_name in DIGEST_HISTORY_GROUPS:
        try:
            result = asyncio.run(_fetch_group_history_async(
                group_name, DIGEST_HISTORY_DAYS, DIGEST_HISTORY_MAX_MESSAGES
            ))
            if result:
                parts.append(result)
                print(f"  Telegram history: fetched {len(result)} chars from '{group_name}'")
        except Exception as e:
            print(f"  Telegram history: failed to fetch '{group_name}': {e}")
    return "\n\n=============================\n\n".join(parts)


# ------- Web search tool (DuckDuckGo) for trade ideas -------
SEARCH_TOOL = {
    "name": "web_search",
    "description": "Search the web for current crypto prices, technicals, macro data, on-chain metrics, and recent news. Use to ground trade ideas in real-time data.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query"
            }
        },
        "required": ["query"]
    }
}

def web_search(query: str) -> str:
    try:
        results = DDGS().text(query, max_results=6)
        return json.dumps(results, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


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
- Every URL you write in the SOURCES section MUST be copied verbatim from a post's "url" field above. Do not modify, shorten, or invent URLs.
- If a topic is not represented in the verified posts, you may NOT include it.
- If you quote a post, the quote must be a literal substring of that post's "text" field.
- If verified posts are too few or off-topic, write "Low signal in this window — only N verified posts retrieved" and produce a thin digest with only what's supported.

Synthesize into this EXACT structure. Be concrete, cite handles, no fluff.

{header_emoji} {category_title_upper} SIGNAL DIGEST — {label}

## 1. TOP STORIES
The 3-5 most-discussed narratives across all sub-lists. Each:
- **One-line headline**
- 1-2 sentence why-it-matters
- Cite handles (@x, @y, @z)

## 2. MARKET SNAPSHOT
{market_snapshot_guidance}

## 3. NARRATIVE SUSTAINABILITY
Which themes are gaining traction (multi-handle convergence) vs fading (declining mentions, contradicting calls). Brief.

## 4. BY SUB-LIST
For EACH sub_list that appears in the verified posts JSON below, write a bolded label followed by 1-2 most important signals from handles in that sub_list. Discover the sub_lists dynamically from the data — do NOT assume a fixed set. Skip any sub_list with zero verified posts (don't list it at all). Format: `- **<sub_list_name>:** <signals>`

## 5. 📖 JARGON DECODER
Pick 4-8 crypto-native terms that appeared in sections 1-5 above (e.g. funding rate, basis trade, LST, LRT, restaking, MEV, perp, AMM, TVL, OI, bridge exploit, ve-tokenomics, depeg, liquidation cascade, points farming, FDV vs market cap, sequencer, rollup, blob fees, ETF flows, basis spread). SKIP basics already-known: BTC, ETH, bull/bear, market cap, wallet, stablecoin.

For each term, write a 3-5 sentence paragraph using this format:

📌 <TERM>
[Mechanical definition in crypto context] [Closest TradFi analogy with specific instrument] [Why it matters to you as an investor — what signal it gives or what risk it represents]

QUALITY BAR — examples of the depth expected:

📌 FUNDING RATE
In perpetual futures (crypto's main derivative — contracts with no expiry), longs pay shorts (or vice versa) every 8 hours based on the gap between perp price and spot. Closest TradFi analogy: the overnight repo rate or the carry cost of holding a futures position into delivery — it's the price of leverage. When funding spikes positive, traders are paying steep premiums to stay long, often a contrarian top signal; deeply negative funding can flag capitulation. As an investor, persistent +0.05%/8h funding (~55% annualized) on BTC means leveraged crowd is offsides — watch for forced unwinds.

📌 BRIDGE EXPLOIT
Cross-chain bridges hold pooled collateral (e.g. ETH locked on Ethereum, wrapped ETH minted on Solana). Exploits drain the locked side — the wrapped tokens on the other chain become unbacked claims. Closest TradFi analogy: a custodial bank run where the custodian's vault is empty but depository receipts still circulate. Historically $2.5B+ stolen this way (Ronin, Wormhole, Nomad). For an investor, bridge TVL is a hidden tail risk in any L2/L1 thesis — a $500M bridge hack can crater the receiving chain's native token 30% in hours regardless of fundamentals.

End with:
---SOURCES---
A numbered list. For EACH source: handle, 1-line reason cited, and the EXACT url from the verified posts array. Only include sources you actually drew from above. Do not add commentary URLs.

READER PROFILE: experienced macro/equity fundamental investor (yield curves, P/E, DCF, credit spreads, options Greeks, duration, carry trades, 13F filings) with NO crypto background. When introducing crypto-native jargon in sections 1-4, use a parenthetical TradFi analogy on first mention — e.g. "funding rate (~ overnight repo rate for perpetual futures)". Then expand fully in the JARGON DECODER section.

## 6. TRADE IDEAS
Specific actionable setups mentioned by handles in this category. Each: $TICKER, direction (long/short), entry/target/stop where mentioned, conviction level, source handle. If no concrete setups: "No concrete setups posted this window" — don't manufacture.

If the raw output is mostly empty or noise, say "Low signal in this window" instead of inventing content. In that case, still produce a JARGON DECODER for 4-5 broadly important terms readers should know."""


# Crypto-themed keywords for auto-classifying sub_lists
CRYPTO_KEYWORDS = ("crypto", "defi", "trading", "infra", "builder", "chain", "l1", "l2", "eth", "btc", "sol", "nft", "web3", "dex", "perp")

def is_crypto_sublist(sub_list: str) -> bool:
    s = sub_list.lower()
    return any(k in s for k in CRYPTO_KEYWORDS)

CATEGORY_CONFIG = {
    "crypto": {
        "title": "Crypto",
        "title_upper": "CRYPTO",
        "emoji": "🪙",
        "desc": "DeFi, on-chain trading, infrastructure, L1/L2s",
        "market_snapshot": "BTC / ETH / notable alts: price action observations from handles + dominant sentiment lean. Pull specific levels mentioned.",
    },
    "non-crypto": {
        "title": "Macro & Markets",
        "title_upper": "MACRO & MARKETS",
        "emoji": "📈",
        "desc": "TradFi macro, equities, rates, commodities, geopolitics",
        "market_snapshot": "Key macro signals: equity indices, rates, DXY, commodities, credit. Pull specific levels and Fed/central bank commentary from handles.",
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
        market_snapshot_guidance=cfg["market_snapshot"],
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
# STAGE 2b — Trade ideas (Opus + frameworks + web search)
# ============================================================
TRADE_IDEAS_PROMPT = """You are a senior {analyst_role} producing actionable trade ideas for {asset_class}.

You have two analytical frameworks to apply:

=== MACRO FRAMEWORK ===
{macro_framework}

=== {secondary_framework_label} ===
{secondary_framework}

=== TODAY'S DIGEST ===
{digest_text}

=== DIGEST SOURCES ===
{digest_sources}

=== RECENT DIGEST HISTORY (LAST 30 DAYS) ===
Use this longitudinal context to assess narrative sustainability, identify themes that are gaining/fading momentum, spot whether a trade idea aligns with or contradicts recent positioning, and avoid recommending trades that were already played out. If empty, ignore this section.
{telegram_history}

=== FUNDING COST DISCIPLINE (MANDATORY for any perp/leveraged trade) ===
Funding rates are a real, hourly cash cost that scale LINEARLY with time. They are the single most underestimated cost in perp trading and most dangerous on relative-value (RV) pairs and multi-month holds. Internalize the following before proposing any perp trade:

Mechanics:
- Perps have no expiry. Funding is a peer-to-peer payment (long<->short) that tethers perp price to spot. Exchanges do not take this fee.
- Perp > spot (longs in demand) -> funding positive -> longs pay shorts.
- Perp < spot (shorts in demand) -> funding negative -> shorts pay longs.
- Hyperliquid pays hourly; most CEXes every 8h. It scales linearly with holding period.

Why it eats RV/pair trades:
- Single directional trade pays/receives funding on one leg. A pair trade hits BOTH legs and the signs rarely cancel cleanly.
- Crypto perps are structurally long-biased: BTC/ETH funding historically averages ~5-15% annualized positive, alts often higher.
- Long BTC / Short ETH: pay on long leg, receive on short leg -> usually roughly washes. Acceptable.
- Long alt-A / Short alt-B: alt-A funding can spike 30-50%+ annualized during catalyst windows (ETF speculation, etc.); thinly traded alt-B can have ERRATIC or NEGATIVE funding (more shorts than longs). When that happens, you pay on BOTH legs simultaneously. This is the 'chew up' scenario.
- 30% annualized combined funding × 12-week hold = ~7% bleed before the trade moves at all. If your stop is -10%, funding alone can trigger it.

Time scaling:
- 2-day trade @ 20% annualized funding: ~0.1% cost. Negligible.
- 6-month trade @ 20% annualized funding: ~10% cost. Dominates R/R math.
- If targeting +10% to T1 over 1 quarter and paying net 5-10% in funding, gross +10% becomes net 0-5%. Thesis can be right and you barely break even.

MANDATORY checks for any perp trade you propose:
1. Use web_search to pull 30-day AVERAGE funding for each leg (Coinglass funding history page or Hyperliquid funding history). Do NOT trust spot/snapshot funding — it is noisy.
2. Estimate total carry cost for your stated holding period: (long_leg_funding_apr - short_leg_funding_apr) × days_held / 365. State this number explicitly in the TRADE PARAMETERS block as 'Est. funding carry: +/-X% over [N] weeks'.
3. If estimated carry cost is more than ~30% of your target return to T1, flag the trade as STRUCTURALLY COMPROMISED in WHY NOW, and either (a) reduce position size, (b) shorten the timeframe, or (c) propose expressing the view via dated futures (CME, Deribit) instead of perps — fixed cost of carry baked into basis, no surprise funding spikes.
4. For shorter holds (4-12 weeks), perps are usually fine; instruct the user in WHY NOW to 'monitor funding weekly and exit if funding inverts persistently against the trade'.
5. Watch for asymmetric funding as ALPHA: if one leg is paying 40%+ annualized to shorts during a crowded long, that itself is a contrarian short signal with a funding TAILWIND. Call this out explicitly when relevant.

This section applies only to perp/leveraged trades. For spot trades, dated futures, or non-crypto equity/index/FX trades, skip the funding analysis (note 'N/A — spot trade' or 'N/A — dated future, carry priced in basis').

INSTRUCTIONS:

Apply BOTH frameworks systematically to today's digest. Use the web_search tool to look up current prices, technicals (RSI, moving averages, support/resistance), {asset_data_hints}, and any macro data points referenced in the frameworks.

Produce 3-5 actionable trade ideas. For each trade:

TRADE [N]: [Long/Short] $TICKER — [one-line thesis]

MACRO CONTEXT
Apply the Macro Framework: What regime are we in (inflation vs growth quadrant)? What narratives are driving this? What does positioning/sentiment look like? What are the risks — why would someone sell this to you?

TECHNICAL/FUNDAMENTAL SETUP
Apply the {secondary_framework_label}: {secondary_application_hint}

TRADE PARAMETERS
- Direction: Long / Short
- Instrument: Perp / Spot / Dated future (be explicit — funding only applies to perps)
- Conviction: 1-10
- Risk: 1-4% (sized by conviction per the framework)
- Entry: specific level or range
- Stop: wide, not at obvious levels (per framework guidance)
- Targets: T1, T2, T3
- R:R ratio
- Timeframe
- Est. funding carry (perps only): +/-X% over the stated holding period, computed from 30-day avg funding on each leg via web_search. Write 'N/A — spot/dated' if not a perp.

WHY NOW
What catalyst drives the narrative shift? What signal grade is this (low/medium/high per the P72 framework)?

WHAT INVALIDATES
What information would come out against this thesis? At what point would you no longer put on the trade today?

After all trades, include:

PORTFOLIO OVERVIEW
How do these trades correlate? Are there spreads that hedge systemic risk? What is the aggregate risk exposure? Apply the "check for correlations between existing positions" guidance from the Trading Framework.

LAYMAN EXPLANATION
Pick 4-8 jargon terms from the trade ideas above that an experienced macro/equity investor with NO {jargon_decoder_target} background might not immediately grasp. For each:

TERM: <name>
[3-5 sentences: (1) what it is mechanically, (2) closest analogy from the reader's domain ({reader_familiar_domain}), (3) why it matters specifically to the trades above]

Quality bar example:
{jargon_example}

---SOURCES---
Numbered list. Each source: description, full URL from web searches or Telegram channel name from the digest."""


TRADE_IDEAS_CATEGORY_CONFIG = {
    "crypto": {
        "analyst_role": "macro-crypto analyst",
        "asset_class": "crypto assets (BTC, ETH, alts, perpetual futures, on-chain plays)",
        "secondary_framework_label": "CRYPTO TRADING FRAMEWORK",
        "secondary_framework": TRADING_FRAMEWORK,
        "secondary_application_hint": "What do longer time-frame S/R and trend lines show? RSI, 14/50/200d MA levels. What caused previous volatility at these levels? What is a similar period in history and what happened?",
        "asset_data_hints": "on-chain data, funding rates, open interest, ETF flows",
        "jargon_decoder_target": "crypto",
        "reader_familiar_domain": "TradFi — yield curves, P/E, DCF, credit spreads, options Greeks, duration, carry trades, 13F filings",
        "jargon_example": (
            "TERM: Funding Rate\n"
            "In perpetual futures (crypto's main derivative — contracts with no expiry), longs pay shorts "
            "(or vice versa) every 1-8 hours based on the gap between perp price and spot. It's a peer-to-peer transfer, not an exchange fee, and it scales LINEARLY with holding period. "
            "Closest TradFi analogy: the overnight repo rate or the carry cost of holding a futures position into delivery — it's the price of leverage. "
            "For pair trades you pay/receive on BOTH legs; signs rarely cancel cleanly, so on a multi-month RV pair an annualized 20-30% combined carry can quietly eat the entire target return before the trade moves. "
            "When funding spikes positive (e.g. 30-50%+ APR during catalyst windows), it's often a crowded-long contrarian top signal; deeply negative funding can flag capitulation or a funding TAILWIND on a contrarian short. "
            "For Trade [N], the current 30d avg funding rate of X% signals Y, and the est. carry over the stated hold is Z%."
        ),
    },
    "non-crypto": {
        "analyst_role": "macro/equity analyst",
        "asset_class": "TradFi assets (equities, equity indexes, rates, FX, commodities, credit)",
        "secondary_framework_label": "EQUITY/COMPANY FRAMEWORK",
        "secondary_framework": EQUITY_FRAMEWORK,
        "secondary_application_hint": "Apply the relevant section of the Equity Framework: for single names, do the business model + financials + valuation analysis; for indexes/rates/FX/commodities, apply the macro framework's asset-class-specific sub-section. Always include technicals (S/R, RSI, 14/50/200d MA) and a historical analogue.",
        "asset_data_hints": "COT positioning, ETF flows, options skew, earnings revisions, central bank commentary",
        "jargon_decoder_target": "deep TradFi micro",
        "reader_familiar_domain": "the reader's existing toolkit — keep this section brief if all terms are standard",
        "jargon_example": (
            "TERM: 2s10s Curve\n"
            "The spread between 2-year and 10-year Treasury yields. Inversion (2Y > 10Y) has historically preceded "
            "every US recession by 6-18 months. Mechanically: short rates are anchored by Fed policy expectations "
            "while long rates reflect growth + inflation expectations + term premium. A flattening/inverting curve "
            "signals the market expects the Fed to cut. For Trade [N], the current 2s10s level of X bps signals Y."
        ),
    },
}


def generate_trade_ideas(digest_text: str, digest_sources: str, category: str = "crypto", telegram_history: str = "") -> str:
    if not MACRO_FRAMEWORK:
        print("  Skipping trade ideas: macro framework not loaded")
        return ""

    cfg = TRADE_IDEAS_CATEGORY_CONFIG[category]
    if not cfg["secondary_framework"]:
        print(f"  Skipping {category} trade ideas: {cfg['secondary_framework_label']} not loaded")
        return ""

    prompt = TRADE_IDEAS_PROMPT.format(
        macro_framework=MACRO_FRAMEWORK,
        analyst_role=cfg["analyst_role"],
        asset_class=cfg["asset_class"],
        secondary_framework_label=cfg["secondary_framework_label"],
        secondary_framework=cfg["secondary_framework"],
        secondary_application_hint=cfg["secondary_application_hint"],
        asset_data_hints=cfg["asset_data_hints"],
        jargon_decoder_target=cfg["jargon_decoder_target"],
        reader_familiar_domain=cfg["reader_familiar_domain"],
        jargon_example=cfg["jargon_example"],
        digest_text=digest_text,
        digest_sources=digest_sources,
        telegram_history=telegram_history or "(no historical context available)",
    )

    messages = [{"role": "user", "content": prompt}]

    while True:
        resp = anthropic_client.messages.create(
            model=TRADE_IDEAS_MODEL,
            max_tokens=8000,
            thinking={"type": "adaptive"},
            tools=[SEARCH_TOOL],
            messages=messages,
        )

        print(f"  Trade ideas call: stop={resp.stop_reason}, usage=in:{resp.usage.input_tokens} out:{resp.usage.output_tokens}")

        if resp.stop_reason != "tool_use":
            break

        assistant_content = resp.content
        serializable = []
        tool_results = []
        for block in assistant_content:
            if block.type == "thinking":
                serializable.append({"type": "thinking", "thinking": block.thinking, "signature": block.signature})
            elif block.type == "tool_use":
                serializable.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
                query = block.input.get("query", "")
                print(f"    web_search: {query}")
                result = web_search(query)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
            elif block.type == "text":
                serializable.append({"type": "text", "text": block.text})

        messages.append({"role": "assistant", "content": serializable})
        messages.append({"role": "user", "content": tool_results})

    text_parts = [b.text for b in resp.content if b.type == "text"]
    return "\n".join(text_parts)


# ============================================================
# PDF rendering (fpdf2)
# ============================================================
import re as _re

_SECTION_HEADER_RE = _re.compile(
    r"^(TRADE\s+\d+:|PORTFOLIO OVERVIEW|TRADFI TRANSLATOR|MACRO CONTEXT|"
    r"TECHNICAL SETUP|TRADE PARAMETERS|WHY NOW|WHAT INVALIDATES|"
    r"[A-Z][A-Z\s/&]+:)\s*"
)


def _latin1(text: str) -> str:
    return text.encode("latin-1", "replace").decode("latin-1")


def safe_cell(pdf: FPDF, text: str, font: tuple | None = None):
    if not text or not text.strip():
        return
    if font:
        pdf.set_font(*font)
    pdf.set_x(pdf.l_margin)
    clean = _latin1(text.strip())
    try:
        pdf.multi_cell(w=pdf.epw, text=clean)
    except Exception:
        try:
            pdf.multi_cell(w=pdf.epw, text=clean[:100] + "...")
        except Exception:
            pdf.ln()


def render_trade_ideas_pdf(trade_text: str) -> str:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(w=pdf.epw, text=_latin1("CRYPTO TRADE IDEAS"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    for line in trade_text.split("\n"):
        stripped = line.strip()

        if not stripped:
            pdf.ln(2)
            continue

        if stripped.startswith("---") and len(stripped.replace("-", "").strip()) == 0:
            y = pdf.get_y()
            pdf.set_draw_color(180, 180, 180)
            pdf.line(pdf.l_margin, y, pdf.l_margin + pdf.epw, y)
            pdf.ln(4)
            continue

        if stripped.startswith("TRADE ") and ":" in stripped:
            pdf.ln(4)
            safe_cell(pdf, stripped, font=("Helvetica", "B", 12))
            pdf.ln(2)
            continue

        if _SECTION_HEADER_RE.match(stripped):
            pdf.ln(2)
            safe_cell(pdf, stripped, font=("Helvetica", "B", 10))
            continue

        if stripped.startswith("TERM:"):
            pdf.ln(2)
            safe_cell(pdf, stripped, font=("Helvetica", "BI", 10))
            continue

        safe_cell(pdf, stripped, font=("Helvetica", "", 9))

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    pdf.output(tmp.name)
    return tmp.name


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

    # Split verified posts into crypto / non-crypto buckets
    crypto_posts = [p for p in verified if is_crypto_sublist(p.get("sub_list", ""))]
    non_crypto_posts = [p for p in verified if not is_crypto_sublist(p.get("sub_list", ""))]
    print(f"\nBucket split: crypto={len(crypto_posts)}, non-crypto={len(non_crypto_posts)}")

    # Fetch 30-day history from existing digest groups once — reused for both trade idea calls
    print("\n=== Fetching Telegram digest history (30 days) ===")
    telegram_history = gather_telegram_history()
    print(f"  History total: {len(telegram_history)} chars")

    pin_first = True  # only pin the very first digest message of the run

    for category, bucket in [("crypto", crypto_posts), ("non-crypto", non_crypto_posts)]:
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
            sources_text = f"🔗 {title_upper} SOURCES\n" + sources.strip()
            for src_chunk in chunk_for_tg(sources_text):
                tg_send_message(src_chunk, disable_notification=True)
                time.sleep(0.5)

        if pin_first and first_msg_id:
            tg_pin_message(first_msg_id)
            print(f"  Pinned digest message {first_msg_id}")
            pin_first = False

        # ---- Stage 2b: Trade ideas PDF for this category ----
        print(f"\n=== Stage 2b [{category}]: Trade Ideas (Opus + frameworks + web search) ===")
        t2 = time.time()
        trade_text = generate_trade_ideas(body, sources, category=category, telegram_history=telegram_history)
        print(f"  done in {time.time() - t2:.1f}s, text = {len(trade_text)} chars")

        if not trade_text.strip():
            print(f"  No trade ideas generated for {category} — skipping PDF")
            continue

        print(f"=== Stage 3b [{category}]: Telegram PDF ===")
        pdf_path = render_trade_ideas_pdf(trade_text)
        print(f"  PDF rendered: {pdf_path}")
        try:
            caption = f"{header_emoji} {cfg['title']} Trade Ideas — {label}"
            resp = tg_send_file(pdf_path, caption=caption)
            pdf_msg_id = resp["result"]["message_id"]
            print(f"  PDF posted: message {pdf_msg_id}")
        finally:
            try:
                os.unlink(pdf_path)
            except OSError:
                pass

    # Final verification footer (once per run)
    footer = (
        f"✅ {len(verified)} posts verified (HTTP-checked URLs, handle-matched). "
        f"{len(rejected)} candidates rejected."
    )
    tg_send_message(footer, disable_notification=True)

    print("\nDone.")


if __name__ == "__main__":
    main()
