"""Logs en los tres lugares: stdout (JSON), disco (archivo rotativo) y memoria (GET /logs).

El enunciado pide logs en memoria y en disco; stdout es para kubectl logs / Cloud Logging.
"""
import collections
import json
import logging
import logging.handlers
import os
import sys
import time

_MEMORY: collections.deque = collections.deque(maxlen=int(os.getenv("LOG_MEMORY_LINES", "1000")))


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "service": self.service,
            "msg": record.getMessage(),
        }
        data.update(getattr(record, "fields", {}))
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


class MemoryHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _MEMORY.append(self.format(record))


def setup_logging(service: str) -> logging.Logger:
    fmt = JsonFormatter(service)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout), MemoryHandler()]
    log_dir = os.getenv("LOG_DIR", "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                os.path.join(log_dir, f"{service}.log"), maxBytes=5_000_000, backupCount=3, encoding="utf-8"
            )
        )
    except OSError:
        pass  # sin disco escribible seguimos con stdout + memoria
    logger = logging.getLogger(service)
    logger.handlers = []
    for h in handlers:
        h.setFormatter(fmt)
        logger.addHandler(h)
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    logger.propagate = False
    return logger


def log(logger: logging.Logger, msg: str, level: int = logging.INFO, **fields) -> None:
    logger.log(level, msg, extra={"fields": fields})


def recent(n: int = 200) -> list[dict]:
    return [json.loads(line) for line in list(_MEMORY)[-n:]]
