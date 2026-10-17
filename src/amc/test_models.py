from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from django.contrib.gis.geos import Point
from asgiref.sync import sync_to_async
from amc.factories import (
    CharacterFactory,
    ChampionshipFactory,
    ChampionshipPointFactory,
)
from amc.models import CharacterLocation, Character, Player


class CharacterLocationTestCase(TestCase):
    async def test_activity(self):
        character = await sync_to_async(CharacterFactory)()
        for n in range(0, 1000):
            await CharacterLocation.objects.acreate(
                timestamp=timezone.now() - timedelta(hours=1) + timedelta(seconds=n),
                character=character,
                location=Point(10000 + n * 100, 10000 + n * 100, 0),
            )
        is_online, is_active = await CharacterLocation.get_character_activity(
            character,
            timezone.now() - timedelta(hours=1),
            timezone.now() - timedelta(seconds=0),
        )
        self.assertTrue(is_online)
        self.assertTrue(is_active)

    async def test_activity_offline(self):
        character = await sync_to_async(CharacterFactory)()
        is_online, is_active = await CharacterLocation.get_character_activity(
            character,
            timezone.now() - timedelta(hours=1),
            timezone.now() - timedelta(seconds=0),
        )
        self.assertFalse(is_online)
        self.assertFalse(is_active)

    async def test_activity_afk(self):
        character = await sync_to_async(CharacterFactory)()
        for n in range(0, 1000):
            await CharacterLocation.objects.acreate(
                timestamp=timezone.now() - timedelta(hours=1) + timedelta(seconds=n),
                character=character,
                location=Point(10000 + n * 0.1, 10000 + n * 0.1, 0),
            )
        is_online, is_active = await CharacterLocation.get_character_activity(
            character,
            timezone.now() - timedelta(hours=1),
            timezone.now() - timedelta(seconds=0),
        )
        self.assertTrue(is_online)
        self.assertFalse(is_active)

    async def test_batch_character_activity(self):
        """Batch version should produce same results as individual calls."""
        active_char = await sync_to_async(CharacterFactory)()
        afk_char = await sync_to_async(CharacterFactory)()
        offline_char = await sync_to_async(CharacterFactory)()

        # Active character: large movement
        for n in range(0, 100):
            await CharacterLocation.objects.acreate(
                timestamp=timezone.now() - timedelta(hours=1) + timedelta(seconds=n),
                character=active_char,
                location=Point(10000 + n * 100, 10000 + n * 100, 0),
            )

        # AFK character: tiny movement
        for n in range(0, 100):
            await CharacterLocation.objects.acreate(
                timestamp=timezone.now() - timedelta(hours=1) + timedelta(seconds=n),
                character=afk_char,
                location=Point(10000 + n * 0.1, 10000 + n * 0.1, 0),
            )

        # Offline character: no locations at all

        start = timezone.now() - timedelta(hours=1)
        end = timezone.now()

        result = await CharacterLocation.batch_get_character_activity(
            [active_char, afk_char, offline_char],
            start,
            end,
        )

        # Active character
        self.assertEqual(result[active_char.id], (True, True))
        # AFK character (online but not active)
        self.assertEqual(result[afk_char.id], (True, False))
        # Offline character
        self.assertEqual(result[offline_char.id], (False, False))


class ChampionshipTestCase(TestCase):
    async def test_award_personal_prizes(self):
        championship = await sync_to_async(ChampionshipFactory)()
        await sync_to_async(ChampionshipPointFactory)(
            championship=championship,
        )
        await sync_to_async(ChampionshipPointFactory)(
            championship=championship,
        )
        prizes = await championship.calculate_personal_prizes()
        print(prizes)

    async def test_award_team_prizes(self):
        championship = await sync_to_async(ChampionshipFactory)()
        p1 = await sync_to_async(ChampionshipPointFactory)(
            championship=championship,
        )
        await sync_to_async(ChampionshipPointFactory)(
            championship=championship, team=p1.team
        )
        await sync_to_async(ChampionshipPointFactory)(
            championship=championship,
        )
        prizes = await championship.calculate_team_prizes()
        print(prizes)


class CharacterMangerTestCase(TestCase):
    async def test_change_name(self):
        character1, *_ = await Character.objects.aget_or_create_character_player(
            "test", 123, character_guid=234
        )
        character2, *_ = await Character.objects.aget_or_create_character_player(
            "test2", 123, character_guid=234
        )
        self.assertEqual(character1.id, character2.id)

    async def test_blank_guid_line_does_not_rewrite_name(self):
        """A blank display name (wanted player mid-chase) resolves by GUID but
        must NOT rewrite Character.name to ''."""
        character, *_ = await Character.objects.aget_or_create_character_player(
            "TestPlayer", 321, character_guid="guid-blank-1"
        )
        resolved, _, _, _ = await Character.objects.aget_or_create_character_player(
            "", 321, character_guid="guid-blank-1"
        )
        self.assertEqual(resolved.id, character.id)
        await resolved.arefresh_from_db()
        self.assertEqual(resolved.name, "TestPlayer")

    async def test_blank_name_without_guid_is_unresolvable(self):
        """Blank name + no GUID: unresolvable — no rename, no orphan create."""
        from amc.factories import CharacterFactory, PlayerFactory

        player = await sync_to_async(PlayerFactory)(unique_id="76561199000000999")
        await sync_to_async(CharacterFactory)(
            player=player, name="RealName", guid="guid-blank-2"
        )
        character, _, created, _ = await (
            Character.objects.aget_or_create_character_player("", "76561199000000999")
        )
        self.assertIsNone(character)
        self.assertFalse(created)
        row = await Character.objects.aget(guid="guid-blank-2")
        self.assertEqual(row.name, "RealName")
        await Player.objects.filter(unique_id="76561199000000999").adelete()

    async def test_add_guid(self):
        # Create a legacy GUID-less character directly (bypassing the manager)
        from amc.models import Player

        player, _ = await Player.objects.aget_or_create(unique_id=123)
        legacy = await Character.objects.acreate(name="test", player=player, guid=None)

        # Now provide a GUID — the legacy character should be claimed
        character, *_ = await Character.objects.aget_or_create_character_player(
            "test", 123, character_guid=234
        )
        self.assertEqual(legacy.id, character.id)
        self.assertEqual(character.guid, "234")

    async def test_missing_guid(self):
        character1, *_ = await Character.objects.aget_or_create_character_player(
            "test", 123, character_guid=234
        )
        character2, *_ = await Character.objects.aget_or_create_character_player(
            "test", 123
        )
        self.assertEqual(character1.id, character2.id)

    async def test_no_guid_falls_back_to_existing_guidful_character_on_rename(self):
        """When no GUID is provided and the name doesn't match, reuse the
        player's existing GUID-ful character instead of creating a duplicate."""
        original, *_ = await Character.objects.aget_or_create_character_player(
            "OldName", 123, character_guid="AAAA1111BBBB2222CCCC3333DDDD4444"
        )
        # Player logs in with a new name but GUID resolution failed
        found, *_ = await Character.objects.aget_or_create_character_player(
            "NewName", 123
        )
        # Should reuse the existing character, not create a new one
        self.assertEqual(found.id, original.id)
        self.assertEqual(found.guid, "AAAA1111BBBB2222CCCC3333DDDD4444")
        # Name should be updated to the new name
        await found.arefresh_from_db()
        self.assertEqual(found.name, "NewName")

    async def test_no_guid_returns_none_when_player_has_no_characters(self):
        """When no GUID is provided and the player has no characters at all,
        no character is created (to avoid orphan GUID-less duplicates)."""
        found, player, created, _ = await Character.objects.aget_or_create_character_player(
            "BrandNewPlayer", 999
        )
        self.assertFalse(created)
        self.assertIsNone(found)

    async def test_new_alt(self):
        character1, *_ = await Character.objects.aget_or_create_character_player(
            "test", 123, character_guid=234
        )
        character2, *_ = await Character.objects.aget_or_create_character_player(
            "test", 123, character_guid=345
        )
        self.assertNotEqual(character1.id, character2.id)

    async def test_guid_conflict_preserves_existing_and_deletes_orphan(self):
        """When a second character tries to save a GUID that already belongs to
        another character, the existing owner keeps its GUID and the orphan is deleted."""
        from unittest.mock import AsyncMock, patch

        from amc.models import Player

        # Create the authoritative character with GUID via the normal path
        original, *_ = await Character.objects.aget_or_create_character_player(
            "test", 123, character_guid="AAAA1111BBBB2222CCCC3333DDDD4444"
        )
        self.assertEqual(original.guid, "AAAA1111BBBB2222CCCC3333DDDD4444")

        # Create a GUID-less impostor directly (bypassing the rename-safe fallback)
        player = await Player.objects.aget(unique_id=123)
        impostor = await Character.objects.acreate(name="test2", player=player, guid=None)
        self.assertIsNone(impostor.guid)
        self.assertNotEqual(original.id, impostor.id)
        orphan_id = impostor.id

        # Simulate _login_guid_dependent_actions trying to assign the same GUID
        from amc.tasks import _login_guid_dependent_actions

        mock_http = AsyncMock()
        with patch(
            "amc.tasks._resolve_guid",
            return_value=("AAAA1111BBBB2222CCCC3333DDDD4444", {}),
        ):
            with patch("amc.tasks.refresh_player_name", new_callable=AsyncMock):
                await _login_guid_dependent_actions(
                    impostor,
                    player,
                    "test2",
                    123,
                    mock_http,
                    mock_http,
                    False,
                )

        # Original must still own the GUID
        await original.arefresh_from_db()
        self.assertEqual(original.guid, "AAAA1111BBBB2222CCCC3333DDDD4444")

        # Orphan character should be deleted
        self.assertFalse(
            await Character.objects.filter(id=orphan_id).aexists(),
            f"Orphan character {orphan_id} should have been deleted",
        )
