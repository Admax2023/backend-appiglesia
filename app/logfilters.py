"""Logging filters.

Tracking tokens are bearer secrets carried in the URL path (/api/public/orders/{token}[/proofs]).
Access logs must not contain them, so the uvicorn access logger gets a filter that replaces the
token segment with a fixed placeholder. Reverse-proxy logs need the same care (docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import logging
import re

_TOKEN_PATH = re.compile(r"(/api/public/orders/)[^/?#\s\"]+")
REDACTED = "<token>"


def redact_path(text: str) -> str:
    return _TOKEN_PATH.sub(r"\g<1>" + REDACTED, text)


class RedactTrackingTokenFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(redact_path(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.msg, str):
            record.msg = redact_path(record.msg)
        return True


def install() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactTrackingTokenFilter) for f in logger.filters):
        logger.addFilter(RedactTrackingTokenFilter())
