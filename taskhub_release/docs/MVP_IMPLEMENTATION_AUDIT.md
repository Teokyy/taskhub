# MVP implementation audit — 2026-11-26

Stage: pre-alpha. This is a repository code audit; live Telegram and production database have not been tested.

## Found in repository
- Legacy room/team task workflows and a public task feed with direct claiming, submission review and internal balance logic.
- New additive DB schema: categories, category_requests, responses, chats, messages, task_history, reviews, subscriptions, achievements, notifications.
- Seeded two-level category taxonomy.
- Category listing and admin create/update/hide/move endpoints; category-request resolution endpoint.
- Marketplace response create/list/withdraw/accept endpoints.
- Private chat list, read and send endpoints.
- Deal completion, reviews and disputes endpoints.
- Frontend first-pass response actions, chat view, category selection and subscriptions.
- Legacy public direct-claim and submit/review routes return 410 to prevent bypassing response selection.

## Still missing / incomplete
- Bot onboarding quest, persistent steps, bot commands /tasks, /mine, /chats, /menu, /quest, /skip.
- Hourly digest worker and notification deduplication.
- Online presence and 10-minute suppression for chat message notifications.
- Hide-for-self message endpoint.
- Moderator dispute queue and full transcript UI.
- Moderator category management/request queue UI.
- Mutual-consent cancellation after an accepted response.
- Executor withdrawal after selection without penalty.
- Review aggregates in public profile.
- Full event notification coverage, including response rejected and review reminders to both parties.
- Robust category-tree validation and deletion guards.
- End-to-end integration tests for the new flow; initial tests are being added.
- Legacy service catalog and finance features remain in code and must be removed from the MVP navigation/flows without deleting historical data.
- Old JSON notification preferences remain beside the new subscriptions table and need migration/cleanup.

## Critical acceptance tests
1. Fresh and populated DB migrations preserve existing data.
2. Tasks accept only visible leaf categories; Other/Miscellaneous requires a 200-char request.
3. Owner cannot respond to own task; executor can respond once only.
4. Customer can accept exactly one response under concurrency; other responses are rejected; exactly one chat is created.
5. Only customer/executor can read/write chat; moderator sees full transcript.
6. Messages max 2,000 chars; read_at updates; hide-for-self doesn't hide from moderator.
7. Edit restrictions and task_history are enforced.
8. Completion and reviews require correct participants/status; duplicate reviews fail.
9. Dispute changes task state and is visible to moderators.
10. Subscriptions cap at 20; root subscription covers child categories.
11. Event notifications and hourly digest are deduplicated.
12. Bot onboarding persists and supports skip/restart.
