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
from fpdf import FPDF

def _clean(val):
    return val.strip().replace("﻿", "").replace("￾", "")

TELEGRAM_API_ID = int(_clean(os.environ["TELEGRAM_API_ID"]))
TELEGRAM_API_HASH = _clean(os.environ["TELEGRAM_API_HASH"])
TELEGRAM_SESSION = _clean(os.environ.get("TELEGRAM_SESSION", "crypto_session"))
ANTHROPIC_API_KEY = _clean(os.environ.get("ANTHROPIC_API_KEY", ""))
CRYPTO_GROUP_NAME = os.environ.get("CRYPTO_GROUP_NAME", "🪙 Crypto Digest")
TRADE_IDEAS_GROUP_NAME = os.environ.get("TRADE_IDEAS_GROUP_NAME", "📊 Crypto Trade Ideas")
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


MACRO_FRAMEWORK = """MACRO ANALYSIS FRAMEWORK
- Narratives: What major narratives/themes are driving crypto? Sustainable? What's next?
- Growth regime: Inflationary boom/bust vs disinflationary boom/bust — which quadrant are we in?
- Monetary policy: Rate decisions, CB stance, liquidity conditions, M2 trends
- Fiscal policy: Government spending, stablecoin regulation, crypto-specific legislation
- Positioning & flows: On-chain flows, exchange inflows/outflows, stablecoin supply, funding rates
- Sentiment: Fear & Greed index, social sentiment, are people fading or chasing?
- Risk: What could go wrong? Why would someone sell this to you?
- Catalyst: What will drive the next narrative shift?"""

TRADING_FRAMEWORK = """TRADE EXECUTION FRAMEWORK
- Technicals: Key support/resistance, RSI, 50/200d MA, trend structure
- Sizing: 1-4% risk per trade, confidence-rated. Wide stops, not at obvious levels.
- Entry structure: DCA below intrinsic value, or wait for confirmation?
- Risk-reward: Asymmetric setups only. What's the 5:1 or better?
- Correlations: Check correlation with BTC, ETH, and existing positions
- Historical playbook: When did we last see this setup? What happened?
- Signal grading: Low/medium/high grade signal — what data would upgrade it?
- Monitoring: What invalidates the thesis? When to cut?"""


async def generate_trade_ideas_pdf(ai_client, digest_text, label):
    trade_prompt = f"""You are a crypto trading analyst. Below is today's crypto digest and two analytical frameworks (Macro and Trading).

Your job: produce 3-5 ACTIONABLE trade ideas based on the digest news, structured using both frameworks.

{MACRO_FRAMEWORK}

{TRADING_FRAMEWORK}

For each trade idea, use this structure:

TRADE [N]: [Long/Short] $TICKER — [one-line thesis]

MACRO CONTEXT:
- Current regime and where this asset fits
- Key macro tailwinds/headwinds
- Positioning and flow context

TECHNICAL SETUP:
- Key levels (support, resistance, current price if known)
- Trend structure and momentum
- Entry zone and stop loss level

TRADE PARAMETERS:
- Direction: Long/Short
- Conviction: [1-10]
- Suggested risk: [1-4]% of portfolio
- Entry: [price zone or condition]
- Stop loss: [level or % from entry]
- Target(s): [T1, T2 with rationale]
- Risk/Reward: [ratio]
- Timeframe: [days/weeks/months]

WHY NOW:
- What catalyst from today's news makes this actionable?
- What's the historical playbook?

WHAT INVALIDATES:
- Specific conditions that kill the thesis

---

End with a PORTFOLIO OVERVIEW section:
- Net positioning bias (long/short/neutral)
- Correlation check across all ideas
- Key macro risk to monitor

Use the web_search tool to look up current prices, technicals, or on-chain data for any token you include. Every trade idea MUST have current price context.

TODAY'S DIGEST:
{digest_text}"""

    def _call():
        messages = [{"role": "user", "content": trade_prompt}]
        while True:
            response = ai_client.messages.create(
                model="claude-opus-4-7",
                max_tokens=8000,
                thinking={"type": "adaptive"},
                tools=[SEARCH_TOOL],
                messages=messages,
            )
            if response.stop_reason != "tool_use":
                return response
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"  [trade ideas search] {block.input['query']}")
                    result = web_search(block.input["query"])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

    print("  Generating actionable trade ideas...")
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(None, _call)

    trade_text = ""
    for block in response.content:
        if block.type == "text":
            trade_text = block.text.strip()

    if not trade_text:
        print("  No trade ideas generated.")
        return None

    for line in trade_text.split("\n"):
        print(f"  {line}")

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, "CRYPTO TRADE IDEAS", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 8, label, new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(6)

    def safe_cell(pdf, h, txt, font=None):
        if font:
            pdf.set_font(*font)
        pdf.set_x(pdf.l_margin)
        if not txt.strip():
            return
        try:
            pdf.multi_cell(w=pdf.epw, h=h, text=txt)
        except Exception:
            try:
                pdf.multi_cell(w=pdf.epw, h=h, text=txt[:100])
            except Exception:
                pdf.ln(h)

    pdf.set_font("Helvetica", "", 9)
    for line in trade_text.split("\n"):
        clean = line.encode("latin-1", "replace").decode("latin-1")
        if line.startswith("TRADE ") and ":" in line:
            pdf.ln(4)
            safe_cell(pdf, 6, clean, ("Helvetica", "B", 12))
            pdf.set_font("Helvetica", "", 9)
        elif line.startswith("PORTFOLIO OVERVIEW"):
            pdf.ln(4)
            safe_cell(pdf, 6, clean, ("Helvetica", "B", 12))
            pdf.set_font("Helvetica", "", 9)
        elif line.endswith(":") and line.isupper():
            pdf.ln(2)
            safe_cell(pdf, 6, clean, ("Helvetica", "B", 10))
            pdf.set_font("Helvetica", "", 9)
        elif line.strip() == "---":
            pdf.ln(3)
            pdf.set_draw_color(180, 180, 180)
            pdf.line(pdf.get_x(), pdf.get_y(), pdf.get_x() + pdf.epw, pdf.get_y())
            pdf.ln(3)
        elif line.strip():
            safe_cell(pdf, 5, clean)
        else:
            pdf.ln(3)

    myt = timezone(timedelta(hours=8))
    date_str = datetime.now(myt).strftime("%Y-%m-%d")
    pdf_path = os.path.join(tempfile.gettempdir(), f"crypto_trade_ideas_{date_str}.pdf")
    pdf.output(pdf_path)
    print(f"  PDF saved: {pdf_path}")
    return pdf_path


def get_time_window():
    """Clock-based window: runs at UTC 1,7,13,19 daily (6h windows).
    Computes previous scheduled run time from the cron schedule."""
    now_utc = datetime.now(timezone.utc)
    myt = timezone(timedelta(hours=8))
    current_hour = now_utc.hour

    schedule_hours = [1, 7, 13, 19]
    prev_hours = [h for h in schedule_hours if h < current_hour]
    if prev_hours:
        prev_run_utc = now_utc.replace(hour=max(prev_hours), minute=0, second=0, microsecond=0)
    else:
        prev_run_utc = (now_utc - timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)

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

IMPORTANT: Keep the entire digest CONCISE — aim for under 3500 characters total (excluding sources). Be dense and informative, not verbose.

Produce a digest in this exact format:

🪙 CRYPTO DIGEST | {label}

Group stories by category. For each token/story, use this COMPACT format:
$<TICKER> — <one-line news headline>
↳ Why it matters: <1-2 sentences, plain English, no jargon without explanation>
↳ Sentiment: Bullish/Bearish/Neutral

Categories (skip any with no stories): Layer 1s/Layer 2s | DeFi/DEXs | Memecoins/NFTs | Regulation/Policy | Market/Macro

SUMMARY: 2-3 sentences on key themes across all categories, written for a layman.

Rules:
- Merge duplicate stories across channels into one entry
- Skip channels with no crypto-relevant content
- Layer 1s/Layer 2s always comes first
- Omit sections with no relevant stories — do not write "[No significant stories]"
- Maximum 10-12 entries total — prioritise the most impactful
- Use $ prefix for tickers (e.g. $BTC, $ETH, $SOL)
- Include price moves when mentioned in the source messages

After the digest, output this EXACT line on its own:
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
                    max_tokens=4000,
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

            print("\n  --- GENERATING TRADE IDEAS PDF ---\n")
            pdf_path = await generate_trade_ideas_pdf(ai_client, body, label)
            if pdf_path:
                trade_group = await get_or_create_group(tg, TRADE_IDEAS_GROUP_NAME, "Actionable crypto trade ideas")
                sent_pdf = await tg.send_file(
                    trade_group,
                    pdf_path,
                    caption="📊 Actionable Trade Ideas — see attached PDF",
                )
                await tg.pin_message(trade_group, sent_pdf.id, notify=False)
                print(f"  PDF sent to '{TRADE_IDEAS_GROUP_NAME}' group.")
                try:
                    os.remove(pdf_path)
                except OSError:
                    pass

        print(f"\n{'='*70}")
        print("  Done.")
        print(f"{'='*70}\n")

    finally:
        await tg.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
