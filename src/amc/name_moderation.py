"""Display-name helpers for login-time auto-moderation.

The deterministic regex slur blocklist was removed (2026-08) because the game
already enforces offensive-name blocking at the source; the LLM judge (Stage B)
is now the only moderation layer here. What remains in this module is the
reserved-tag handling — staff/mod display identities (``[GOV]``, ``[MOD]``,
etc.) must never be auto-renamed or fed to the judge.

Kept free of Django imports at module level so it can be unit-tested in plain
Python (no DB, no settings).
"""

from __future__ import annotations

import re

_RESERVED_TAG_PATTERN = re.compile(r"^\[[A-Z0-9]+\]", re.IGNORECASE)


def strip_reserved_tags(name: str) -> str:
    """Strip a leading reserved bracket tag (e.g. ``[GOV]``, ``[M]``, ``[DOT]``).

    Reserved tags identify staff/mod identities and must never be auto-renamed
    or fed into the moderation check.
    """
    return _RESERVED_TAG_PATTERN.sub("", name).strip() if name else name
