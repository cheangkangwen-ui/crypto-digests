# crypto-digests

Monorepo for two Telegram digest bots. Full commit history of both original repos is preserved.

| Bot | Folder | Feeds | Schedule (SGT) | Workflow |
|---|---|---|---|---|
| Crypto Signal Digest | `signal/` | "crypto signal digest" chat — synthesizes posts from ~100 high-signal X handles via Grok x_search + Claude | Daily 8am | `.github/workflows/signal-digest.yml` |
| Crypto News Digest | `news/` | "crypto digest" chat — summarizes Telegram crypto-news channels via Claude | Weekdays 8am & 8pm | `.github/workflows/news-digest.yml` |

Both digests enforce a hard length budget (body fits in 2 Telegram messages), attribute every price move to its driver, and keep a full-depth Jargon Decoder for readers with a TradFi background.

## Required Actions secrets

| Secret | Used by |
|---|---|
| `ANTHROPIC_API_KEY` | both |
| `XAI_API_KEY` | signal |
| `TELEGRAM_BOT_TOKEN` | signal |
| `TELEGRAM_CHAT_ID` | signal |
| `TELEGRAM_API_ID` | news |
| `TELEGRAM_API_HASH` | news |
| `TELEGRAM_SESSION_GHA` | news (Telethon session string; regenerate with `signal/gen_session.py` pattern) |

## Manual runs

Both workflows support `workflow_dispatch` — trigger from the Actions tab.
