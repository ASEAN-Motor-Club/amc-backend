"""Autocomplete callback tests — guard against the shadowing-recursion pattern.

Both EventsCog and ModerationCog used to define a `player_autocomplete` METHOD
that called `await self.player_autocomplete(...)`, relying on the instance
attribute set in __init__ shadowing the method. If that instance attribute ever
went missing, the method recursed into itself forever (RecursionError). These
tests pop any instance attribute and assert the callback still terminates.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from django.test import TestCase

from amc_cogs.events import EventsCog
from amc_cogs.moderation import ModerationCog


class PlayerAutocompleteTerminatesTests(TestCase):
    async def test_moderation_cog_autocomplete_does_not_recurse(self):
        bot = MagicMock()
        bot.http_client_game = MagicMock()
        cog = ModerationCog(bot)
        # Simulate the pre-fix landmine: instance attribute gone, only the
        # class method remains. Pre-fix this recursed to RecursionError.
        vars(cog).pop("player_autocomplete", None)

        with patch("amc_cogs.utils.get_players", new=AsyncMock(return_value=[])):
            choices = await cog.player_autocomplete(MagicMock(), "")

        self.assertEqual(choices, [])

    async def test_events_cog_autocomplete_does_not_recurse(self):
        bot = MagicMock()
        bot.event_http_client_game = MagicMock()
        cog = EventsCog(bot)
        vars(cog).pop("player_autocomplete", None)

        with patch("amc_cogs.utils.get_players", new=AsyncMock(return_value=[])):
            choices = await cog.player_autocomplete(MagicMock(), "")

        self.assertEqual(choices, [])
