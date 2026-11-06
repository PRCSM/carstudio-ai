# n8n workflows

Source for the workflows on `paikhom.app.n8n.cloud` (built with the n8n Workflow SDK).

| Workflow | File | Trigger |
|---|---|---|
| CarStudio · Telegram bot | `telegram-bot.workflow.ts` | Telegram updates (messages + button taps) |
| CarStudio · Job events → Telegram | `job-events.workflow.ts` | `POST /webhook/carstudio-job-events` from the API worker |

## Credentials (create in n8n → Credentials)

| Name | Type | Value |
|---|---|---|
| CarStudio Telegram | Telegram API | Bot token from @BotFather |
| CarStudio API | Header Auth | Name `Authorization`, value `Bearer <N8N_INTERNAL_API_TOKEN>` (from Railway shared variables) |

The same **CarStudio API** credential is used both to call the API and to authenticate the
API's job events arriving at the webhook (the worker sends the same bearer token).

## Flow

1. Photo → `POST /v1/uploads` (headers `X-Customer-Ref: <chat id>`, `X-Channel: telegram`) → style buttons.
2. Button `s|<style>|<upload_id>` → `POST /v1/jobs` with `Idempotency-Key: tg-<upload>-<style>`.
3. Worker finishes → outbox → webhook → `GET /v1/jobs/{id}/results` → Telegram `sendPhoto`.
