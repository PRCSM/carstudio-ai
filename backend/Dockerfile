FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 U2NET_HOME=/models
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

# Bake the background-removal model into the image so the free local layer works offline.
RUN python -c "from rembg import new_session; new_session('isnet-general-use')"

COPY app ./app
COPY migrations ./migrations
RUN useradd -m appuser && chown -R appuser /srv /models
USER appuser

# Railway sets $PORT. The worker service overrides this command (see infra/railway-deployment.md).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
