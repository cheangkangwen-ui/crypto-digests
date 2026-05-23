# Crypto Signal Digest

Daily digest of high-signal posts from 100 curated X handles → posted to Telegram supergroup.

## Architecture

```
GitHub Actions (cron 00:00 UTC = 08:00 SGT)
  └─> read_crypto_signals.py
        ├─ Stage 1: single Grok x_search call (model: grok-4-fast)
        │           over all 100 handles for last 24h
        ├─ Stage 2: Claude Sonnet synthesizes structured digest
        └─ Stage 3: Telegram bot posts + pins
```

## Triggers

- **Auto**: every day at 08:00 SGT
- **Manual**: GitHub repo → **Actions** tab → **Crypto Signal Digest** → **Run workflow**

## Required GitHub Secrets

| Secret | What |
|--------|------|
| `XAI_API_KEY` | xAI API key (get from console.x.ai) |
| `ANTHROPIC_API_KEY` | Anthropic key for Claude synth |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Negative integer for supergroup, e.g. `-1003973591075` |

## Cost

- Grok (`grok-4-fast` x_search, all 100 handles in 1 call): ~$1-2/run
- Claude Sonnet 4.5 synth: ~$0.10-0.20/run
- Telegram: free
- **~$30-60/month** for 1×/day + occasional manual runs

To lower further: edit `handles.csv` and remove low-signal accounts.

## Files

- `read_crypto_signals.py` — main script
- `handles.csv` — 100 X handles with sub-list labels
- `requirements.txt` — `anthropic`, `requests`
- `.github/workflows/daily-digest.yml` — schedule + manual trigger

## Digest format

5 sections + sources:
1. **TOP STORIES** — 3-5 cross-handle narratives
2. **MARKET SNAPSHOT** — BTC/ETH/alts + sentiment
3. **TRADE IDEAS** — concrete setups (or "no concrete setups posted")
4. **NARRATIVE SUSTAINABILITY** — gaining vs fading themes
5. **BY SUB-LIST** — 1-2 signals per DeFi/Trading/Macro/Other/Infra-Builders
6. **SOURCES** — numbered cited handles + URLs

Reader profile baked into prompt: macro/equity investor with no crypto background. Jargon gets TradFi analogies on first use.
