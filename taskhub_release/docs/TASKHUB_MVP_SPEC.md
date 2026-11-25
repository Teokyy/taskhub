# TaskHub Marketplace MVP

Approved scope. Implementation stage: pre-alpha.

## Product goal
Validate demand for a task marketplace. Primary metric: share of published tasks that receive at least one accepted response and reach `done`.

## Roles
- Customer: publish/edit tasks, review responses, select executor, confirm completion, review.
- Executor: browse tasks, respond, chat after selection, review.
- Moderator: handle disputes, inspect full deal chats, process category requests, manage categories.
- One account may act as both customer and executor.

## Money
Direct settlement between customer and executor. TaskHub does not hold funds, provide escrow, or process payouts.

## Included in MVP
- Tasks with a two-level category tree.
- Responses and customer selection.
- Private deal chat, text-only, max 2,000 characters, polling every 3–5 seconds.
- Category subscriptions (up to 20), Telegram event notifications and hourly digest.
- Telegram bot onboarding quest.
- Requests for new categories via Other → Miscellaneous (description max 200 characters).
- One review per party per completed deal (rating 1–5, text max 500 characters).
- Disputes and moderator access to full chat history.

## Explicitly excluded
Provider service catalog, escrow, platform payouts, repeatable services/tasks, weighted/multi-factor reputation, Bayesian average, chat attachments.

## Task lifecycle
`open` → `in_progress` → `done`; `cancelled` and `disputed` are alternate states.
1. Customer publishes an open task.
2. Executor sends one response.
3. Customer accepts one response; other responses become rejected; one private chat is created.
4. Customer confirms completion; chat becomes read-only; both parties may review.
5. Either party can open a dispute; moderator can inspect full chat.

Customer can cancel an open task. After executor selection, cancellation requires mutual consent or dispute resolution.

## Editing
Before selection: all fields editable. After selection: description and deadline only; reward/category locked. Executor is notified and may withdraw without penalty. Every changed field is written to task_history.

## Responses and chat
One response per task/executor; optional proposed deadline. Executor may withdraw while response is pending. One chat per accepted response; only the two deal participants can access it. Messages cannot be physically deleted; hide-for-self only. Warn (do not block) phone numbers and messenger links. Moderator can see complete history.

## Categories
Exactly two levels; no orphan subcategories:
- Marketing: SMM, Targeted ads, Copywriting, SEO, Social design, Social video editing, Email campaigns, Influencer marketing.
- Discord: Bots, Server setup/design, Moderation, Roles/permissions, Integrations, Emoji/banner design.
- Games: Guides/text, Modding, Account leveling, Game assets, Boost/training, Testing/bug reports, Streams/editing.
- Development: Telegram bots, Websites/landing pages, Mobile apps, Scripts/automation.
- Design: Logos/branding, Illustrations, UI/UX, Presentations.
- Texts/translations: Copywriting, Editing, Translation, Transcription.
- Video/audio: Video editing, Voice-over, Motion design, Podcasts.
- Education/tutoring: Languages, School subjects, Programming, Exam prep.
- Business/finance: Accounting, Legal help, Business plans, Consultations.
- Other: Miscellaneous.

Moderator can create, rename, move and hide categories. Hidden categories retain existing tasks but accept no new tasks/subscriptions. Deletion is forbidden while tasks or subscriptions reference a category. All tree changes are audited.

## Category requests
Choosing Other → Miscellaneous requires a niche description (max 200 chars). Request is queued with a task link. Moderator adds category, moves task to an existing category, or rejects; requester is notified.

## Subscriptions and notifications
Up to 20 root/subcategory subscriptions. Root subscription covers all children. One-click unsubscribe in bot. Event notifications: new response to customer; accepted/rejected response to executor; post-selection edit to executor; new chat message when recipient has been offline 10+ minutes; completion/review reminder to both; category-request decision to requester. Hourly digest is one message per user and only sent when matching new tasks exist. Telegram only.

## Bot onboarding
First /start starts a skippable 3-step quest: choose customer/executor/both; choose root category interests; take first action (customer creates task, executor opens feed, both see both). Progress persists; /quest restarts; /skip exits without creating role/subscriptions. “First quest” achievement. Commands: /tasks, /mine, /chats, /menu, /skip.

## Reviews
Only after done. One review per party per deal, rating 1–5, text max 500 chars. Profile shows average and count. No replies.

## Non-functional requirements
Feed under 2 seconds at 10,000 active tasks. Event notifications under 1 minute; digest within an hour. Audit task/response/category actions. Do not expose phone/email in chat or reviews. Russian bot interface.

## Acceptance rule
A feature is not complete because an endpoint or button exists. It must pass end-to-end tests with separate customer, executor and moderator accounts, including permission and negative cases.
