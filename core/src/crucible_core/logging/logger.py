"""Concise idempotent stdlib logger configuration."""

from __future__ import annotations

import logging as stdlib_logging

_configured = False


def configure_logging(
    level: int = stdlib_logging.INFO,
    *,
    force: bool = False,
) -> None:
    """Configure the application logger once; safe to call repeatedly."""
    global _configured
    if _configured and not force:
        return
    formatter = stdlib_logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    app_logger = stdlib_logging.getLogger("crucible_core")
    if force:
        app_logger.handlers.clear()
    if not any(
        isinstance(item, stdlib_logging.StreamHandler)
        for item in app_logger.handlers
    ):
        handler = stdlib_logging.StreamHandler()
        handler.setFormatter(formatter)
        app_logger.addHandler(handler)
    app_logger.setLevel(level)
    stdlib_logging.getLogger().setLevel(level)
    _configured = True


def get_logger(name: str = "crucible_core") -> stdlib_logging.Logger:
    return stdlib_logging.getLogger(name)
