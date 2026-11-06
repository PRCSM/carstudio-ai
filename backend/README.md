# backend

Built on Railway from `backend/Dockerfile` (services set `dockerfilePath=Dockerfile`, root `/backend`).

- api: default CMD (uvicorn on $PORT)
- worker: `celery -A app.workers.celery_app worker -B --loglevel=INFO --concurrency=2`
