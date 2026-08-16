"""Login-time offensive-name auto-moderation.

Stage A: a deterministic, leetspeak-aware blocklist for display names.
Kept free of Django imports at module level so it can be unit-tested in plain
Python (no DB, no settings). The lightweight LLM judge (Stage B) and the
orchestrator that wires both into the login path live alongside this in the
app modules.
"""

from __future__ import annotations

import re

# Leetspeak decoding table. Conservative on purpose: over-aggressive mapping
# (e.g. 6->g, 2->z) invents slurs inside clean names. Only decode the
# characters that genuinely appear in the obfuscated forms we've observed.
_LEET_MAP = {
    "1": "i",
    "!": "i",
    "|": "i",
    "£": "i",
    "3": "e",
    "4": "a",
    "@": "a",
    "5": "s",
    "$": "s",
    "7": "t",
    "0": "o",
    "9": "g",
}

_RESERVED_TAG_PATTERN = re.compile(r"^\[[A-Z0-9]+\]", re.IGNORECASE)

# Patterns are matched with `re.search` against normalize_name(name) — i.e. the
# lowercase, leetspeak-decoded, separator-stripped form. The doubled consonant
# in the n-slur (`nigg`) is deliberate: it matches nigga/nigger/nigga-with-a
# but NOT "Nigeria"/"Niger"/"Nigel"/"enigma" (all have a single g after the i).
# A bounded single-g variant (`niga`) catches common one-g obfuscations like
# "delivry1gaa"[...]  while still not matching clean words.
SLUR_PATTERNS: list[tuple[str, str]] = [
    # Severe racial / ethnic slurs
    (r"n[i1]gg", "racial_slur"),        # nigga, nigger, nigg
    (r"n[i1]ga(?:a+)?", "racial_slur"), # single-g "niga" variant
    (r"k[i1]k[3e]", "racial_slur"),     # kike
    (r"sp[i1]c", "racial_slur"),        # spic
    (r"ch[i1]n[e3k]", "racial_slur"),   # chink
    (r"b[3e][a4]n[e3]r", "racial_slur"),# beaner
    (r"w[3e]t[4a]b?[a4]ck", "racial_slur"),  # wetback
    (r"coon", "racial_slur"),
    (r"tarbab[3e]?", "racial_slur"),
    # Anti-gay / homophobic slurs
    (r"f[a4]g[o0]?t", "homophobic_slur"),  # fagot / faggot
    (r"f[a4]gg", "homophobic_slur"),
    (r"j[3e]rk|homo", "homophobic_slur"),
    # Misogynistic slurs
    (r"cunt", "misogynistic_slur"),
    (r"b[1i]tch", "misogynistic_slur"),
    (r"sl[a4]v[3e]", "hate_terms"),        # slave
    # Generic hate / derogatory terms
    (r"r[3e]t[a4]rd", "ableist_slur"),
    (r"n[a4]z[i1]", "hate_slur"),
    (r"r[a4]p[3e]", "hate_slur"),          # rape/rap3
    (r"p[3e]d[0o]", "hate_slur"),          # pedo
]

_COMPILED: list[tuple[re.Pattern, str]] = [
    (re.compile(p), label) for p, label in SLUR_PATTERNS
]


def normalize_name(name: str) -> str:
    """Lowercase, decode common leetspeak, drop separators.

    Returns a single continuous token for pattern matching.
    """
    s = name.strip().lower()
    out = [_LEET_MAP.get(ch, ch) for ch in s]
    s = "".join(out)
    return re.sub(r"[\s.\\_~'\"`/()\[\];:,\,!?|]+", "", s)


def is_offensive_blocklist(name: str) -> tuple[bool, list[str]]:
    """Return (matched, categories) for a name flagged by the Stage A blocklist.

    Deterministic and exact-token based — distinct from the LLM judge. A known
    slur (obfuscated or not) is a certain violation; clean names return False.
    """
    token = normalize_name(name)
    categories: list[str] = []
    for pat, label in _COMPILED:
        if pat.search(token):
            categories.append(label)
    return bool(categories), categories


def strip_reserved_tags(name: str) -> str:
    """Strip a leading reserved bracket tag (e.g. ``[GOV]``, ``[M]``, ``[DOT]``).

    Reserved tags identify staff/mod identities and must never be auto-renamed
    or fed into the slur check.
    """
    return _RESERVED_TAG_PATTERN.sub("", name).strip() if name else name