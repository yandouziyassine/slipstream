from __future__ import annotations

import re
from collections.abc import Iterable

# A '/' not preceded by a word character, '/', ':' or '<' starts a local path, not part of a
# URL or a closing tag.
_POSIX_PATH = re.compile(r"(?<![\w/:<])/[^\s'\"<>]+")
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\[^\s'\"<>]*")


def redact_paths(text: str) -> str:
    # Run errors are published, and an OSError names local files (and so the user's name).
    return _WINDOWS_PATH.sub("<path>", _POSIX_PATH.sub("<path>", text))


def redact(text: str, secrets: Iterable[str], limit: int = 300) -> str:
    # Longest first, so a secret that contains another one is removed whole.
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    return redact_paths(text)[:limit]
