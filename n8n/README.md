# n8n workflows

Source for the workflows on `paikhom.app.n8n.cloud` (built with the n8n Workflow SDK).

| Workflow | File | Trigger | Status |
|---|---|---|---|
| CarStudio · Telegram bot v2 | `telegram-bot-v2.workflow.ts` | Telegram updates (photos, albums, button taps, `/status`) | live |
| CarStudio · Job events → Telegram v2 | `job-events-v2.workflow.ts` | `POST /webhook/carstudio-job-events-v2` from the worker | live |
| CarStudio · Telegram bot | `telegram-bot.workflow.ts` | single photo, style buttons | v1, kept for rollback |
| CarStudio · Job events → Telegram | `job-events.workflow.ts` | `POST /webhook/carstudio-job-events` | v1, kept for rollback |

## Credentials (create in n8n → Credentials)

| Name | Type | Value |
|---|---|---|
| CarStudio Telegram | Telegram API | Bot token from @BotFather |
| CarStudio API | Header Auth | Name `Authorization`, value `Bearer <N8N_INTERNAL_API_TOKEN>` (from Railway shared variables) |

The same **CarStudio API** credential is used both to call the API and to authenticate the
API's job events arriving at the webhook (the worker sends the same bearer token).

## Flow (v2)

1. Photo or album → `POST /v1/uploads` (headers `X-Customer-Ref: <chat id>`, `X-Channel: telegram`, `X-Media-Group` for albums). Only the first upload of an album shows the buttons.
2. Buttons: mode (studio edit or extra angles), style, plate blur → `POST /v1/sets` with an `Idempotency-Key`.
3. Worker finishes the set → outbox → webhook (Bearer token + HMAC) → `GET /v1/sets/{id}`.
4. For each image: Telegram `sendPhoto` (preview, labelled by angle), then the full-quality JPEG via `sendDocument`. Then one summary message.
