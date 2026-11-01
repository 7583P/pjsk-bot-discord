import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record):
        # Exception messages can contain connection credentials; only log type and frames.
        message = record.getMessage()
        import re

        message = re.sub(
            r"(?:postgres(?:ql)?://)[^\s]+", "[REDACTED_DATABASE_URL]", message
        )
        message = re.sub(r"(/webhooks/\d+/)[^/\s?]+", r"\1[REDACTED]", message)
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
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
