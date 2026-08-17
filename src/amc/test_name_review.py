"""Tests for the manual-review Rename/Whitelist button action handlers.

These handlers (`name_policy.apply_review_rename` / `apply_review_whitelist`)
are what the Discord `NameReviewView` buttons invoke. They need PostgreSQL
(ArrayField) so they run under the flake's pytest check / CI.
"""

import pytest
from asgiref.sync import sync_to_async
from django.test import override_settings

from amc.factories import CharacterFactory, PlayerFactory
from amc.models import ForcedNameLog, NameModerationLog, NameWhitelist
from amc.name_policy import apply_review_rename, apply_review_whitelist

pytestmark = pytest.mark.django_db


class _FakeHttp:
    """Minimal aiohttp-like client — records writes as async-context entries."""

    def __init__(self):
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append(("post", url))
        return _FakeResponse()

    def put(self, url, **kwargs):
        # aiohttp's put() returns an async context manager (not a coroutine).
        self.calls.append(("put", url))
        return _FakeResponse()


class _FakeResponse:
    """Async context manager standing in for aiohttp's response object."""

    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return ""


async def _make_review_log(player=None, character=None, base_name="BadName"):
    player = player or await sync_to_async(PlayerFactory)()
    character = character or await sync_to_async(CharacterFactory)(player=player)
    return (
        player,
        character,
        await NameModerationLog.objects.acreate(
            player=player,
            character=character,
            base_name=base_name,
            verdict_source="llm",
            is_violation=True,
            confidence=0.5,
            categories=["hate_slur"],
            action="manual_review",
            suggested_name="GoodName",
            reason="borderline",
        ),
    )


@override_settings(NAMER_ENABLED=True, NAMER_ANNOUNCE=False)
@pytest.mark.asyncio
async def test_apply_review_rename_sets_forced_name_and_audits():
    player, character, log = await _make_review_log()
    new_name = await apply_review_rename(
        log.pk, actor_discord_id=123456789,
        http_client=_FakeHttp(), http_client_mod=_FakeHttp(),
    )
    # forced_name set to the LLM suggestion
    fresh = await sync_to_async(type(player).objects.get)(pk=player.pk)
    assert fresh.forced_name == "GoodName"
    assert new_name == "GoodName"
    # ForcedNameLog written with the acting discord id
    fname = await ForcedNameLog.objects.aget(player=player)
    assert fname.action == "set"
    assert fname.new_name == "GoodName"
    assert fname.actor_discord_id == 123456789
    # Audit row flipped to rename
    refetched = await NameModerationLog.objects.aget(pk=log.pk)
    assert refetched.action == "rename"
    assert refetched.suggested_name == "GoodName"


@override_settings(NAMER_ENABLED=True, NAMER_ANNOUNCE=False)
@pytest.mark.asyncio
async def test_apply_review_rename_calls_refresh_after_lock():
    """Rename button must invoke the in-game refresh (chokepoint) via mod client."""
    player, character, log = await _make_review_log()
    fake = _FakeHttp()
    await apply_review_rename(
        log.pk, actor_discord_id=123456789,
        http_client=_FakeHttp(), http_client_mod=fake,
    )
    # refresh_player_name posts to the mod server; the fake records the attempt
    # then raises. It may or may not be recorded depending on where it throws —
    # assert the call was attempted.
    assert fake.calls  # at least one mod-server call attempted


@override_settings(NAMER_ENABLED=True)
@pytest.mark.asyncio
async def test_apply_review_whitelist_persists_per_player():
    player, character, log = await _make_review_log(base_name="SillyName")
    base = await apply_review_whitelist(log.pk, actor_discord_id=987654321)
    assert base == "sillyname"
    # Whitelist row exists, lowercased, scoped to this player
    wl = await NameWhitelist.objects.aget(player=player, name="sillyname")
    assert wl.added_by == 987654321
    # Audit row flipped to whitelist
    refetched = await NameModerationLog.objects.aget(pk=log.pk)
    assert refetched.action == "whitelist"


@override_settings(NAMER_ENABLED=True)
@pytest.mark.asyncio
async def test_apply_review_whitelist_idempotent():
    player, character, log = await _make_review_log(base_name="SameName")
    await apply_review_whitelist(log.pk, actor_discord_id=1)
    # Second call on a SEPARATE log for the same player+name must not duplicate
    _, _, log2 = await _make_review_log(player=player, base_name="SameName")
    await apply_review_whitelist(log2.pk, actor_discord_id=2)
    assert await NameWhitelist.objects.filter(player=player, name="samename").acount() == 1
