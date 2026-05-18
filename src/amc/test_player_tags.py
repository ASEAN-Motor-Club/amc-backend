import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from amc.player_tags import (
    strip_all_tags,
    build_display_name,
    refresh_player_name,
)


@pytest.fixture(autouse=True)
def clear_cache():
    from django.core.cache import cache
    cache.clear()


# --- build_display_name ---


def test_build_display_name_no_tags():
    assert build_display_name("PlayerOne") == "PlayerOne"
    assert (
        build_display_name("PlayerOne", has_custom_parts=False, gov_level=0)
        == "PlayerOne"
    )


def test_build_display_name_mod_only():
    assert build_display_name("PlayerOne", has_custom_parts=True) == "[M] PlayerOne"


def test_build_display_name_gov_only():
    assert build_display_name("PlayerOne", gov_level=3) == "[G3] PlayerOne"


def test_build_display_name_gov_multi_digit():
    assert build_display_name("PlayerOne", gov_level=23) == "[G23] PlayerOne"


def test_build_display_name_mod_and_gov():
    assert (
        build_display_name("PlayerOne", has_custom_parts=True, gov_level=3)
        == "[MG3] PlayerOne"
    )


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_police_only():
    assert build_display_name("PlayerOne", police_level=1) == "[P1] PlayerOne"


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_police_and_mods():
    assert (
        build_display_name("PlayerOne", police_level=1, has_custom_parts=True)
        == "[MP1] PlayerOne"
    )


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_police_and_gov():
    assert (
        build_display_name("PlayerOne", police_level=1, gov_level=3)
        == "[P1G3] PlayerOne"
    )


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_police_mods_and_gov():
    assert (
        build_display_name(
            "PlayerOne", police_level=1, has_custom_parts=True, gov_level=3
        )
        == "[MP1G3] PlayerOne"
    )


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_police_suppresses_crim():
    """Police membership suppresses criminal tag."""
    assert (
        build_display_name("PlayerOne", police_level=1, criminal_level=1)
        == "[P1] PlayerOne"
    )


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_police_suppresses_crim_with_gov():
    """Police suppresses criminal tag (gov does not)."""
    assert (
        build_display_name("PlayerOne", police_level=1, criminal_level=3, gov_level=3)
        == "[P1G3] PlayerOne"
    )


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_crim_level_1():
    assert build_display_name("PlayerOne", criminal_level=1) == "[C1] PlayerOne"


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_crim_level_5():
    assert build_display_name("PlayerOne", criminal_level=5) == "[C5] PlayerOne"


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_crim_multi_digit():
    assert build_display_name("PlayerOne", criminal_level=12) == "[C12] PlayerOne"


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_crim_not_suppressed_without_police():
    """Criminal tag is NOT suppressed when player is not police."""
    assert (
        build_display_name("PlayerOne", criminal_level=2, has_custom_parts=True)
        == "[MC2] PlayerOne"
    )


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_crim_and_mods():
    assert (
        build_display_name("PlayerOne", criminal_level=1, has_custom_parts=True)
        == "[MC1] PlayerOne"
    )


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_crim_not_suppressed_by_gov():
    """Criminal tag is NOT suppressed by gov (only police suppresses it)."""
    assert (
        build_display_name("PlayerOne", criminal_level=3, gov_level=3)
        == "[C3G3] PlayerOne"
    )


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_all_active_crim_not_suppressed():
    """All flags active: criminal NOT suppressed by gov, so [MC2G3]."""
    assert (
        build_display_name(
            "PlayerOne", criminal_level=2, has_custom_parts=True, gov_level=3
        )
        == "[MC2G3] PlayerOne"
    )


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_all_flags_with_police():
    """All flags + police: criminal suppressed, so [MP1G3]."""
    assert (
        build_display_name(
            "PlayerOne",
            criminal_level=5,
            has_custom_parts=True,
            police_level=1,
            gov_level=3,
        )
        == "[MP1G3] PlayerOne"
    )


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_police_level_2():
    assert build_display_name("PlayerOne", police_level=2) == "[P2] PlayerOne"


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_police_level_10():
    assert build_display_name("PlayerOne", police_level=10) == "[P10] PlayerOne"


def test_build_display_name_wanted_only():
    assert (
        build_display_name("PlayerOne", wanted_stars=5)
        == "[R*****] PlayerOne"
    )


def test_build_display_name_wanted_w1():
    assert (
        build_display_name("PlayerOne", wanted_stars=1)
        == "[R*] PlayerOne"
    )


def test_build_display_name_wanted_w3():
    assert (
        build_display_name("PlayerOne", wanted_stars=3)
        == "[R***] PlayerOne"
    )


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_wanted_and_crim():
    assert (
        build_display_name("PlayerOne", criminal_level=3, wanted_stars=4)
        == "[R****C3] PlayerOne"
    )


def test_build_display_name_wanted_and_mods():
    assert (
        build_display_name("PlayerOne", has_custom_parts=True, wanted_stars=2)
        == "[RM**] PlayerOne"
    )


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_wanted_with_police():
    """Wanted tag shows even when police is active."""
    assert (
        build_display_name("PlayerOne", police_level=1, wanted_stars=5)
        == "[RP1*****] PlayerOne"
    )


def test_build_display_name_wanted_with_gov():
    """Wanted tag shows even when gov is active."""
    assert (
        build_display_name("PlayerOne", gov_level=3, wanted_stars=5)
        == "[R*****G3] PlayerOne"
    )


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_wanted_with_police_and_gov():
    """Wanted tag shows even when both police and gov are active."""
    assert (
        build_display_name("PlayerOne", police_level=1, gov_level=3, wanted_stars=5)
        == "[RP1*****G3] PlayerOne"
    )


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_wanted_with_crim_police_gov():
    """Wanted shows alongside police+gov; crim suppressed."""
    assert (
        build_display_name(
            "PlayerOne", criminal_level=3, police_level=1, gov_level=3, wanted_stars=5
        )
        == "[RP1*****G3] PlayerOne"
    )


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_wanted_with_crim_mods():
    assert (
        build_display_name(
            "PlayerOne", criminal_level=1, has_custom_parts=True, wanted_stars=3
        )
        == "[RM***C1] PlayerOne"
    )


def test_build_display_name_rp_mode_only():
    assert build_display_name("PlayerOne", rp_mode=True) == "[R] PlayerOne"


def test_build_display_name_rp_mode_with_mods_and_gov():
    """R goes first: [RMG3] PlayerOne."""
    assert (
        build_display_name(
            "PlayerOne", rp_mode=True, has_custom_parts=True, gov_level=3
        )
        == "[RMG3] PlayerOne"
    )


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_rp_mode_with_police_wanted_gov():
    """R prepended before P/stars/G: [RP1**G3] PlayerOne (crim suppressed by police)."""
    assert (
        build_display_name(
            "PlayerOne",
            rp_mode=True,
            police_level=1,
            wanted_stars=2,
            criminal_level=0,
            gov_level=3,
        )
        == "[RP1**G3] PlayerOne"
    )


def test_build_display_name_rp_mode_default_false():
    """Not passing rp_mode leaves the tag unaffected."""
    assert (
        build_display_name("PlayerOne", has_custom_parts=True) == "[M] PlayerOne"
    )


# --- guild suffix ---


def test_build_display_name_guild_only():
    assert (
        build_display_name("PlayerOne", guild_abbreviation="GOP")
        == "PlayerOne[GOP]"
    )


def test_build_display_name_guild_with_mod():
    assert (
        build_display_name("PlayerOne", has_custom_parts=True, guild_abbreviation="GOP")
        == "[M] PlayerOne[GOP]"
    )


def test_build_display_name_guild_with_mod_and_gov():
    assert (
        build_display_name(
            "PlayerOne", has_custom_parts=True, gov_level=3, guild_abbreviation="RCL"
        )
        == "[MG3] PlayerOne[RCL]"
    )


@pytest.mark.skip(reason="player tags feature on hold")
def test_build_display_name_guild_with_all_flags():
    assert (
        build_display_name(
            "PlayerOne",
            rp_mode=True,
            has_custom_parts=True,
            police_level=1,
            wanted_stars=2,
            gov_level=3,
            guild_abbreviation="TXI",
        )
        == "[RMP1**G3] PlayerOne[TXI]"
    )


def test_build_display_name_guild_none():
    assert build_display_name("PlayerOne", guild_abbreviation=None) == "PlayerOne"


# --- strip_all_tags ---


def test_strip_new_format():
    assert strip_all_tags("[M] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[G3] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MG3] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[C] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[CM] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[CMG23] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[P] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MP] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MPG3] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[PG3] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[C1] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[C5] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[C12] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MC2] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MC1G3] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[*****] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[W5] PlayerOne") == "PlayerOne"  # legacy W-prefix
    assert strip_all_tags("[W12] PlayerOne") == "PlayerOne"  # legacy W-prefix
    assert strip_all_tags("[C3*****] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MC1***] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[\u2605\u2605\u2605\u2605\u2605] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MC1\u2605\u2605\u2605\u2605] PlayerOne") == "PlayerOne"
    assert (
        strip_all_tags("[P1\u2605\u2605\u2605\u2605\u2605G3] PlayerOne") == "PlayerOne"
    )
    # RP mode tag
    assert strip_all_tags("[R] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[RM] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[RMG3] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[RP1**G3] PlayerOne") == "PlayerOne"


def test_strip_legacy_format():
    assert strip_all_tags("[MODS] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[GOV1] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MODS] [GOV3] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[CRIM] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[CRIM] [MODS] [GOV3] PlayerOne") == "PlayerOne"
    # [DOT] is a permanent team tag — it must NOT be stripped
    assert strip_all_tags("[DOT] PlayerOne") == "[DOT] PlayerOne"


def test_strip_legacy_subscript_format():
    assert strip_all_tags("[G₃] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MG₃] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[CMG₂₃] PlayerOne") == "PlayerOne"


def test_strip_all_tags_preserves_base_name():
    assert strip_all_tags("PlayerOne [123]") == "PlayerOne [123]"
    assert strip_all_tags("PlayerOne") == "PlayerOne"


def test_strip_guild_suffix():
    assert strip_all_tags("PlayerOne[GOP]") == "PlayerOne"
    assert strip_all_tags("[M] PlayerOne[GOP]") == "PlayerOne"
    assert strip_all_tags("[RP1**G3] PlayerOne[TXI]") == "PlayerOne"


def test_strip_guild_suffix_does_not_strip_permanent_tag():
    """[DOT] is a permanent team tag, not a guild suffix."""
    assert strip_all_tags("[DOT] PlayerOne") == "[DOT] PlayerOne"


# --- refresh_player_name integration tests ---


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_updates_custom_name(mock_set_name):
    from amc.factories import CharacterFactory, PlayerFactory
    from asgiref.sync import sync_to_async

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player,
        name="TestPlayer",
        guid="test-guid-1",
    )

    session = MagicMock()
    await refresh_player_name(character, session, has_custom_parts=True)

    await character.arefresh_from_db()
    assert character.custom_name == "[M] TestPlayer"
    from amc.player_tags import set_character_name

    set_character_name.assert_awaited_once_with(
        session, "test-guid-1", "[M] TestPlayer"
    )


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_preserves_mod_state_legacy(mock_set_name):
    """Preserves mod state from legacy [MODS] tag."""
    from amc.factories import CharacterFactory, PlayerFactory
    from asgiref.sync import sync_to_async

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player,
        name="TestPlayer",
        custom_name="[MODS] TestPlayer",
        guid="test-guid-2",
    )

    session = MagicMock()
    await refresh_player_name(character, session)

    await character.arefresh_from_db()
    assert character.custom_name == "[M] TestPlayer"


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
@patch("amc.player_tags.cache.aget", new_callable=AsyncMock)
async def test_refresh_player_name_skips_push_when_cached(mock_cache_aget, mock_set_name):
    """If the pushed_name cache already matches, skip the mod server call."""
    from amc.factories import CharacterFactory, PlayerFactory
    from asgiref.sync import sync_to_async

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player,
        name="TestPlayer",
        guid="test-guid-cache",
    )

    mock_cache_aget.return_value = "[M] TestPlayer"
    session = MagicMock()
    await refresh_player_name(character, session, has_custom_parts=True)

    mock_set_name.assert_not_awaited()
    mock_cache_aget.assert_awaited_once_with("pushed_name:test-guid-cache")


@pytest.mark.asyncio
@patch("amc.mod_server.cache.aset", new_callable=AsyncMock)
@patch("amc.mod_server.cache.aget", new_callable=AsyncMock)
async def test_get_player_singleflight(mock_cache_aget, mock_cache_aset):
    """Successful fetch is cached; subsequent calls hit the cache."""
    from amc.mod_server import get_player

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={"data": [{"PlayerName": "Hamster"}]})

    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_response
    mock_cm.__aexit__.return_value = False

    mock_session = MagicMock()
    mock_session.get.return_value = mock_cm

    mock_cache_aget.return_value = None

    result = await get_player(mock_session, "76561199552800721")

    assert result == {"PlayerName": "Hamster"}
    mock_cache_aset.assert_awaited_once_with(
        "mod_player_info:76561199552800721", {"PlayerName": "Hamster"}, timeout=5
    )


@pytest.mark.skip(reason="player tags feature on hold")
@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_police_suppresses_crim(mock_set_name):
    """Police session + criminal record → [P1] (criminal suppressed)."""
    from amc.factories import CharacterFactory, PlayerFactory
    from amc.models import CriminalRecord, PoliceSession
    from asgiref.sync import sync_to_async

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player,
        name="TestPlayer",
        guid="test-guid-police-2",
    )

    await PoliceSession.objects.acreate(character=character)
    await CriminalRecord.objects.acreate(
        character=character,
        reason="Money delivery",
        cleared_at=None,  # active record
    )

    session = MagicMock()
    await refresh_player_name(character, session)

    await character.arefresh_from_db()
    assert character.custom_name == "[P1] TestPlayer"
