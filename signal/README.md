# Crypto Signal Digest

Local Windows pipeline that scrapes 100 high-signal crypto X handles via Playwright (your logged-in X session), synthesizes a 5-section digest via Claude Opus 4.7, and posts to a Telegram supergroup via bot. Runs 4x daily via Windows Task Scheduler.

## Why local Windows

X aggressively blocks scraping from datacenter IPs (GitHub Actions, AWS, Azure). Running from your home ASUS bypasses this — your residential IP isn't flagged, and Playwright uses your real X session cookies.

## Architecture

```
Windows Task Scheduler (4x daily — SGT 9am/3pm/9pm/3am, UTC 1/7/13/19)
        ↓
run.ps1
        ↓
.venv\Scripts\python.exe  read_crypto_signals.py
   ├── Stage 1: Playwright + Chromium + x_cookies.json → scrape 100 handles (~5min)
   ├── Stage 2: Claude Opus 4.7 (adaptive thinking) → 5-section digest
   └── Stage 3: Telegram bot API → chunked post + pin
```

## Files

- `read_crypto_signals.py` — main script (~440 lines)
- `handles.csv` — 100 curated handles (DeFi 37 / Trading 30 / Macro 15 / Other 11 / Infra-Builders 7)
- `requirements.txt` — anthropic, requests, playwright
- `install.ps1` — one-shot installer (venv, deps, Chromium, .env prompts, scheduled tasks)
- `run.ps1` — invoked by Task Scheduler; runs the script and logs to `logs\`
- `uninstall.ps1` — removes scheduled tasks
- `x_cookies.json` — your X session cookies (NOT in git; export from Cookie-Editor)
- `.env` — secrets (NOT in git)
- `logs\` — per-run logs

## Setup (first time)

### 1. Clone the repo on your ASUS

```powershell
cd $HOME
git clone https://github.com/cheangkangwen-ui/crypto-signal-digest.git
cd crypto-signal-digest
```

### 2. Export X cookies

1. Install [Cookie-Editor extension](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm) in Comet/Chrome
2. Open x.com (logged in as @kangawo1)
3. Click Cookie-Editor icon → **Export → Export as JSON**
4. Save the file as `x_cookies.json` in this folder

### 3. Run installer

```powershell
.\install.ps1
```

It will:
- Create a Python venv in `.venv\`
- Install dependencies + Playwright Chromium
- Prompt for `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` → writes to `.env`
- Register 4 Windows Scheduled Tasks at SGT 9am/3pm/9pm/3am

### 4. Smoke test

```powershell
.\run.ps1
```

Watch the output; should print scraping progress, Claude synth, then post to Telegram. Logs land in `logs\YYYY-MM-DD_HH-MM.log`.

## Operating

### List scheduled tasks
```powershell
schtasks /Query /TN CryptoSignalDigest-0900
```

### Run a one-shot ad-hoc (skip duplicate guard)
```powershell
$env:SKIP_DUPLICATE_CHECK = "1"
.\run.ps1
Remove-Item Env:\SKIP_DUPLICATE_CHECK
```

### Debug a small handle subset
```powershell
$env:DEBUG_HANDLE_LIMIT = "3"
$env:SKIP_DUPLICATE_CHECK = "1"
.\run.ps1
```

### Refresh X cookies (when they expire — ~6 months)
1. Re-export from Cookie-Editor (same steps as setup)
2. Overwrite `x_cookies.json`
3. Next scheduled run picks it up automatically

### Remove scheduled tasks
```powershell
.\uninstall.ps1
```

## Phase 1 lessons baked in

- **BOM cleaning** on all env vars (PowerShell injects UTF-16 BOM)
- **UTF-8 stdout** via `PYTHONIOENCODING` and `sys.stdout.reconfigure`
- **Chunking** at 4000 chars on newline boundaries, numbered `[N/M]`
- **Duplicate guard** with `SKIP_DUPLICATE_CHECK=1` escape hatch
- **Clock-based 6-hour windows** (UTC 1/7/13/19)
- **Health alert** to Telegram if scraper returns 0 posts (cookie expiry detection)

## Cost

- X scraping: free (your home IP, your cookies)
- Claude Opus 4.7 synthesis: ~$0.30-0.50/run = ~$60/month at 4x daily
- Telegram bot: free
- **Total: ~$60/month**

## Troubleshooting

**"Scraped 0 posts" in logs + Telegram health alert:**
- Cookies expired or session invalidated → re-export `x_cookies.json`
- X DOM changed → check `logs\*.log` for `DEBUG` lines showing page title/body

**Tasks not running:**
- Check Task Scheduler GUI: `Win+R` → `taskschd.msc` → look for `CryptoSignalDigest-*`
- Tasks only run when your ASUS is on/awake. For 3am SGT, you need to either: leave it on overnight, OR remove the 0300 task with `schtasks /Delete /TN CryptoSignalDigest-0300 /F`

**"Python not found":**
- Install Python 3.10+ from [python.org/downloads](https://www.python.org/downloads/) and check "Add to PATH"

## Security

- `.env`, `x_cookies.json`, `.last_run_marker`, and `logs/` are in `.gitignore` — never committed
- Cookies are equivalent to your X password until they expire — keep `x_cookies.json` local
- Rotate cookies by logging out of x.com → log back in → re-export
- Bot token and API keys: rotate via their respective consoles if compromised
