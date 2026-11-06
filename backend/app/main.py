import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging

settings = get_settings()
configure_logging()
log = logging.getLogger("app")

if settings.sentry_dsn:
    import sentry_sdk

    sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.app_env, traces_sample_rate=0.1,
                    send_default_pii=False)

app = FastAPI(title="CarStudio AI", version="0.1.0",
              docs_url="/docs" if settings.app_env != "production" else None)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins or [settings.frontend_origin],
                   allow_methods=["GET", "POST", "DELETE"],
                   allow_headers=["Content-Type", "Idempotency-Key", "X-Client-Session"])


@app.middleware("http")
async def request_id(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["X-Request-Id"] = rid
    return response


def _envelope(request: Request, status: int, code: str, message: str, retryable: bool) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {
        "code": code, "message": message, "retryable": retryable,
        "request_id": getattr(request.state, "request_id", None)}})


@app.exception_handler(AppError)
async def app_error(request: Request, exc: AppError):
    return _envelope(request, exc.status_code, exc.code, exc.message, exc.retryable)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    fields = [".".join(str(p) for p in e["loc"][1:]) for e in exc.errors()]
    return _envelope(request, 422, "invalid_input", f"Invalid fields: {', '.join(fields)}", False)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error", extra={"request_id": getattr(request.state, "request_id", None)})
    return _envelope(request, 500, "internal_error", "Something went wrong.", True)


app.include_router(router)
