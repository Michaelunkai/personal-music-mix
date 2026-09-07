"""Redacted structured logging helpers for local runs."""

from __future__ import annotations

import logging
from typing import Any


SENSITIVE = ("token", "cookie", "authorization", "headers", "password", "secret")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[redacted]" if any(word in key.lower() for word in SENSITIVE) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def get_logger(name: str = "ytmusic_recommender") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
    return logger
