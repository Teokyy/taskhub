# TaskHub — marketplace MVP (pre-alpha)

TaskHub is being rebuilt around a public task marketplace. This repository is in **pre-alpha**; do not treat the current deployment as a stable beta.

## Product flow
- Customer publishes a task in a two-level category.
- Executors send responses; a customer accepts one response.
- Acceptance creates a private chat for the two deal participants.
- Customer confirms completion; both parties can leave one review.
- Either party can open a dispute; moderators can inspect the full conversation.
- Category subscriptions and deal notifications are delivered through the Telegram bot.

## Payments
The customer and executor settle directly. TaskHub does not hold funds, escrow, or process payouts in this MVP.

## Out of scope
Provider service catalog, repeatable services/tasks, escrow, internal marketplace payouts, chat attachments, and weighted reputation.

## Railway deployment
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Required variables: `BOT_TOKEN`, `PUBLIC_URL` (HTTPS URL), `ADMIN_TELEGRAM_IDS`.
- Optional: `MODERATOR_TELEGRAM_IDS` (comma-separated Telegram IDs), `DEV_MODE=false`.
- Attach a Railway Volume at `/data` to persist SQLite.
- The bot runs in the same service via polling when `BOT_TOKEN` and `PUBLIC_URL` are configured. Run one app instance for polling and the hourly digest worker.

## Local checks
From `taskhub_release`:
- `python -m compileall -q app`
- `python -m unittest discover -s tests -v`
- `node --check app/static/app.js`

## Important
Open the Mini App through the Telegram bot so Telegram initData can be verified. Directly opening the domain in a normal browser does not authenticate a user.

A feature is not considered complete just because its API route or UI button exists. It must pass end-to-end tests with separate customer, executor, and moderator accounts.
