import os
import sys
import asyncio
import tempfile
import anthropic

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import Channel
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.functions.channels import CreateChannelRequest
from datetime import datetime, timezone, timedelta

def _clean(val):
    return val.strip().replace("﻿", "").replace("￾", "")

TELEGRAM_API_ID = int(_clean(os.environ["TELEGRAM_API_ID"]))
TELEGRAM_API_HASH = _clean(os.environ["TELEGRAM_API_HASH"])
TELEGRAM_SESSION = _clean(os.environ.get("TELEGRAM_SESSION", "crypto_session"))
ANTHROPIC_API_KEY = _clean(os.environ.get("ANTHROPIC_API_KEY", ""))
CRYPTO_GROUP_NAME = os.environ.get("CRYPTO_GROUP_NAME", "🪙 Crypto Digest")
FOLDER_NAME = "Crypto"

TG_SEMAPHORE = 30

SEARCH_TOOL = {
    "name": "web_search",
    "description": (
        "Search the web for current crypto news, token info, protocol details, or market context. "
        "Use this when you need to look up a token, protocol, or topic that is unclear from the messages alone."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query"}
        },
        "required": ["query"],
    },
}


def web_search(query: str, max_results: int = 6) -> str:
    from duckduckgo_search import DDGS
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return "No results found."
        return "\n\n".join(
            f"**{r['title']}**\n{r['href']}\n{r['body']}" for r in results
        )
    except Exception as e:
        return f"Search failed: {e}"


def get_time_window():
    """Clock-based window: runs at UTC 1,7,13,19 daily (6h windows).
    Computes previous scheduled run time from the cron schedule."""
    now_utc = datetime.now(timezone.utc)
    myt = timezone(timedelta(hours=8))
    current_hour = now_utc.hour

    schedule_hours = [0, 12]
    prev_hours = [h for h in schedule_hours if h < current_hour]
    if prev_hours:
        prev_run_utc = now_utc.replace(hour=max(prev_hours), minute=0, second=0, microsecond=0)
    else:
        prev_run_utc = (now_utc - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)

    label = f"{prev_run_utc.astimezone(myt).strftime('%H:%M')} - {now_utc.astimezone(myt).strftime('%H:%M')} MYT"
    return prev_run_utc, now_utc, label


async def get_or_create_group(tg, name, about):
    dialogs = await tg.get_dialogs()
    for d in dialogs:
        if d.name == name and getattr(d.entity, "megagroup", False):
            return d.entity
    result = await tg(CreateChannelRequest(
        title=name,
        about=about,
        megagroup=True,
    ))
    return result.chats[0]


async def fetch_channel(tg, dialog, start_utc, now_utc, sem):
    async with sem:
        messages = []
        try:
            async for m in tg.iter_messages(dialog, offset_date=now_utc):
                if not m.date:
                    continue
                if m.date < start_utc:
                    break
                if m.text:
                    messages.append(m)
        except Exception:
            pass
        return messages


async def main():
    ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    session = StringSession(TELEGRAM_SESSION) if len(TELEGRAM_SESSION) > 20 else TELEGRAM_SESSION
    tg = TelegramClient(session, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await tg.connect()

    if not await tg.is_user_authorized():
        await tg.disconnect()
        raise Exception("Not authorized.")

    try:
        crypto_group = await get_or_create_group(tg, CRYPTO_GROUP_NAME, "Automated crypto news digests")

        if not os.environ.get("SKIP_DUPLICATE_CHECK"):
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
            async for msg in tg.iter_messages(crypto_group, limit=3):
                if msg.date and msg.date >= cutoff and msg.text and "CRYPTO DIGEST" in msg.text:
                    print("Crypto digest already sent in last 5 minutes. Skipping.")
                    return

        start_utc, now_utc, label = get_time_window()

        print(f"\n{'='*70}")
        print(f"  CRYPTO NEWS ANALYSIS  |  Window: {label}")
        print(f"{'='*70}\n")

        filters_result = await tg(GetDialogFiltersRequest())
        filter_list = filters_result.filters if hasattr(filters_result, 'filters') else filters_result

        def folder_title(f):
            t = getattr(f, 'title', None)
            if t is None:
                return None
            return t.text if hasattr(t, 'text') else str(t)

        crypto_folder = next(
            (f for f in filter_list if folder_title(f) == FOLDER_NAME), None
        )
        if not crypto_folder:
            print(f"ERROR: '{FOLDER_NAME}' folder not found in Telegram.")
            return

        folder_peer_ids = {
            p.channel_id for p in crypto_folder.include_peers if hasattr(p, 'channel_id')
        }
        print(f"  '{FOLDER_NAME}' folder contains {len(folder_peer_ids)} channel(s).")

        dialogs = await tg.get_dialogs()
        channels = [
            d for d in dialogs
            if isinstance(d.entity, Channel) and d.entity.id in folder_peer_ids
        ]

        active_channels = [
            d for d in channels
            if d.message and d.message.date and d.message.date >= start_utc
        ]
        print(f"  {len(channels)} channels in folder, {len(active_channels)} posted in window. Fetching...\n")

        tg_sem = asyncio.Semaphore(TG_SEMAPHORE)
        tasks = [fetch_channel(tg, d, start_utc, now_utc, tg_sem) for d in active_channels]
        results = await asyncio.gather(*tasks)

        all_messages = []
        for dialog, messages in zip(active_channels, results):
            if not messages:
                continue
            channel_block = f"### {dialog.name}\n" + "\n".join(
                f"[{m.date.astimezone().strftime('%H:%M')}] {m.text[:400]}"
                for m in reversed(messages)
            )
            all_messages.append(channel_block)

        print(f"  Got messages from {len(all_messages)} channels.")

        if not all_messages:
            print("  No messages found in window.")
            return

        raw_dump = "\n\n".join(all_messages)

        prompt = f"""You are a crypto analyst explaining crypto news to a layman. Below are raw messages from {len(all_messages)} Telegram channels in a "Crypto" folder, covering {label}.

For each significant story, use the web_search tool to look up the token, protocol, or context if you are unsure about what it is or why it matters. Only search where context would meaningfully improve the explanation.

IMPORTANT: The digest section (stories + summary) should be concise — aim for under 3500 characters. But the Jargon Decoder section can be as long as needed to properly educate.

Produce a digest in this exact format:

🪙 CRYPTO DIGEST | {label}

Group stories by category. For each token/story, use this COMPACT format:
$<TICKER> — <one-line news headline>
↳ Why it matters: <1-2 sentences, plain English, no jargon without explanation>
↳ Sentiment: Bullish/Bearish/Neutral

Categories (skip any with no stories): Layer 1s/Layer 2s | DeFi/DEXs | Memecoins/NFTs | Regulation/Policy | Market/Macro

SUMMARY: 2-3 sentences on key themes across all categories, written for a layman.

JARGON DECODER:
After the summary, add a section titled "📖 JARGON DECODER". The reader is an experienced macro and equity fundamental investor — they deeply understand yield curves, P/E, DCF, central bank policy, credit spreads, options Greeks, duration, carry trades, flow analysis, 13F filings, etc. They do NOT have crypto background.

For each crypto-specific term that appeared in this digest, write a THOROUGH explanation (3-5 sentences) that:
1. First explains what it is in crypto — how it works mechanically
2. Then maps it to the closest TradFi analogy so the reader can anchor it
3. Then explains why it matters / how to think about it as an investor

Format each entry as:
📌 <TERM>
<3-5 sentence explanation with TradFi mapping>

Example of the depth expected:
📌 Funding Rate
In crypto perpetual futures (futures that never expire), the funding rate is a periodic payment between longs and shorts that keeps the perp price anchored to spot. When funding is positive, longs pay shorts — meaning the market is net long and willing to pay for leverage. Think of it like the cost of carry on a futures contract, but settled every 8 hours instead of at expiry. As a signal, extreme positive funding is like seeing very high short interest costs in equities — it means crowded positioning and vulnerability to a squeeze or flush. Traders watch funding as a real-time sentiment and positioning indicator, similar to how you'd watch put/call ratios or COT data.

📌 Bridge Exploit
A bridge is the infrastructure that lets you move tokens between two separate blockchains (e.g., Ethereum to Polygon). Think of it like a correspondent bank or clearinghouse — you deposit assets on one chain, the bridge locks them, and mints equivalent tokens on the other chain. A bridge exploit means an attacker found a vulnerability in this custodial intermediary and drained the locked funds — equivalent to a clearinghouse breach. These are among the highest-severity crypto incidents because bridges hold concentrated pools of assets, similar to how a CCP failure would ripple across markets.

Include 4-8 terms. Only include terms that actually appear in TODAY's digest. Skip terms the reader likely already knows (BTC, ETH, bull/bear, market cap, wallet).

Rules:
- Merge duplicate stories across channels into one entry
- Skip channels with no crypto-relevant content
- Layer 1s/Layer 2s always comes first
- Omit sections with no relevant stories — do not write "[No significant stories]"
- Maximum 10-12 entries total — prioritise the most impactful
- Use $ prefix for tickers (e.g. $BTC, $ETH, $SOL)
- Include price moves when mentioned in the source messages

After the digest (including the jargon decoder), output this EXACT line on its own:
---SOURCES---

Then list source references as numbered items. For each token/claim, cite the Telegram channel name it came from. For web searches, include the URL. Format:
1. [Ticker or topic] — Channel Name or URL
2. ...

RAW MESSAGES:
{raw_dump}"""

        print("  Sending to Opus for analysis...\n")

        loop = asyncio.get_event_loop()

        def _call():
            messages = [{"role": "user", "content": prompt}]
            while True:
                response = ai_client.messages.create(
                    model="claude-opus-4-7",
                    max_tokens=6000,
                    thinking={"type": "adaptive"},
                    tools=[SEARCH_TOOL],
                    messages=messages,
                )
                if response.stop_reason != "tool_use":
                    return response
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        print(f"  [web search] {block.input['query']}")
                        result = web_search(block.input["query"])
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})

        response = await loop.run_in_executor(None, _call)

        digest_text = ""
        for block in response.content:
            if block.type == "text":
                digest_text = block.text.strip()
                for line in digest_text.split("\n"):
                    print(f"  {line}")

        if digest_text:
            print("\n  Sending to Telegram...")

            if "---SOURCES---" in digest_text:
                body, sources = digest_text.split("---SOURCES---", 1)
                body = body.strip()
                sources = sources.strip()
            else:
                body = digest_text
                sources = ""

            full_text = body
            chunk_size = 4000
            chunks = []
            while len(full_text) > chunk_size:
                split_at = full_text.rfind("\n", 0, chunk_size)
                if split_at == -1:
                    split_at = chunk_size
                chunks.append(full_text[:split_at])
                full_text = full_text[split_at:].lstrip("\n")
            if full_text:
                chunks.append(full_text)
            first_msg = None
            for i, chunk in enumerate(chunks):
                if len(chunks) > 1:
                    chunk = f"[{i+1}/{len(chunks)}]\n\n" + chunk
                sent = await tg.send_message(crypto_group, chunk)
                if i == 0:
                    first_msg = sent
                await asyncio.sleep(0.5)

            if sources:
                sources_msg = f"🔗 SOURCES\n{'='*40}\n\n{sources}"
                await asyncio.sleep(0.5)
                await tg.send_message(crypto_group, sources_msg)

            if first_msg:
                await tg.pin_message(crypto_group, first_msg.id, notify=False)
            print(f"  Sent {len(chunks)} digest message(s)" + (" + 1 sources message." if sources else "."))

        print(f"\n{'='*70}")
        print("  Done.")
        print(f"{'='*70}\n")

    finally:
        await tg.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
