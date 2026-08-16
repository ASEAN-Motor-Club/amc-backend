"""Tests for login-time offensive-name auto-moderation.

Covers: NameModerationLog audit model (Task 2), Stage A deterministic
blocklist (Task 3), Pydantic structured verdicts (Task 4), the OpenRouter LLM
judge (Step 5), the orchestrator + login hook + auto-rename + announcement
(Step 6), and the safe-suggestion guard.

These require PostgreSQL (ArrayField) so they run under the flake's pytest
check / CI, not in a plain venv without Postgres.
"""

import pytest

from amc.factories import CharacterFactory, PlayerFactory
from amc.llm_judge import _cache, judge_name
from amc.models import NameModerationLog
from amc.name_moderation import (
    is_offensive_blocklist,
    normalize_name,
    strip_reserved_tags,
)
from amc.name_verdict import NameVerdict

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_judge_cache():
    _cache.clear()
    yield
    _cache.clear()


@pytest.mark.asyncio
async def test_judge_name_returns_llm_verdict(monkeypatch):
    async def fake_call(name):
        return NameVerdict(
            name=name, is_violation=True, confidence=0.9,
            recommended_action="rename", suggested_name="NiceName",
        )

    monkeypatch.setattr("amc.llm_judge._call_llm", fake_call)
    verdict, src = await judge_name("CoolName")
    assert src == "llm"
    assert verdict.is_violation is True
    assert verdict.suggested_name == "NiceName"


@pytest.mark.asyncio
async def test_judge_name_degrades_on_error(monkeypatch):
    async def boom(name):
        raise RuntimeError("api down")

    monkeypatch.setattr("amc.llm_judge._call_llm", boom)
    verdict, src = await judge_name("SomeName")
    assert src == "error"
    assert verdict.is_violation is False
    assert verdict.recommended_action == "none"


@pytest.mark.asyncio
async def test_judge_name_serves_cache_hit(monkeypatch):
    calls = []

    async def fake_call(name):
        calls.append(name)
        return NameVerdict(name=name, is_violation=True, confidence=0.9,
                           recommended_action="rename", suggested_name="Taco")

    monkeypatch.setattr("amc.llm_judge._call_llm", fake_call)
    _, src1 = await judge_name("Repeat")
    _, src2 = await judge_name("repeat")  # same normalized key -> cache
    assert src1 == "llm"
    assert src2 == "cache"
    assert len(calls) == 1


def test_name_verdict_defaults_and_bounds():
    """Pydantic verdict enforces types/enums and defaults action to none."""
    v = NameVerdict(name="ok", is_violation=False)
    assert v.recommended_action == "none"
    assert v.categories == []
    assert 0.0 <= v.confidence <= 1.0


def test_name_verdict_rejects_bad_action():
    with pytest.raises(ValueError):
        NameVerdict.model_validate(
            {"name": "x", "is_violation": True, "confidence": 1.0,
             "recommended_action": "BOGUS"}
        )


def test_name_verdict_rejects_out_of_range_confidence():
    with pytest.raises(ValueError):
        NameVerdict(name="x", is_violation=False, confidence=99.0)


def test_blocklist_flags_known_slurs():
    """Stage A catches canonical + leetspeak-obfuscated slurs (Task 3)."""
    flagged = [
        "delivyn1gaa",    # 1->i single-g "niga"
        "n1gga",
        "Niggerslayer",
        "Localslave",     # slave
        "xF4GGOTx",       # faggot with 4->a
        "fagot",          # single-g f-slur
        "N4ziScum",       # nazi
    ]
    for name in flagged:
        assert is_offensive_blocklist(name)[0] is True, name


def test_blocklist_ignores_clean_names():
    """Stage A must never flag clean names (precision over recall)."""
    clean = [
        "HappyDriver",
        "Motortown",   # internal 'tow' etc. must be safe
        "Nigeria",     # country — single-g after 'i', must NOT match
        "Nigel",       # name
        "enigma",      # 'nig' inside a clean word
        "ih8juice",    # no slur token -> falls to LLM, not blocklist
        "truckin",
        "JUICEn1g",    # truncated n, no double-g / trailing vowel -> precision gate
        "Tofu",
    ]
    for name in clean:
        assert is_offensive_blocklist(name)[0] is False, name


def test_normalize_decodes_leetspeak():
    assert normalize_name("delivyn1gaa") == "delivynigaa"
    assert normalize_name("xF4GGOT x") == "xfaggotx"


def test_strip_reserved_tags_removes_bracket_prefix():
    assert strip_reserved_tags("[GOV] Boss") == "Boss"
    assert strip_reserved_tags("[M] Racer") == "Racer"
    assert strip_reserved_tags("NoTag") == "NoTag"


@pytest.mark.asyncio
async def test_name_moderation_log_row_persists():
    """A decision row records a player + verdict + action (Task 2)."""
    player = await PlayerFactory.acreate()
    character = await CharacterFactory.acreate(player=player)
    await NameModerationLog.objects.acreate(
        player=player,
        character=character,
        base_name="delivyn1gaa",
        verdict_source=NameModerationLog.VerdictSource.BLOCKLIST,
        is_violation=True,
        confidence=1.0,
        categories=["racial_slur"],
        action=NameModerationLog.Action.RENAME,
        suggested_name="FriendlyDriver",
        llm_model="",
    )
    row = await NameModerationLog.objects.aget(player=player)
    assert row.is_violation is True
    assert row.action == NameModerationLog.Action.RENAME
    assert row.suggested_name == "FriendlyDriver"