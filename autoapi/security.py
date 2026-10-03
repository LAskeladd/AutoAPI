"""Redact configured credentials in diagnostics, without changing raw passthrough."""
from __future__ import annotations

import logging
import threading
import traceback
from collections.abc import Callable, Iterable

from .config import AppConfig


def redact_secrets(text: str, secrets: Iterable[str]) -> str:
    # Longest first also handles credentials sharing a prefix.
    for secret in sorted({value for value in secrets if value}, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


class SecretRedactingFilter(logging.Filter):
    def __init__(self, config_supplier: Callable[[], AppConfig]) -> None:
        super().__init__()
        self._config_supplier = config_supplier
        self._known_secrets: set[str] = set()
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        config = self._config_supplier()
        with self._lock:
            self._known_secrets.update(
                candidate.api_key
                for chain in config.virtual_models.values() for candidate in chain
            )
            # Retain old keys: requests started before hot reload can still log errors.
            secrets = tuple(self._known_secrets)
        message = record.getMessage()
        redacted = redact_secrets(message, secrets)
        if redacted != message:
            record.msg, record.args = redacted, ()
        if record.exc_info:
            exception_text = record.exc_text or "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = redact_secrets(exception_text, secrets)
        if record.stack_info:
            record.stack_info = redact_secrets(record.stack_info, secrets)
        return True
