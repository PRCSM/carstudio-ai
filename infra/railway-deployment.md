# Railway deployment

One Docker image (`backend/Dockerfile`), two services, plus Redis.

| Service | Start command | Public |
|---|---|---|
| `api` | default `CMD` (uvicorn on `$PORT`) | Yes — generate a domain |
| `worker` | `celery -A app.workers.celery_app worker -B --loglevel=INFO --concurrency=1 --max-tasks-per-child=1 --max-memory-per-child=600000` | No |
| `redis` | Railway Redis template | No (private network) |

`-B` runs the beat scheduler inside the worker (outbox dispatch, stuck-job requeue, retention cleanup).
Run exactly **one** worker replica while beat is embedded; split beat into its own service before scaling.

## Variables
Set on both `api` and `worker` (Railway → service → Variables). Use `${{Redis.REDIS_URL}}` for `REDIS_URL`.
See `.env.example` for the full list. Secrets (`*_KEY`, `*_TOKEN`, `*_SECRET`, `DATABASE_URL`) are
entered by the owner in the Railway dashboard, never committed or pasted into chat.

`DATABASE_URL` must use Supabase's **transaction pooler** (port 6543), with the `postgresql+psycopg://` scheme.

## Migrations
Schema lives in `backend/migrations/*.sql` and is applied as a controlled release step
(currently via the Supabase connector). Workers never auto-migrate.

## Health checks
API health check path: `/health/live`. Readiness (DB + Redis): `/health/ready`.
