import time
from unittest.mock import patch, AsyncMock

from asgiref.sync import sync_to_async
from django.test import TestCase

from amc.factories import PlayerFactory, CharacterFactory
from amc.models import (
    Confiscation,
    PoliceSession,
)
from amc.webhook import handle_pickup_cargo
from amc.webhook_context import EventContext


def _pickup_event(
    character_guid, payment=5000, previous_owner_guid=None, cargo_key="Money"
):
    return {
        "hook": "ServerPickupCargo",
        "timestamp": int(time.time()),
        "data": {
            "CharacterGuid": str(character_guid),
            "Cargo": {
                "Net_CargoKey": cargo_key,
                "Net_Payment": payment,
                "PreviousOwnerCharacterGuid": previous_owner_guid,
            },
        },
    }


@patch("amc.handlers.police.send_system_message", new_callable=AsyncMock)
@patch("amc.handlers.police.send_fund_to_player_wallet", new_callable=AsyncMock)
@patch("amc.police.record_confiscation_for_level", new_callable=AsyncMock)
@patch(
    "amc.handlers.police.record_pd_fund_confiscation_income", new_callable=AsyncMock
)
@patch("amc.handlers.police.despawn_player_cargo", new_callable=AsyncMock)
@patch("amc.handlers.police.transfer_money", new_callable=AsyncMock)
@patch("amc.handlers.police.announce", new_callable=AsyncMock)
class ConfiscationHandlerTests(TestCase):
    """Tests for handle_pickup_cargo — police Money confiscation."""

    async def _setup_police_and_criminal(self):
        """Create a police officer (with active session) and a non-police player."""
        officer_player = await sync_to_async(PlayerFactory)()
        officer = await sync_to_async(CharacterFactory)(player=officer_player)
        await PoliceSession.objects.acreate(character=officer)

        criminal_player = await sync_to_async(PlayerFactory)()
        criminal = await sync_to_async(CharacterFactory)(player=criminal_player)
        return officer, criminal

    async def test_police_confiscates_money(
        self,
        mock_announce,
        mock_transfer,
        mock_despawn,
        mock_treasury,
        mock_level,
        mock_fund_wallet,
        mock_sys_msg,
    ):
        """Police picking up Money from non-police confiscates it into the PD fund (no officer reward by default)."""
        officer, criminal = await self._setup_police_and_criminal()

        event = _pickup_event(
            officer.guid,
            payment=10_000,
            previous_owner_guid=criminal.guid,
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        ctx = EventContext(http_client=mock_http, http_client_mod=mock_http_mod)
        await handle_pickup_cargo(event, officer.player, officer, ctx)

        # Confiscation record created
        self.assertEqual(await Confiscation.objects.acount(), 1)
        conf = await Confiscation.objects.afirst()
        self.assertEqual(conf.character_id, criminal.id)
        self.assertEqual(conf.officer_id, officer.id)
        self.assertEqual(conf.amount, 10_000)

        # Previous owner charged; no officer reward (share defaults to 0)
        mock_transfer.assert_called_once_with(
            mock_http_mod,
            -10_000,
            "Money Confiscated",
            str(criminal.player.unique_id),
        )

        # PD fund credited
        mock_treasury.assert_called_once_with(10_000, "Police Confiscation")

        # No officer wallet ledger entry or notification
        mock_fund_wallet.assert_not_called()
        mock_sys_msg.assert_not_called()

        # Cargo despawned
        mock_despawn.assert_called_once_with(mock_http_mod, str(officer.guid))

    async def test_officer_share_pays_reward(
        self,
        mock_announce,
        mock_transfer,
        mock_despawn,
        mock_treasury,
        mock_level,
        mock_fund_wallet,
        mock_sys_msg,
    ):
        """With POLICE_CONFISCATION_OFFICER_SHARE=0.5 the officer gets half, the rest goes to the PD fund."""
        from django.test import override_settings

        officer, criminal = await self._setup_police_and_criminal()

        event = _pickup_event(
            officer.guid,
            payment=10_000,
            previous_owner_guid=criminal.guid,
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        ctx = EventContext(http_client=mock_http, http_client_mod=mock_http_mod)
        with override_settings(POLICE_CONFISCATION_OFFICER_SHARE=0.5):
            await handle_pickup_cargo(event, officer.player, officer, ctx)

        # Charge + half-share reward
        mock_transfer.assert_any_call(
            mock_http_mod,
            5_000,
            "Confiscation Reward",
            str(officer.player.unique_id),
        )
        mock_fund_wallet.assert_called_once_with(5_000, officer, "Confiscation Reward")
        # Full amount still credited to the PD fund
        mock_treasury.assert_called_once_with(10_000, "Police Confiscation")

    async def test_non_police_no_confiscation(
        self,
        mock_announce,
        mock_transfer,
        mock_despawn,
        mock_treasury,
        mock_level,
        mock_fund_wallet,
        mock_sys_msg,
    ):
        """Non-police picking up Money should not trigger confiscation."""
        player = await sync_to_async(PlayerFactory)()
        character = await sync_to_async(CharacterFactory)(player=player)

        other_player = await sync_to_async(PlayerFactory)()
        other_char = await sync_to_async(CharacterFactory)(player=other_player)

        event = _pickup_event(
            character.guid,
            payment=5000,
            previous_owner_guid=other_char.guid,
        )
        ctx = EventContext(http_client=AsyncMock(), http_client_mod=AsyncMock())
        await handle_pickup_cargo(event, player, character, ctx)

        self.assertEqual(await Confiscation.objects.acount(), 0)
        mock_transfer.assert_not_called()

    async def test_non_money_cargo_no_confiscation(
        self,
        mock_announce,
        mock_transfer,
        mock_despawn,
        mock_treasury,
        mock_level,
        mock_fund_wallet,
        mock_sys_msg,
    ):
        """Police picking up non-Money cargo should not trigger confiscation."""
        officer, criminal = await self._setup_police_and_criminal()

        event = _pickup_event(
            officer.guid,
            payment=5000,
            previous_owner_guid=criminal.guid,
            cargo_key="oranges",
        )
        ctx = EventContext(http_client=AsyncMock(), http_client_mod=AsyncMock())
        await handle_pickup_cargo(event, officer.player, officer, ctx)

        self.assertEqual(await Confiscation.objects.acount(), 0)
        mock_transfer.assert_not_called()

    async def test_self_confiscation_blocked(
        self,
        mock_announce,
        mock_transfer,
        mock_despawn,
        mock_treasury,
        mock_level,
        mock_fund_wallet,
        mock_sys_msg,
    ):
        """Police picking up their own Money should not trigger confiscation."""
        officer, _ = await self._setup_police_and_criminal()

        event = _pickup_event(
            officer.guid,
            payment=5000,
            previous_owner_guid=officer.guid,
        )
        ctx = EventContext(http_client=AsyncMock(), http_client_mod=AsyncMock())
        await handle_pickup_cargo(event, officer.player, officer, ctx)

        self.assertEqual(await Confiscation.objects.acount(), 0)
        mock_transfer.assert_not_called()

    async def test_police_on_police_blocked(
        self,
        mock_announce,
        mock_transfer,
        mock_despawn,
        mock_treasury,
        mock_level,
        mock_fund_wallet,
        mock_sys_msg,
    ):
        """Police picking up Money from another police officer should not trigger confiscation."""
        officer1_player = await sync_to_async(PlayerFactory)()
        officer1 = await sync_to_async(CharacterFactory)(player=officer1_player)
        await PoliceSession.objects.acreate(character=officer1)

        officer2_player = await sync_to_async(PlayerFactory)()
        officer2 = await sync_to_async(CharacterFactory)(player=officer2_player)
        await PoliceSession.objects.acreate(character=officer2)

        event = _pickup_event(
            officer1.guid,
            payment=5000,
            previous_owner_guid=officer2.guid,
        )
        ctx = EventContext(http_client=AsyncMock(), http_client_mod=AsyncMock())
        await handle_pickup_cargo(event, officer1.player, officer1, ctx)

        self.assertEqual(await Confiscation.objects.acount(), 0)
        mock_transfer.assert_not_called()

    async def test_missing_previous_owner_guid(
        self,
        mock_announce,
        mock_transfer,
        mock_despawn,
        mock_treasury,
        mock_level,
        mock_fund_wallet,
        mock_sys_msg,
    ):
        """Missing PreviousOwnerCharacterGuid should not trigger confiscation."""
        officer, _ = await self._setup_police_and_criminal()

        event = _pickup_event(officer.guid, payment=5000, previous_owner_guid=None)
        ctx = EventContext(http_client=AsyncMock(), http_client_mod=AsyncMock())
        await handle_pickup_cargo(event, officer.player, officer, ctx)

        self.assertEqual(await Confiscation.objects.acount(), 0)
        mock_transfer.assert_not_called()

    async def test_zero_payment_no_confiscation(
        self,
        mock_announce,
        mock_transfer,
        mock_despawn,
        mock_treasury,
        mock_level,
        mock_fund_wallet,
        mock_sys_msg,
    ):
        """Zero-payment cargo should not trigger confiscation."""
        officer, criminal = await self._setup_police_and_criminal()

        event = _pickup_event(
            officer.guid,
            payment=0,
            previous_owner_guid=criminal.guid,
        )
        ctx = EventContext(http_client=AsyncMock(), http_client_mod=AsyncMock())
        await handle_pickup_cargo(event, officer.player, officer, ctx)

        self.assertEqual(await Confiscation.objects.acount(), 0)
        mock_transfer.assert_not_called()

    async def test_unknown_previous_owner_triggers_confiscation(
        self,
        mock_announce,
        mock_transfer,
        mock_despawn,
        mock_treasury,
        mock_level,
        mock_fund_wallet,
        mock_sys_msg,
    ):
        """Unknown PreviousOwnerCharacterGuid (not in DB) should still trigger confiscation without charging."""
        officer, _ = await self._setup_police_and_criminal()

        # Pass a guid that doesn't exist in DB
        event = _pickup_event(
            officer.guid, payment=5000, previous_owner_guid="UNKNOWN-GUID"
        )
        mock_http = AsyncMock()
        mock_http_mod = AsyncMock()
        ctx = EventContext(http_client=mock_http, http_client_mod=mock_http_mod)
        await handle_pickup_cargo(event, officer.player, officer, ctx)

        # Confiscation record created with character=None
        self.assertEqual(await Confiscation.objects.acount(), 1)
        conf = await Confiscation.objects.afirst()
        self.assertIsNone(conf.character_id)
        self.assertEqual(conf.officer_id, officer.id)
        self.assertEqual(conf.amount, 5000)

        # No transfers at all: unknown owner isn't charged, no officer reward
        mock_transfer.assert_not_called()

        # PD fund credited
        mock_treasury.assert_called_once_with(5000, "Police Confiscation")

        # No officer wallet ledger entry
        mock_fund_wallet.assert_not_called()

        # Cargo despawned
        mock_despawn.assert_called_once_with(mock_http_mod, str(officer.guid))


class PdFundJournalTests(TestCase):
    """Ledger-level tests for record_pd_fund_confiscation_income."""

    async def test_pd_fund_journal_balanced(self):
        from amc_finance.models import Account, LedgerEntry
        from amc_finance.services import record_pd_fund_confiscation_income

        await record_pd_fund_confiscation_income(1234, "Police Confiscation")

        pd_fund = await Account.objects.aget(
            name="Police Department Fund", book=Account.Book.GOVERNMENT
        )
        self.assertEqual(pd_fund.account_type, Account.AccountType.ASSET)
        self.assertEqual(pd_fund.balance, 1234)

        revenue = await Account.objects.aget(
            name="Confiscation Revenue", book=Account.Book.GOVERNMENT
        )
        self.assertEqual(revenue.account_type, Account.AccountType.REVENUE)

        # Journal is balanced: total debits == total credits
        legs = [le async for le in LedgerEntry.objects.all()]
        self.assertEqual(sum(le.debit for le in legs), 1234)
        self.assertEqual(sum(le.credit for le in legs), 1234)

        # Treasury Fund is untouched
        self.assertFalse(
            await Account.objects.filter(name="Treasury Fund").aexists()
        )
