# Crypto Signal Digest

Phase 2 of the crypto digest project. Reads posts from 100 high-signal X handles
(5 themed sub-lists curated on @kangawo1) via Grok's Live Search (`x_search`),
synthesizes a 5-section digest, posts to a Telegram group via bot.

## Architecture

```
Cloudflare Worker cron (1,7,13,19 UTC)
        ↓ workflow_dispatch
GitHub Actions
        ↓
read_crypto_signals.py
   ├── Load 100 handles from handles.csv (DeFi 37 / Trading 30 / Macro 15 / Other 11 / Infra 7)
   ├── Batch into ≤10 handles per Grok call (~11 calls/run)
   ├── Stage 1: per-sub-list signal collection (Grok with x_search + date window)
   ├── Stage 2: synthesis into 5-section digest (Top Stories / Market Snapshot / Trade Ideas / Narrative Sustainability / By Sub-List)
   └── Post to Telegram via bot, pin first message
```

## Required GitHub Secrets

- `GROK_API_KEY` — xAI API key
- `TELEGRAM_BOT_TOKEN` — from @BotFather
- `TELEGRAM_CHAT_ID` — destination group chat ID (negative number for groups)

## Schedule

4x daily, UTC 1 / 7 / 13 / 19 = SGT 9am / 3pm / 9pm / 3am
Clock-based 6-hour windows.

## Files

- `read_crypto_signals.py` — main script
- `handles.csv` — 100 curated handles with sub-list labels
- `requirements.txt` — Python deps (openai, requests, fpdf2, pandas)
- `.github/workflows/digest.yml` — GHA workflow (workflow_dispatch only)
- `.gitignore` — excludes sessions, env files, gen_session.py
