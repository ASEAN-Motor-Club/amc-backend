"""Tests for the Shuto live-player board cog.

No DB needed: the cog only reads the native game web API and edits one Discord
message in place.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from amc_cogs.shuto_status import BOARD_FOOTER, BOARD_TITLE, ShutoStatusCog


class _History:
    def __init__(self, messages):
        self.messages = messages

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for message in self.messages:
            yield message


def _cog():
    bot = MagicMock()
    bot.shuto_http_client_game = MagicMock()
    bot.user = object()
    return ShutoStatusCog(bot, channel_id=123, api_password="test-pw")


def _bot_message(title, footer, user):
    message = MagicMock()
    message.author = user
    embed = MagicMock()
    embed.title = title
    embed.footer.text = footer
    message.embeds = [embed]
    message.edit = AsyncMock()
    return message


@pytest.mark.asyncio
async def test_fetch_players_uses_direct_api_with_password():
    cog = _cog()
    payload = {
        "succeeded": True,
        "data": {
            "1": {"unique_id": "1", "name": "Alice"},
            "2": {"unique_id": "2", "name": "Bob"},
        },
    }
    with patch(
        "amc_cogs.shuto_status.game_api_request",
        new=AsyncMock(return_value=payload),
    ) as request:
        players = await cog._fetch_players()

    request.assert_awaited_once_with(
        cog.bot.shuto_http_client_game, "/player/list", password="test-pw"
    )
    assert [player["name"] for _, player in players] == ["Alice", "Bob"]


@pytest.mark.asyncio
async def test_fetch_players_raises_on_api_error():
    cog = _cog()
    payload = {"succeeded": False, "message": "Invalid password"}
    with (
        patch(
            "amc_cogs.shuto_status.game_api_request",
            new=AsyncMock(return_value=payload),
        ),
        pytest.raises(RuntimeError),
    ):
        await cog._fetch_players()


def test_build_embed_matches_prod_board_layout():
    cog = _cog()
    embed = cog._build_embed([("1", {"unique_id": "1", "name": "Alice"})])
    fields = {field.name: field.value for field in embed.fields}
    assert embed.title == "Active Players"
    assert embed.footer.text == BOARD_FOOTER
    assert fields["Player Count"] == "1"
    assert "Alice" in (fields["Players"] or "")


def test_build_embed_no_players():
    embed = _cog()._build_embed([])
    fields = {field.name: field.value for field in embed.fields}
    assert fields["Player Count"] == "0"
    assert fields["Players"] == "No players online"


def test_build_embed_offline():
    embed = _cog()._build_embed([], offline=True)
    assert embed.title == BOARD_TITLE
    assert embed.footer.text == BOARD_FOOTER
    assert any(
        field.name == "Status" and "unreachable" in (field.value or "")
        for field in embed.fields
    )


@pytest.mark.asyncio
async def test_upsert_edits_only_its_own_message_in_shared_channel():
    cog = _cog()
    prod_message = _bot_message(
        "Active Players", "Updated every 30 seconds", cog.bot.user
    )
    shuto_message = _bot_message(BOARD_TITLE, BOARD_FOOTER, cog.bot.user)
    channel = MagicMock()
    channel.history.return_value = _History([prod_message, shuto_message])

    await cog._upsert_message(channel, cog._build_embed([]))

    assert cog.last_embed_message is shuto_message
    shuto_message.edit.assert_awaited_once()
    prod_message.edit.assert_not_called()
    channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_sends_new_message_when_none_exists():
    cog = _cog()
    channel = MagicMock()
    channel.history.return_value = _History([])
    sent = MagicMock()
    channel.send = AsyncMock(return_value=sent)
    embed = cog._build_embed([])

    await cog._upsert_message(channel, embed)

    assert cog.last_embed_message is sent
    channel.send.assert_awaited_once_with(embed=embed)
