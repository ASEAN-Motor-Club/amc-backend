"""Tests for login-time offensive-name auto-moderation.

Covers: NameModerationLog audit model, Pydantic structured verdicts, the
OpenRouter LLM judge, the orchestrator + login hook + auto-rename +
announcement, and the safe-suggestion guard. (The deterministic regex
blocklist was removed — the game already enforces offensive names.)

These require PostgreSQL (ArrayField) so they run under the flake's pytest
check / CI, not in a plain venv without Postgres.
"""

import pytest
from asgiref.sync import sync_to_async
from django.test import override_settings

from amc.factories import CharacterFactory, PlayerFactory
from amc.llm_judge import _cache, judge_name
from amc.models import ForcedNameLog, NameModerationLog, Player
from amc.name_moderation import strip_reserved_tags
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


async def _reload_player(player):
    """Re-fetch a player by unique_id (async-safe reload)."""
    return await Player.objects.aget(unique_id=player.unique_id)


@pytest.mark.asyncio
@override_settings(NAMER_ENABLED=False)
async def test_run_name_moderation_disabled_does_nothing():
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
    character.name = "delvyn1gaa"
    character.custom_name = None
    await character.asave()

    await run_name_moderation(character, player, _FakeHttp(), _FakeHttp())

    # Scoped to this player (other DB tests may have created rows; fixtures
    # intentionally create different players per test).
    assert await NameModerationLog.objects.filter(player=player).acount() == 0
    assert (await _reload_player(player)).forced_name is None


@pytest.mark.asyncio
@override_settings(
    NAMER_ENABLED=True,
    NAMER_AUTO_CONFIDENCE_THRESHOLD=0.9,
    NAMER_REVIEW_CHANNEL_ID="1366478091131551834",
)
async def test_run_name_moderation_llm_high_conf_renames(monkeypatch):
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
    character.name = "CoolName"  # clean name -> LLM judge
    character.custom_name = None
    await character.asave()

    async def fake_judge(name):
        return (
            NameVerdict(
                name=name, is_violation=True, confidence=0.97,
                categories=["racial_slur"], reason="n-word slur",
                suggested_name="MuchBetter", recommended_action="rename",
            ),
            "llm",
        )

    posted = []

    def fake_enqueue(channel_id, content, timestamp):
        posted.append((channel_id, content))

    monkeypatch.setattr("amc.name_policy.judge_name", fake_judge)
    monkeypatch.setattr("amc.tasks.enqueue_discord_message", fake_enqueue)
    await run_name_moderation(character, player, _FakeHttp(), _FakeHttp())

    assert (await _reload_player(player)).forced_name == "MuchBetter"
    assert await ForcedNameLog.objects.filter(player=player).acount() == 1
    row = await NameModerationLog.objects.filter(player=player).aget()
    assert row.verdict_source == "llm"
    assert row.action == "rename"
    assert row.reason == "n-word slur"
    assert len(posted) == 1
    channel, content = posted[0]
    assert channel == "1366478091131551834"
    assert "MuchBetter" in content
    assert "n-word slur" in content
    assert "CoolName" in content


@pytest.mark.asyncio
@override_settings(
    NAMER_ENABLED=True,
    NAMER_AUTO_CONFIDENCE_THRESHOLD=0.9,
    NAMER_REVIEW_CHANNEL_ID="1366478091131551834",
)
async def test_run_name_moderation_high_conf_nonracial_does_not_rename(monkeypatch):
    """High-confidence NON-racial violation must NOT auto-rename (manual review)."""
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
    character.name = "CoolHate"
    character.custom_name = None
    await character.asave()

    async def fake_judge(name):
        return (
            NameVerdict(
                name=name, is_violation=True, confidence=0.97,
                categories=["homophobic_slur"], reason="homophobic slur",
                suggested_name="NiceFriend", recommended_action="rename",
            ),
            "llm",
        )

    posted = []

    def fake_enqueue(channel_id, content, timestamp):
        posted.append((channel_id, content))

    monkeypatch.setattr("amc.name_policy.judge_name", fake_judge)
    monkeypatch.setattr("amc.tasks.enqueue_discord_message", fake_enqueue)
    await run_name_moderation(character, player, _FakeHttp(), _FakeHttp())

    # Must NOT lock/rename, despite 0.97 confidence — category is not racial.
    assert (await _reload_player(player)).forced_name is None
    assert await ForcedNameLog.objects.filter(player=player).acount() == 0
    # No rename happened, so no rename-log. Manual review DOES post once (review
    # channel), distinct from the auto-rename Discord log.
    assert len(posted) == 1
    channel, content = posted[0]
    assert "CoolHate" in content
    assert "manual review" in content
    row = await NameModerationLog.objects.filter(player=player).aget()
    assert row.action == "manual_review"


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
                categories=["hate_slur"], reason="borderline, human review",
                recommended_action="manual_review",
            ),
            "llm",
        )

    monkeypatch.setattr("amc.name_policy.judge_name", fake_judge)
    await run_name_moderation(character, player, _FakeHttp(), _FakeHttp())

    assert (await _reload_player(player)).forced_name is None  # no lock on sub-threshold
    row = await NameModerationLog.objects.filter(player=player).aget()
    assert row.action == "manual_review"
    assert row.reason == "borderline, human review"


def test_safe_suggested_rejects_junk_only():
    """Guard now only enforces reservation, chars, and length (blocklist removed)."""
    assert _safe_suggested_name("Cool Name") == "Cool Name"
    assert _safe_suggested_name("[GOV] boss") is None   # reserved tag
    assert _safe_suggested_name("has/invalid") is None  # char check
    assert _safe_suggested_name("") is None
    assert _safe_suggested_name(None) is None
    assert _safe_suggested_name("A" * 60) is None       # too long