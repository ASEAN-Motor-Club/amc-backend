import asyncio
from datetime import timedelta
from decimal import Decimal
from django.utils import timezone
from django.db.models import Sum
from asgiref.sync import sync_to_async
from amc.models import Player, Delivery
from amc_finance.models import Account, LedgerEntry
from amc_finance.services import create_journal_entry

from enum import Enum
from typing import Optional, Tuple


class LoanLimitReason(Enum):
    """Enumeration for reasons a character's loan may be limited."""

    INELIGIBLE = "You are currently ineligible for a loan due to your social score."
    UNVERIFIED = "You must first verify yourself through discord"
    BANK_POLICY = "Your loan has reached the maximum amount set by bank policy."
    EARNINGS_HISTORY = "Your loan amount is limited by your recent earnings history."
    SOCIAL_SCORE = "Your loan amount has been reduced due to a low social score."

    def __str__(self):
        """Returns the human-readable message for the reason."""
        return self.value


async def get_character_max_loan(character) -> Tuple[int, Optional[LoanLimitReason]]:
    """
    Calculates the maximum loan amount for a character and identifies the reason if it's limited.

    Returns:
      A tuple containing:
      - int: The maximum loan amount.
      - Optional[LoanLimitReason]: The reason for the loan limit, or None if not limited.
    """
    SOCIAL_SCORE_LOAN_MODIFIER = 0.05
    BANK_POLICY_CAP = 6_000_000

    player = await Player.objects.aget(characters=character)
    deliveries_agg = await Delivery.objects.filter(character__player=player).aaggregate(
        total_payment=Sum("payment", default=0) + Sum("subsidy", default=0),
    )

    if player.discord_user_id is None:
        return 0, LoanLimitReason.UNVERIFIED

    # --- Loan Calculation ---

    # 1. Start with the base loan from character levels
    base_loan = 10_000
    if character.driver_level:
        base_loan += character.driver_level * 3_000
    if character.truck_level:
        base_loan += character.truck_level * 3_000

    max_loan = float(base_loan)
    reason: Optional[LoanLimitReason] = None

    # 2. Apply social score modifier
    social_score_reduced_loan = False
    if player and hasattr(player, "social_score"):
        loan_modifier = player.social_score * SOCIAL_SCORE_LOAN_MODIFIER
        max_loan *= 1 + loan_modifier
        if player.social_score < 0:
            social_score_reduced_loan = True

    # If social score makes them ineligible, it's the final reason
    if max_loan <= 0:
        return 0, LoanLimitReason.INELIGIBLE

    # 3. Apply the bank's hard policy cap
    if max_loan > BANK_POLICY_CAP:
        max_loan = BANK_POLICY_CAP
        reason = LoanLimitReason.BANK_POLICY

    # 4. Apply the cap based on earnings history
    # This check comes after the bank policy cap, so it will correctly
    # become the reason if it's an even more restrictive limit.
    earnings_cap = deliveries_agg["total_payment"] * 5
    if max_loan > earnings_cap:
        max_loan = earnings_cap
        reason = LoanLimitReason.EARNINGS_HISTORY

    # 5. If no cap was hit, check if a negative social score was the limiting factor
    if reason is None and social_score_reduced_loan:
        reason = LoanLimitReason.SOCIAL_SCORE

    return int(max_loan), reason


async def get_player_bank_balance(character):
    account, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.LIABILITY,
        book=Account.Book.BANK,
        character=character,
        defaults={
            "name": "Checking Account",
        },
    )
    return account.balance


async def get_player_loan_balance(character):
    loan_account, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.BANK,
        character=character,
        defaults={
            "name": f"Loan #{character.id} - {character.name}",
        },
    )
    return loan_account.balance


LOAN_INTEREST_RATES = [0.1, 0.2, 0.3]


def calc_loan_fee(amount, character, max_loan, credit_score=100):
    threshold = Decimal(0)
    fee = Decimal(0)
    for i, interest_rate in enumerate(LOAN_INTEREST_RATES, start=1):
        prev_threshold = threshold
        threshold += max_loan / Decimal(2**i)
        amount_under_threshold = min(
            max(Decimal(0), Decimal(amount) - prev_threshold),
            threshold - prev_threshold,
        )
        if amount_under_threshold > 0:
            fee += Decimal(amount_under_threshold) * Decimal(interest_rate)

    if amount > threshold:
        fee += (Decimal(amount) - threshold) * Decimal(interest_rate)

    # Credit score multiplier (piecewise linear):
    #   Score 0→100: multiplier 2.0→1.0
    #   Score 100→200: multiplier 1.0→0.5
    clamped_score = max(0, min(200, credit_score))
    if clamped_score <= 100:
        fee_multiplier = 2.0 - clamped_score / 100
    else:
        fee_multiplier = 1.0 - 0.5 * (clamped_score - 100) / 100
    fee = int(fee * Decimal(str(fee_multiplier)))
    return fee


async def register_player_take_loan(amount, character):
    if amount <= 0:
        raise ValueError("Loan amount must be positive")
    max_loan, _ = await get_character_max_loan(character)
    fee = calc_loan_fee(
        amount, character, max_loan, credit_score=character.credit_score
    )
    principal = Decimal(amount) + Decimal(fee)

    loan_account, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.BANK,
        character=character,
        defaults={
            "name": f"Loan #{character.id} - {character.name}",
        },
    )

    bank_vault, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.BANK,
        character=None,
        defaults={
            "name": "Bank Vault",
        },
    )

    bank_revenue, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.REVENUE,
        book=Account.Book.BANK,
        character=None,
        defaults={
            "name": "Bank Revenue",
        },
    )

    await sync_to_async(create_journal_entry)(
        timezone.now(),
        "Player Loan",
        character,
        [
            {
                "account": loan_account,
                "debit": principal,
                "credit": 0,
            },
            {
                "account": bank_revenue,
                "debit": 0,
                "credit": principal - amount,
            },
            {
                "account": bank_vault,
                "debit": 0,
                "credit": amount,
            },
        ],
    )
    return principal, fee


NPL_DEFAULT_REPAYMENT_RATE = Decimal("0.10")  # 10% of balance per period
NPL_DEFAULT_PERIOD_DAYS = 7  # weekly
NPL_MIN_BALANCE = 500_000  # minimum loan balance to be considered NPL


def get_non_performing_loans():
    """
    Returns a list of loan Account objects where cumulative repayments
    in the configured period are below the required minimum.

    Each account can override the global defaults via:
    - min_repayment_rate (fraction, e.g. 0.10 = 10%)
    - min_repayment_period_days (e.g. 7 = weekly)

    Annotated with:
    - total_repaid_in_period: actual credits in the period
    - min_required_repayment: balance × rate
    - last_repayment_at: most recent credit entry
    """
    from django.db.models import Max, Subquery, OuterRef, Sum

    now = timezone.now()

    # For the "last_repayment_at" annotation (used for sorting / display)
    last_repayment_subquery = (
        LedgerEntry.objects.filter(
            account=OuterRef("pk"),
            credit__gt=0,
        )
        .values("account")
        .annotate(latest=Max("journal_entry__created_at"))
        .values("latest")
    )

    # Base queryset: all active loan accounts
    qs = (
        Account.objects.filter(
            account_type=Account.AccountType.ASSET,
            book=Account.Book.BANK,
            character__isnull=False,
            balance__gte=NPL_MIN_BALANCE,
        )
        .annotate(
            last_repayment_at=Subquery(last_repayment_subquery),
        )
        .select_related("character", "character__player")
    )

    # We can't use per-row dynamic cutoffs in a single SQL subquery easily,
    # so we fetch and filter in Python. This is fine since loan accounts are
    # a small set (typically < 100).
    results = []
    for account in qs:
        period_days = account.min_repayment_period_days or NPL_DEFAULT_PERIOD_DAYS
        rate = (
            account.min_repayment_rate
            if account.min_repayment_rate is not None
            else NPL_DEFAULT_REPAYMENT_RATE
        )
        cutoff = now - timedelta(days=period_days)

        # Sum of credits in this account's period
        total_repaid = (
            LedgerEntry.objects.filter(
                account=account,
                credit__gt=0,
                journal_entry__created_at__gte=cutoff,
            ).aggregate(total=Sum("credit"))["total"]
        ) or Decimal(0)

        min_required = account.balance * rate

        # Attach computed values for display
        account.total_repaid_in_period = total_repaid
        account.min_required_repayment = min_required
        account.repayment_period_days = period_days

        if total_repaid < min_required:
            results.append(account)

    return results


async def get_character_npl_status(character):
    """
    Check NPL status for a single character's loan account.

    Returns None if no loan or loan below NPL_MIN_BALANCE.
    Otherwise returns a dict with:
      - is_npl: bool
      - loan_balance: Decimal
      - period_days: int
      - repayment_rate: Decimal
      - total_repaid_in_period: Decimal
      - min_required_repayment: Decimal
    """
    try:
        account = await Account.objects.aget(
            account_type=Account.AccountType.ASSET,
            book=Account.Book.BANK,
            character=character,
        )
    except Account.DoesNotExist:
        return None

    if account.balance < NPL_MIN_BALANCE:
        return None

    period_days = account.min_repayment_period_days or NPL_DEFAULT_PERIOD_DAYS
    rate = (
        account.min_repayment_rate
        if account.min_repayment_rate is not None
        else NPL_DEFAULT_REPAYMENT_RATE
    )
    cutoff = timezone.now() - timedelta(days=period_days)

    total_repaid = (
        await LedgerEntry.objects.filter(
            account=account,
            credit__gt=0,
            journal_entry__created_at__gte=cutoff,
        ).aaggregate(total=Sum("credit"))
    )["total"] or Decimal(0)

    min_required = account.balance * rate

    return {
        "is_npl": total_repaid < min_required,
        "loan_balance": account.balance,
        "period_days": period_days,
        "repayment_rate": rate,
        "total_repaid_in_period": total_repaid,
        "min_required_repayment": min_required,
    }


async def is_character_npl(character) -> bool:
    """Quick check: is the character currently in NPL?"""
    status = await get_character_npl_status(character)
    if status is None:
        return False
    return bool(status["is_npl"])


# --- Credit Score ---

CREDIT_SCORE_MET = 10  # +10 per period when obligations met
CREDIT_SCORE_EXCEEDED = 15  # +15 per period when repaid >= 200% of required
CREDIT_SCORE_MISSED = -30  # -30 per period when in NPL
CREDIT_SCORE_MIN = 0
CREDIT_SCORE_MAX = 200
CREDIT_SCORE_MIN_BALANCE = 100_000  # minimum loan balance for credit score evaluation
CREDIT_UTILIZATION_HIGH = Decimal("0.70")  # >70% utilization: -5 per period
CREDIT_UTILIZATION_VERY_HIGH = Decimal("0.90")  # >90% utilization: -10 per period
CREDIT_UTILIZATION_HIGH_PENALTY = -5
CREDIT_UTILIZATION_VERY_HIGH_PENALTY = -10


def get_credit_score_label(score):
    """Human-readable label for a credit score."""
    if score >= 171:
        return "Excellent"
    if score >= 131:
        return "Very Good"
    if score >= 101:
        return "Good"
    if score == 100:
        return "Neutral"
    if score >= 71:
        return "Fair"
    if score >= 41:
        return "Poor"
    return "Very Poor"


async def evaluate_credit_scores(ctx=None):
    """Daily cron: evaluate credit scores for all qualifying loan accounts.

    Each account is scored at most once per NPL period (default 7 days).
    Uses the same repayment window as NPL detection.
    """
    import logging

    logger = logging.getLogger(__name__)

    now = timezone.now()

    accounts = await sync_to_async(
        lambda: list(
            Account.objects.filter(
                account_type=Account.AccountType.ASSET,
                book=Account.Book.BANK,
                character__isnull=False,
                balance__gte=CREDIT_SCORE_MIN_BALANCE,
            ).select_related("character")
        )
    )()

    updated_characters = []
    updated_accounts = []

    for account in accounts:
        period_days = account.min_repayment_period_days or NPL_DEFAULT_PERIOD_DAYS

        # Only evaluate once per period
        if (
            account.last_credit_score_evaluated_at is not None
            and (now - account.last_credit_score_evaluated_at).days < period_days
        ):
            continue

        rate = (
            account.min_repayment_rate
            if account.min_repayment_rate is not None
            else NPL_DEFAULT_REPAYMENT_RATE
        )
        cutoff = now - timedelta(days=period_days)

        total_repaid = await sync_to_async(
            lambda: (
                LedgerEntry.objects.filter(
                    account=account,
                    credit__gt=0,
                    journal_entry__created_at__gte=cutoff,
                ).aggregate(total=Sum("credit"))["total"]
            )
            or Decimal(0)
        )()

        min_required = account.balance * rate
        character = account.character

        if total_repaid < min_required:
            # Missed obligations (NPL)
            delta = CREDIT_SCORE_MISSED
        elif total_repaid >= min_required * 2:
            # Exceeded obligations
            delta = CREDIT_SCORE_EXCEEDED
        else:
            # Met obligations
            delta = CREDIT_SCORE_MET

        # Credit utilization penalty
        try:
            max_loan, _ = await get_character_max_loan(character)
            if max_loan > 0:
                utilization = account.balance / Decimal(max_loan)
                if utilization > CREDIT_UTILIZATION_VERY_HIGH:
                    delta += CREDIT_UTILIZATION_VERY_HIGH_PENALTY
                elif utilization > CREDIT_UTILIZATION_HIGH:
                    delta += CREDIT_UTILIZATION_HIGH_PENALTY
        except Exception:
            pass  # Skip utilization check if max_loan lookup fails

        old_score = character.credit_score
        character.credit_score = max(
            CREDIT_SCORE_MIN,
            min(CREDIT_SCORE_MAX, character.credit_score + delta),
        )

        if character.credit_score != old_score:
            updated_characters.append(character)
            logger.info(
                f"Credit score: {character.name} {old_score} -> {character.credit_score} (delta={delta:+d})"
            )

        account.last_credit_score_evaluated_at = now
        updated_accounts.append(account)

    # Bulk save
    if updated_characters:
        from amc.models import Character

        await sync_to_async(
            lambda: Character.objects.bulk_update(updated_characters, ["credit_score"])
        )()
    if updated_accounts:
        await sync_to_async(
            lambda: Account.objects.bulk_update(
                updated_accounts, ["last_credit_score_evaluated_at"]
            )
        )()

    logger.info(
        f"Credit score evaluation: {len(updated_characters)} scores updated, "
        f"{len(updated_accounts)} accounts evaluated"
    )


async def register_player_repay_loan(amount, character):
    loan_account, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.BANK,
        character=character,
        defaults={
            "name": f"Loan #{character.id} - {character.name}",
        },
    )

    bank_vault, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.ASSET,
        book=Account.Book.BANK,
        character=None,
        defaults={
            "name": "Bank Vault",
        },
    )
    if loan_account.balance < amount:
        raise ValueError("You are repaying more than you owe")

    result = await sync_to_async(create_journal_entry)(
        timezone.now(),
        "Player Loan Repayment",
        character,
        [
            {
                "account": bank_vault,
                "debit": amount,
                "credit": 0,
            },
            {
                "account": loan_account,
                "debit": 0,
                "credit": amount,
            },
        ],
    )

    return result


# --- Loan Repayment (on profit) ---

# The loan utilisation at which repayment rate reaches 100%.
# e.g. 0.5 = 100% repayment when debt is >=50% of loan limit.
# Set to 1.0 to restore the old linear curve (100% only at full utilisation).
REPAYMENT_FULL_AT = Decimal("0.5")


def calculate_loan_repayment(
    payment, loan_balance, max_loan, character_repayment_rate=None
):
    loan_utilisation = loan_balance / max(max_loan, loan_balance)
    slope = Decimal("0.5") / REPAYMENT_FULL_AT
    repayment_percentage = min(Decimal(1), Decimal("0.5") + slope * loan_utilisation)
    if character_repayment_rate is not None:
        repayment_percentage = max(
            repayment_percentage, Decimal(str(character_repayment_rate))
        )

    repayment = min(
        loan_balance,
        max(Decimal(1), Decimal(int(payment * Decimal(repayment_percentage)))),
    )
    return repayment


async def repay_loan_for_profit(
    character, payment, session, repayment_override=None, game_session=None
):
    from amc.mod_server import show_popup, transfer_money
    from amc.game_server import announce

    try:
        loan_balance = await get_player_loan_balance(character)
        if loan_balance == 0:
            return 0

        was_npl = await is_character_npl(character)

        if repayment_override is not None:
            repayment = min(Decimal(str(repayment_override)), loan_balance)
        else:
            max_loan, _ = await get_character_max_loan(character)
            repayment = calculate_loan_repayment(
                Decimal(payment),
                loan_balance,
                max_loan,
                character_repayment_rate=character.loan_repayment_rate,
            )

        await transfer_money(
            session,
            int(-repayment),
            "ASEAN Loan Repayment",
            str(character.player.unique_id),
        )
        await register_player_repay_loan(repayment, character)

        # Announce NPL exit in game
        if was_npl and not await is_character_npl(character):
            announce_session = game_session or session
            asyncio.create_task(
                announce(
                    f"{character.name} is no longer under a Non-Performing Loan repayment plan. Congratulations!",
                    announce_session,
                    color="00FF00",
                )
            )

        return int(repayment)
    except Exception as e:
        asyncio.create_task(
            show_popup(session, f"Repayment failed {e}", character_guid=character.guid)
        )
        raise e
