# TaskHub local starter

## 1. Requirements
- Python 3.11+ installed
- Telegram bot token (create with BotFather; if an old token was exposed, revoke it and use a new one)

## 2. Setup
1. Copy `.env.example` to `.env`.
2. Put your NEW bot token into `BOT_TOKEN`.
3. Put your Telegram numeric ID in `ADMIN_TELEGRAM_IDS`.
4. Keep `DEV_MODE=true` for local testing; this provides a mock user in browser.

## 3. Start the web app
Run `run_server.bat`. Open http://127.0.0.1:8000 .

## 4. Telegram Mini App
Telegram requires an HTTPS URL. Use a tunnel (for example Cloudflare Tunnel or ngrok) pointing to port 8000. Put the tunnel HTTPS URL into `PUBLIC_URL` in `.env`, restart server and bot, then use `/start` in your bot.

## 5. Start bot
Run `run_bot.bat` in a second terminal after setting `BOT_TOKEN` and `PUBLIC_URL`.

This is a starter UI and authentication scaffold only; task management, finance, admin actions and real data flows are not implemented yet.
