from __future__ import annotations

import html


def esc(value: object) -> str:
    """The one escaping helper every dynamic value must pass through before reaching HTML."""
    return html.escape(str(value), quote=True)
