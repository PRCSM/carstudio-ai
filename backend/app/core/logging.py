import json
import logging
import re
import sys

_PHONE = re.compile(r"\+?\d[\d\s-]{8,}\d")
_URL_TOKEN = re.compile(r"(token|sig|signature|key)=[^&\s]+", re.I)


def redact(text: str) -> str:
    text = _PHONE.sub("[phone]", text)
    return _URL_TOKEN.sub(r"\1=[redacted]", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for key in ("request_id", "job_id", "provider", "duration_ms", "status"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
