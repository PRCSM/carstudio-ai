# CarStudio AI

Turns a customer's car photo into a 2K studio-style image, through a Telegram bot, while keeping
the *observed* vehicle unchanged. Generated lighting and backgrounds are AI edits, not evidence of condition.

**Interactive architecture board:** https://architecture-production-6546.up.railway.app
(click any card for what it does and why, or switch to *Flows* to watch a request travel through the system)

## How a photo becomes a studio image

```
upload → validate (type, size, EXIF strip, blur/exposure) → vision profile (advisory)
      → provider chain → QA (geometry + identity) → approved | retry | human review
```

**Provider chain** (`IMAGE_PROVIDER_CHAIN`, tried left to right):

| Layer | Provider | Cost | Skipped when |
|---|---|---|---|
| 1 | `fal_flux_kontext` (paid) | ~$0.04 | not configured, circuit open, or monthly budget reached |
| 2 | `gemini_free` | $0 | customer consent doesn't cover free-tier training use |
| 3 | `local_composite` | $0 | never — cuts the real car out and places it in a drawn studio |

Credit/quota/auth failures open a 5-minute circuit for that provider. Policy rejections stop the chain.
If every remote provider is down, the job is re-queued with backoff instead of failing.

## Layout

```
backend/   FastAPI API, Celery worker, providers, QA, SQL migrations, tests
infra/     local docker compose, Railway notes
```

## Run tests

```bash
cd backend && pip install -e ".[dev]" && pytest -q
```

## Status

- [x] Supabase schema + private buckets (project `carstudio-ai`, ap-south-1)
- [x] API: uploads, jobs, status, results, cancel, delete-my-data, admin review
- [x] Worker: pipeline, provider fallback chain, budget guard, QA, outbox, retention cleanup
- [ ] Railway deploy (needs repo + secrets)
- [ ] n8n WhatsApp workflows
- [ ] Next.js demo
- [ ] Model benchmark on real consented photos
