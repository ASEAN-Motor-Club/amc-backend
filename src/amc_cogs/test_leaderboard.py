"""Tests for the leaderboard cog gov-employee board card."""

import asyncio
import io
import json
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from asgiref.sync import sync_to_async
from django.test import TestCase

from amc.factories import CharacterFactory
from amc.models import GovContributionLog, Player
from amc_cogs.leaderboard import (
    AVATAR_SIZE,
    GOV_BOARD_FILE,
    LeaderboardCog,
    _circle_rgba,
    _fmt_money,
    _render_gov_board_png,
)


def _make_cog() -> LeaderboardCog:
    """Cog instance without __init__ (avoids starting the hourly loop)."""
    cog = LeaderboardCog.__new__(LeaderboardCog)
    cog.bot = MagicMock()
    cog.leaderboard_channel_id = 123
    cog._avatar_cache = {}
    return cog


def _png_bytes(width=64, height=64, rgb=(0.9, 0.2, 0.2)) -> bytes:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fig = Figure(figsize=(width / 10, height / 10), dpi=10)
    FigureCanvasAgg(fig)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor(rgb)
    ax.axis("off")
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()


class FmtMoneyTests(TestCase):
    def test_millions(self):
        self.assertEqual(_fmt_money(4_500_000), "$4.5M")

    def test_exact_million(self):
        self.assertEqual(_fmt_money(1_000_000), "$1M")

    def test_thousands(self):
        self.assertEqual(_fmt_money(421_000), "$421K")

    def test_small(self):
        self.assertEqual(_fmt_money(999), "$999")


class CircleMaskTests(TestCase):
    def test_masks_corners_keeps_center(self):
        rgba = _circle_rgba(_png_bytes())
        self.assertEqual(rgba.ndim, 3)
        self.assertEqual(rgba.shape[2], 4)
        h, w = rgba.shape[:2]
        # antialiased circular mask: corners transparent, center opaque
        self.assertEqual(rgba[0, 0, 3], 0.0)
        self.assertEqual(rgba[h - 1, w - 1, 3], 0.0)
        self.assertEqual(rgba[h // 2, w // 2, 3], 1.0)

    def test_default_avatar_256_normalized_to_avatar_size(self):
        """Discord CDN serves default avatars at 256px regardless of ?size=;
        the mask normalizes to AVATAR_SIZE so all discs render the same size."""
        rgba = _circle_rgba(_png_bytes(256, 256))
        self.assertEqual(rgba.shape[0], AVATAR_SIZE)
        self.assertEqual(rgba.shape[1], AVATAR_SIZE)
        self.assertEqual(rgba[0, 0, 3], 0.0)
        self.assertEqual(rgba[AVATAR_SIZE // 2, AVATAR_SIZE // 2, 3], 1.0)


class RenderGovBoardTests(TestCase):
    def _row(self, name, level, value):
        return {"name": name, "level": level, "value": value, "avatar_rgba": None}

    def test_smoke_produces_png(self):
        all_rows = [self._row("PlayerOne", 3, 1_200_000), self._row("PlayerTwo", 1, 90_000)]
        monthly_rows = [self._row("PlayerTwo", 1, 90_000)]
        buf = _render_gov_board_png(all_rows, monthly_rows, "September")
        data = buf.getvalue()
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertGreater(len(data), 10_000)

    def test_empty_panels_render_placeholder_text(self):
        buf = _render_gov_board_png([], [], "September")
        self.assertTrue(buf.getvalue().startswith(b"\x89PNG"))

    def test_real_avatar_row(self):
        rgba = _circle_rgba(_png_bytes(128, 128))
        all_rows = [{"name": "AvatarGuy", "level": 2, "value": 600_000, "avatar_rgba": rgba}]
        buf = _render_gov_board_png(all_rows, [], "September")
        self.assertTrue(buf.getvalue().startswith(b"\x89PNG"))


class CreateLeaderboardEmbedsTests(TestCase):
    def test_no_gov_data_leaves_embed_without_image(self):
        cog = _make_cog()
        with (
            patch(
                "amc_cogs.leaderboard.top_gov_all_time",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "amc_cogs.leaderboard.top_gov_monthly",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            embed, gov_file = asyncio.run(cog.create_leaderboard_embeds())

        self.assertIsNone(gov_file)
        self.assertIsNone(embed.image.url)

    def test_gov_board_attached_when_data_exists(self):
        cog = _make_cog()

        async def seed():
            player = await Player.objects.acreate(
                unique_id=987654321001,
                discord_user_id=111222333444555666,
                discord_name="avatar guy",
            )
            character = await sync_to_async(CharacterFactory)(
                player=player,
                name="[GOV3] AvatarGuy",
                guid="gov-board-guid-1",
            )
            await GovContributionLog.objects.acreate(
                character=character, contribution=1_500_000
            )
            return character

        async def run():
            await seed()
            try:
                with patch.object(
                    cog, "_get_avatar", new_callable=AsyncMock
                ) as mock_avatar:
                    mock_avatar.return_value = _png_bytes(128, 128)
                    return await cog.create_leaderboard_embeds()
            finally:
                await Player.objects.filter(unique_id=987654321001).adelete()

        embed, gov_file = asyncio.run(run())

        self.assertIsNotNone(gov_file)
        self.assertEqual(gov_file.filename, GOV_BOARD_FILE)
        self.assertEqual(embed.image.url, f"attachment://{GOV_BOARD_FILE}")
        self.assertEqual(embed.footer.text, "Updates every hour • Only top 10 shown")


class EditMessageWithFileTests(TestCase):
    def test_uses_http_multipart_with_new_file(self):
        cog = _make_cog()
        cog.bot.http.edit_message = AsyncMock()
        message = MagicMock()
        message.channel.id = 111
        message.id = 222
        file = discord.File(io.BytesIO(b"\x89PNG-fake-bytes"), filename=GOV_BOARD_FILE)
        embed = discord.Embed(title="test")

        asyncio.run(cog._edit_message_with_file(message, embed, file))

        cog.bot.http.edit_message.assert_awaited_once()
        args, kwargs = cog.bot.http.edit_message.await_args
        self.assertEqual(args, (111, 222))
        params = kwargs["params"]
        self.assertEqual(len(params.files), 1)
        self.assertEqual(params.files[0].filename, GOV_BOARD_FILE)
        # Multipart path: payload rides as JSON in multipart[0]. Its
        # attachments array lists ONLY the new file (id 0 = index of the new
        # upload), so the previous card attachment is replaced.
        payload = json.loads(params.multipart[0]["value"])
        self.assertEqual(
            [a["filename"] for a in payload["attachments"]],
            [GOV_BOARD_FILE],
        )
        self.assertEqual(payload["attachments"][0]["id"], 0)
        self.assertIn("embeds", payload)
