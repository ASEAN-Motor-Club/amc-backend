"""Tests for login-time offensive-name auto-moderation.

Covers: NameModerationLog audit model (Task 2), Stage A deterministic
blocklist (Task 3), Pydantic structured verdicts (Task 4), the OpenRouter LLM
judge (Step 5), the orchestrator + login hook + auto-rename + announcement
(Step 6), and the safe-suggestion guard.

These require PostgreSQL (ArrayField) so they run under the flake's pytest
check / CI, not in a plain venv without Postgres.
"""

import pytest
from asgiref.sync import sync_to_async
from django.test import override_settings

from amc.factories import CharacterFactory, PlayerFactory
from amc.llm_judge import _cache, judge_name
from amc.models import ForcedNameLog, NameModerationLog, Player
from amc.name_moderation import (
    is_offensive_blocklist,
    normalize_name,
    strip_reserved_tags,
)
from amc.name_policy import _safe_suggested_name, run_name_moderation
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
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
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


# ---------------------------------------------------------------------------
# Orchestrator (Task 6)
# ---------------------------------------------------------------------------


class _FakeHttp:
    """Minimal aiohttp-like client whose calls fail — only DB writes matter."""

    def __init__(self):
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append(("post", url))
        raise RuntimeError("network off")


@pytest.mark.asyncio
@override_settings(NAMER_ENABLED=False)
async def test_run_name_moderation_disabled_does_nothing():
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
    character.name = "delvyn1gaa"
    character.custom_name = None
    await character.asave()

    await run_name_moderation(character, player, _FakeHttp(), _FakeHttp())

    assert await NameModerationLog.objects.acount() == 0
    await player.arefresh()
    assert player.forced_name is None


@pytest.mark.asyncio
@override_settings(NAMER_ENABLED=True, NAMER_CANNED_FALLBACK_NAME="FriendlyPlayer")
async def test_run_name_moderation_blocklist_auto_renames():
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
    character.name = "delvyn1gaa"
    character.custom_name = None
    await character.asave()

    await run_name_moderation(character, player, _FakeHttp(), _FakeHttp())

    await player.arefresh()
    assert player.forced_name == "FriendlyPlayer"
    assert await ForcedNameLog.objects.filter(player=player).acount() == 1
    row = await NameModerationLog.objects.aget(player=player)
    assert row.verdict_source == "blocklist"
    assert row.action == "rename"


@pytest.mark.asyncio
@override_settings(NAMER_ENABLED=True, NAMER_AUTO_CONFIDENCE_THRESHOLD=0.9)
async def test_run_name_moderation_llm_high_conf_renames(monkeypatch):
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
    character.name = "CoolName"  # not blocklisted -> Stage B
    character.custom_name = None
    await character.asave()

    async def fake_judge(name):
        return (
            NameVerdict(
                name=name, is_violation=True, confidence=0.97,
                categories=["hate_slur"], reason="contextual slur",
                suggested_name="MuchBetter", recommended_action="rename",
            ),
            "llm",
        )

    monkeypatch.setattr("amc.name_policy.judge_name", fake_judge)
    await run_name_moderation(character, player, _FakeHttp(), _FakeHttp())

    await player.arefresh()
    assert player.forced_name == "MuchBetter"
    assert await ForcedNameLog.objects.filter(player=player).acount() == 1
    row = await NameModerationLog.objects.aget(player=player)
    assert row.verdict_source == "llm"
    assert row.action == "rename"


@pytest.mark.asyncio
@override_settings(NAMER_ENABLED=True)
async def test_run_name_moderation_low_conf_review(monkeypatch):
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
    character.name = "Ambiguous"
    character.custom_name = None
    await character.asave()

    async def fake_judge(name):
        return (
            NameVerdict(
                name=name, is_violation=True, confidence=0.5,
                categories=["hate_slur"], recommended_action="manual_review",
            ),
            "llm",
        )

    monkeypatch.setattr("amc.name_policy.judge_name", fake_judge)
    await run_name_moderation(character, player, _FakeHttp(), _FakeHttp())

    await player.arefresh()
    assert player.forced_name is None  # no lock on sub-threshold
    row = await NameModerationLog.objects.aget(player=player)
    assert row.action == "manual_review"


def test_safe_suggested_rejects_offensive_and_junk():
    assert _safe_suggested_name("Cool Name") == "Cool Name"
    assert _safe_suggested_name("n1gga") is None        # still offensive
    assert _safe_suggested_name("[GOV] boss") is None   # reserved tag
    assert _safe_suggested_name("") is None
    assert _safe_suggested_name(None) is None
    assert _safe_suggested_name("A" * 60) is None       # too long