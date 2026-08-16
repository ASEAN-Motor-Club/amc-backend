"""Stage B: OpenRouter LLM judge for display-name moderation.

An `AsyncOpenAI` client pointed at OpenRouter (identical setup to
amc-peripheral) calls `beta.chat.completions.parse` with the `NameVerdict`
Pydantic model as the enforced structured-output schema. Every failure path
degrades to a non-action verdict — this function NEVER raises into the login
path and NEVER blocks a login.
"""

from __future__ import annotations

import asyncio
import time

from django.conf import settings
from openai import AsyncOpenAI

from amc.name_verdict import NameVerdict

_JUDGE_TIMEOUT_S = 20

_client: AsyncOpenAI | None = None

# Small in-process TTL cache keyed by the exact base name, so a name is judged
# once per window and repeat offenders / same-name logins don't re-hit the LLM.
_cache: dict[str, tuple[float, NameVerdict]] = {}


def _get_client() -> AsyncOpenAI:
    """Lazy OpenRouter client (mirrors amc-peripheral radio_cog.py)."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY_OPENROUTER,
            base_url="https://openrouter.ai/api/v1",
        )
    return _client


def _system_prompt() -> str:
    return (
        "You are a video-game display-name moderator. Judge whether the given "
        "player name violates community anti-hate rules: racist slurs (including "
        "the n-word and its 1337/leetspeak spellings, e.g. '1' for 'i'), "
        "homophobic slurs, misogynistic slurs, or other hateful/profanity names. "
        "Respond ONLY as the given structured JSON schema. If the name violates, "
        "set is_violation=true, give a confidence 0..1, choose categories from "
        "[racial_slur, homophobic_slur, misogynistic_slur, hate_slur, "
        "ableist_slur, sexual], and propose a single clean, friendly replacement "
        "in suggested_name. Never reproduce the slur inside reason or "
        "suggested_name."
    )


def _error_verdict(name: str) -> NameVerdict:
    return NameVerdict(
        name=name,
        is_violation=False,
        confidence=0.0,
        recommended_action="none",
        reason="judge_error",
    )


async def _call_llm(name: str) -> NameVerdict:
    completion = await _get_client().beta.chat.completions.parse(
        model=settings.NAMER_LLM_MODEL,
        response_format=NameVerdict,
        messages=[
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": f"Name to judge: {name!r}"},
        ],
    )
    if not completion.choices or not completion.choices[0].message.parsed:
        return _error_verdict(name)
    return completion.choices[0].message.parsed


async def judge_name(name: str) -> tuple[NameVerdict, str]:
    """Return (verdict, source) for a base name; cached; never raises.

    source is one of "cache" | "llm" | "error" so the caller can record the
    provenance in the audit log.
    """
    key = name.strip().lower()
    now = time.monotonic()
    ttl = getattr(settings, "NAMER_VERDICT_CACHE_TTL", 3600)

    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1].model_copy(deep=True), "cache"

    try:
        verdict = await asyncio.wait_for(_call_llm(name), timeout=_JUDGE_TIMEOUT_S)
    except Exception:
        return _error_verdict(name), "error"

    if verdict.is_violation:
        # Cache only interesting verdicts to keep the cache small.
        _cache[key] = (now, verdict.model_copy(deep=True))
    return verdict, "llm"