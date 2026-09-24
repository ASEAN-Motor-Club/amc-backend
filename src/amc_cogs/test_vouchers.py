"""Tests for the VouchersCog (/my_vouchers).

Pure-mock tests: the cog reads the DB via async Django ORM, which runs on
thread-pool connections that cannot see a plain django_db test transaction,
and this repo's sandbox breaks the transaction=True teardown flush — so we
mock the ORM surface instead (see test_exam.py header for the same trade-off).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from amc_cogs.vouchers import VouchersCog


def _interaction(user_id=123):
    interaction = MagicMock()
    interaction.response = SimpleNamespace(send_message=AsyncMock())
    interaction.user = MagicMock()
    interaction.user.id = user_id
    return interaction


def _player(pk=1):
    player = MagicMock()
    player.pk = pk
    return player


def test_cog_registers_command():
    cog = VouchersCog(MagicMock())
    assert any(cmd.name == "my_vouchers" for cmd in cog.get_app_commands())


def test_my_vouchers_requires_linked_account(monkeypatch):
    from amc.models import Player

    cog = VouchersCog(MagicMock())
    interaction = _interaction(user_id=999999)
    monkeypatch.setattr(
        Player.objects, "aget", AsyncMock(side_effect=Player.DoesNotExist)
    )
    asyncio.run(VouchersCog.my_vouchers.callback(cog, interaction))
    interaction.response.send_message.assert_awaited_once()
    text = interaction.response.send_message.await_args.args[0]
    assert "linked game account" in text
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


def test_my_vouchers_empty(monkeypatch):
    from amc.models import Player, Voucher

    cog = VouchersCog(MagicMock())
    interaction = _interaction(user_id=42)

    monkeypatch.setattr(
        Player.objects, "aget", AsyncMock(return_value=_player())
    )

    mock_qs = MagicMock()
    mock_qs.order_by = MagicMock(return_value=mock_qs)
    mock_qs.__aiter__ = lambda self=None: _aiter([])
    monkeypatch.setattr(Voucher.objects, "filter", MagicMock(return_value=mock_qs))

    asyncio.run(VouchersCog.my_vouchers.callback(cog, interaction))
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert "no unclaimed vouchers" in interaction.response.send_message.await_args.args[0]


def _aiter(items):
    async def it():
        for item in items:
            yield item

    return it()


def test_my_vouchers_lists_unclaimed_only(monkeypatch):
    from amc.models import Player, Voucher

    cog = VouchersCog(MagicMock())
    interaction = _interaction(user_id=42)

    mock_player = _player()

    monkeypatch.setattr(
        Player.objects, "aget", AsyncMock(return_value=mock_player)
    )

    mine = [MagicMock(code="TW-AAA111", amount=100000, reason="Tuning Workshop", created_at=None),
            MagicMock(code="TW-BBB222", amount=200000, reason="Tuning Workshop", created_at=None)]

    def _fake_filter(*a, **k):
        assert k.get("claimed_at__isnull") is True
        assert k.get("player") is mock_player
        mock_qs = MagicMock()
        mock_qs.order_by = MagicMock(return_value=mock_qs)
        mock_qs.__aiter__ = lambda self=None: _aiter(mine)
        return mock_qs

    monkeypatch.setattr(Voucher.objects, "filter", _fake_filter)

    asyncio.run(VouchersCog.my_vouchers.callback(cog, interaction))
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    embed = kwargs["embed"]
    assert "TW-AAA111" in embed.description
    assert "TW-BBB222" in embed.description
    assert "$300,000" in embed.footer.text
