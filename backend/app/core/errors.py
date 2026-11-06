"""Consistent error envelope: {code, message, request_id, retryable}."""


class AppError(Exception):
    status_code = 400
    code = "bad_request"
    retryable = False

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None,
                 retryable: bool | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        if retryable is not None:
            self.retryable = retryable


class ValidationFailed(AppError):
    status_code = 422
    code = "invalid_input"


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class QuotaExceeded(AppError):
    status_code = 429
    code = "quota_exceeded"
    retryable = True


class Conflict(AppError):
    status_code = 409
    code = "conflict"
