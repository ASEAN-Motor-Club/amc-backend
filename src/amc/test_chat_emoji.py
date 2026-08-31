"""Tests for emoji stripping on the Discord → Motor Town chat relay.

Coverage:
- `amc.utils.strip_emojis`: Unicode emoji (incl. ZWJ sequences, flags, keycaps,
  skin tones), custom Discord emoji markup, non-emoji Unicode preservation.
- `amc_cogs.chat.ChatCog.on_message`: both relay paths (as-player and announce
  fallback) send sanitized text; emoji-only messages are dropped.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from django.conf import settings
from django.test import TestCase

from amc.models import Player
from amc.utils import strip_emojis
from amc_cogs.chat import ChatCog


class TestStripEmojis:
    def test_removes_basic_unicode_emoji(self):
        assert strip_emojis("hello 😀 world") == "hello world"

    def test_removes_zwj_sequences(self):
        assert strip_emojis("hi 👨‍👩‍👧‍👦!") == "hi !"

    def test_removes_flags(self):
        assert strip_emojis("gg 🇹🇭🇮🇩") == "gg"

    def test_removes_keycaps_and_skin_tones(self):
        assert strip_emojis("ok 1️⃣ 👍🏽") == "ok"

    def test_replaces_custom_discord_emoji_with_name(self):
        assert strip_emojis("nice <:juice:123456789012345678>") == "nice :juice:"
        assert strip_emojis("<a:spin:1> go") == ":spin: go"

    def test_preserves_non_emoji_unicode(self):
        assert strip_emojis("สวัสดี 안녕 你好 ça va") == "สวัสดี 안녕 你好 ça va"

    def test_collapses_double_spaces(self):
        assert strip_emojis("a 😀 b") == "a b"

    def test_trims_emoji_leftovers_at_ends(self):
        assert strip_emojis("  😀 leading and trailing 😀  ") == "leading and trailing"

    def test_plain_text_untouched(self):
        assert strip_emojis("plain (text) [ok] 123") == "plain (text) [ok] 123"


def _make_message(content, display_name="Player", user_id=111, name="player"):
    msg = MagicMock()
    msg.author.bot = False
    msg.author.id = user_id
    msg.author.display_name = display_name
    msg.author.name = name
    msg.channel.id = settings.DISCORD_GAME_CHAT_CHANNEL_ID
    msg.content = content
    return msg


class TestChatCogEmojiStripping(TestCase):
    async def test_fallback_path_strips_name_and_content(self):
        """Path B: unregistered user → announce('Name: msg') with emojis stripped."""
        cog = ChatCog(MagicMock())
        with (
            patch("amc_cogs.chat.Player") as player_mock,
            patch("amc_cogs.chat.get_players", new_callable=AsyncMock),
            patch("amc_cogs.chat.send_message_as_player", new_callable=AsyncMock) as smp,
            patch("amc_cogs.chat.announce", new_callable=AsyncMock) as ann,
        ):
            player_mock.DoesNotExist = Player.DoesNotExist
            player_mock.objects.aget = AsyncMock(side_effect=Player.DoesNotExist)

            await cog.on_message(_make_message("hi 😀", display_name="Robo😀"))

        ann.assert_awaited_once()
        assert ann.await_args.args[0] == "Robo: hi"
        smp.assert_not_awaited()

    async def test_fallback_path_name_fallback_when_only_emoji(self):
        """Display name that strips to empty falls back to the plain username."""
        cog = ChatCog(MagicMock())
        with (
            patch("amc_cogs.chat.Player") as player_mock,
            patch("amc_cogs.chat.get_players", new_callable=AsyncMock),
            patch("amc_cogs.chat.send_message_as_player", new_callable=AsyncMock) as smp,
            patch("amc_cogs.chat.announce", new_callable=AsyncMock) as ann,
        ):
            player_mock.DoesNotExist = Player.DoesNotExist
            player_mock.objects.aget = AsyncMock(side_effect=Player.DoesNotExist)

            await cog.on_message(_make_message("hello", display_name="😀🇹🇭", name="okplayer"))

        ann.assert_awaited_once()
        assert ann.await_args.args[0] == "okplayer: hello"
        smp.assert_not_awaited()

    async def test_as_player_path_strips_content(self):
        """Path A: registered + online → send_message_as_player with stripped content."""
        cog = ChatCog(MagicMock())
        player = MagicMock()
        player.unique_id = 999
        with (
            patch("amc_cogs.chat.Player") as player_mock,
            patch("amc_cogs.chat.get_players", new_callable=AsyncMock) as gp,
            patch("amc_cogs.chat.send_message_as_player", new_callable=AsyncMock) as smp,
            patch("amc_cogs.chat.announce", new_callable=AsyncMock) as ann,
        ):
            player_mock.DoesNotExist = Player.DoesNotExist
            player_mock.objects.aget = AsyncMock(return_value=player)
            gp.return_value = [("999", {"name": "InGameName"})]

            await cog.on_message(_make_message("hello 😀 <:tag:42>"))

        smp.assert_awaited_once_with(cog.bot.http_client_mod, "hello :tag:", "999")
        ann.assert_not_awaited()

    async def test_emoji_only_message_not_forwarded(self):
        """Nothing reaches the game when the message is emoji-only."""
        cog = ChatCog(MagicMock())
        with (
            patch("amc_cogs.chat.Player") as player_mock,
            patch("amc_cogs.chat.get_players", new_callable=AsyncMock),
            patch("amc_cogs.chat.send_message_as_player", new_callable=AsyncMock) as smp,
            patch("amc_cogs.chat.announce", new_callable=AsyncMock) as ann,
        ):
            player_mock.DoesNotExist = Player.DoesNotExist
            player_mock.objects.aget = AsyncMock(side_effect=Player.DoesNotExist)

            await cog.on_message(_make_message("😀 🇹🇭 👍🏽"))

        smp.assert_not_awaited()
        ann.assert_not_awaited()
