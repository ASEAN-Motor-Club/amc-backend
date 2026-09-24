"""Pending wallet payouts — deliver bank-held funds to a player's in-game wallet at login.

Used by moderation/economy clawbacks: when a correction must move money out of
a player's bank account into their in-game wallet but the player is offline,
the payout is queued here and delivered by the login hook (amc.tasks), mirroring
the persistent-mute reapply pattern.

Two-phase safety: the ledger withdrawal (register_player_withdrawal) is booked
first (balance-guarded — raises on insufficient funds), then the mod wallet
transfer runs. `booked_at` marks the ledger leg so a wallet-transfer failure
can be retried WITHOUT booking the withdrawal twice.
"""

import logging

from django.utils import timezone

from amc.mod_server import show_popup, transfer_money
from amc.models import PendingWalletPayout
from amc_finance.services import register_player_withdrawal

logger = logging.getLogger(__name__)


async def deliver_pending_wallet_payouts(
    player, character, http_client_mod
) -> None:
    """Deliver every unpaid pending payout for this player at login."""
    async for payout in PendingWalletPayout.objects.filter(
        player=player, paid_at__isnull=True
    ).order_by("created_at"):
        try:
            # Phase 1 — book the ledger leg once (balance-guarded).
            if payout.booked_at is None:
                await register_player_withdrawal(
                    payout.amount,
                    character,
                    player,
                    description=f"Wallet payout: {payout.reason}",
                )
                payout.booked_at = timezone.now()
                await payout.asave(update_fields=["booked_at"])

            # Phase 2 — move the cash into the in-game wallet.
            await transfer_money(
                http_client_mod,
                payout.amount,
                f"Wallet payout: {payout.reason}",
                player.unique_id,
            )
            payout.paid_at = timezone.now()
            await payout.asave(update_fields=["paid_at"])
            logger.info(
                "Delivered pending wallet payout %s (%s) to %s: %s",
                payout.id,
                payout.amount,
                player.unique_id,
                payout.reason,
            )
            await show_popup(
                http_client_mod,
                f"Wallet payout: {payout.amount:,} has been added to your wallet.",
                player_id=player.unique_id,
            )
        except ValueError as e:
            # Insufficient bank balance — leave unpaid for a later login.
            logger.warning(
                "Pending wallet payout %s not deliverable yet: %s", payout.id, e
            )
        except Exception:
            # Transfer failure after booking — row stays booked, retry delivers
            # only the wallet leg (no double withdrawal).
            logger.exception(
                "Failed to deliver pending wallet payout %s", payout.id
            )
