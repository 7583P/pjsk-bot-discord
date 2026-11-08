import json
import logging
import re
from datetime import datetime, timezone


def redact(value):
    value = re.sub(r"(?:postgres(?:ql)?://)[^\s]+", "[REDACTED_DATABASE_URL]", value)
    value = re.sub(
        r'(?i)([?&#](?:code|state|token|access_token|pjsk_token|authToken)=)[^&\s"\']+',
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9_.-]+", r"\1[REDACTED]", value)
    return re.sub(r"(/webhooks/\d+/)[^/\s?]+", r"\1[REDACTED]", value)


class SecretFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(str(record.msg))
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact(v) if isinstance(v, str) else v for v in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                k: redact(v) if isinstance(v, str) else v
                for k, v in record.args.items()
            }
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        # Exception messages can contain connection credentials; only log type and frames.
        message = redact(record.getMessage())
        data = dict(
            time=datetime.now(timezone.utc).isoformat(),
            level=record.levelname,
            logger=record.name,
            message=message,
        )
        if record.exc_info:
            import traceback

            data["exception"] = record.exc_info[0].__name__
            data["frames"] = traceback.format_list(
                traceback.extract_tb(record.exc_info[2])
            )
        return json.dumps(data, ensure_ascii=True)


def configure():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(SecretFilter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    for name in ["uvicorn.access", "uvicorn.error", "httpx"]:
        logger = logging.getLogger(name)
        if not any(isinstance(f, SecretFilter) for f in logger.filters):
            logger.addFilter(SecretFilter())
