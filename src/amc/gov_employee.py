from datetime import timedelta
from django.db.models import F, Sum
from django.utils import timezone
from amc_finance.services import player_donation
from amc.player_tags import refresh_player_name

GOV_LEVEL_STEP = 500_000
GOV_ROLE_DURATION = timedelta(hours=24)
GOV_BOARD_LIMIT = 5


def calculate_gov_level(contributions: int) -> int:
    """Calculate government employee level from cumulative contributions.
    Level scales infinitely: floor(contributions / step) + 1"""
    return (contributions // GOV_LEVEL_STEP) + 1


def strip_gov_name(name: str) -> str:
    """Remove [GOVn] prefix from a name."""
    from amc.player_tags import strip_all_tags

    return strip_all_tags(name)


def make_gov_name(name: str, level: int) -> str:
    """Build a display name with a [GOVn] prefix."""
    clean = strip_gov_name(name)
    return f"[GOV{level}] {clean}"


async def activate_gov_role(character, session):
    """Activate the government employee role for 24 hours."""
    level = calculate_gov_level(character.gov_employee_contributions)
    character.gov_employee_until = timezone.now() + GOV_ROLE_DURATION
    character.gov_employee_level = level
    await character.asave(update_fields=["gov_employee_until", "gov_employee_level"])

    await refresh_player_name(character, session)


async def deactivate_gov_role(character, session):
    """Deactivate the government employee role and restore name."""
    character.gov_employee_until = None
    character.gov_employee_level = 0
    await character.asave(update_fields=["gov_employee_until", "gov_employee_level"])

    await refresh_player_name(character, session)


async def redirect_income_to_treasury(
    amount, character, description, http_client=None, session=None, contribution=None
):
    """Record a government employee's income as a treasury contribution.

    Args:
        amount: Real money confiscated from wallet → treasury ledger.
        contribution: Total economic value for gov level progression.
            Defaults to amount if not specified. May include subsidy
            credit that was never in the wallet.
    """
    if contribution is None:
        contribution = amount

    # Ledger: only record real money that was confiscated
    await player_donation(int(amount), character, description=description)

    # Contributions: track full economic value (including subsidy) for levels
    character.gov_employee_contributions = F("gov_employee_contributions") + int(
        contribution
    )
    await character.asave(update_fields=["gov_employee_contributions"])

    # Per-event history for the monthly leaderboard (all-time total lives on
    # the Character field above). Zero-contribution calls (subsidy-only job
    # bonuses) are skipped — they carry no leaderboard value.
    if contribution > 0:
        from amc.models import GovContributionLog

        await GovContributionLog.objects.acreate(
            character=character, contribution=int(contribution)
        )

    # Refresh to get actual DB value, then recalculate level
    await character.arefresh_from_db(fields=["gov_employee_contributions"])
    new_level = calculate_gov_level(character.gov_employee_contributions)
    if new_level != character.gov_employee_level:
        character.gov_employee_level = new_level
        await character.asave(update_fields=["gov_employee_level"])

        # Level up logic
        await refresh_player_name(character, session)

        if http_client:
            from amc.game_server import announce
            import asyncio

            asyncio.create_task(
                announce(
                    f"🎉 {character.name} has been promoted to Government Employee Level {new_level}!",
                    http_client,
                    color="90EE90",
                )
            )


async def expire_gov_employees(ctx):
    """Cron task: deactivate expired gov employee roles for online players."""
    from amc.models import Character

    expired = Character.objects.filter(
        gov_employee_until__isnull=False,
        gov_employee_until__lt=timezone.now(),
    ).select_related("player")
    http_client_mod = ctx.get("http_client_mod")
    async for character in expired:
        try:
            await deactivate_gov_role(character, http_client_mod)
        except Exception as e:
            import logging

            logging.getLogger(__name__).exception(
                f"Error expiring gov role for {character}: {e}"
            )


async def top_gov_all_time(limit: int = GOV_BOARD_LIMIT):
    """Top characters by cumulative gov contributions (all-time board)."""
    from amc.models import Character

    qs = (
        Character.objects.filter(gov_employee_contributions__gt=0)
        .select_related("player")
        .order_by("-gov_employee_contributions", "name")[:limit]
    )
    return [row async for row in qs]


def _month_start():
    """Start of the current calendar month in the club timezone (Asia/Bangkok)."""
    return timezone.localtime().replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )


async def top_gov_monthly(limit: int = GOV_BOARD_LIMIT):
    """Top characters by gov contributions logged in the current month.

    Returns a list of (Character, monthly_total) tuples ordered by total.
    Requires GovContributionLog history — rows only exist from when the log
    shipped, so early months are partial.
    """
    from amc.models import Character, GovContributionLog

    month_rows = [
        row
        async for row in GovContributionLog.objects.filter(
            timestamp__gte=_month_start()
        )
        .values("character")
        .annotate(total=Sum("contribution"))
        .order_by("-total", "character")[:limit]
    ]
    if not month_rows:
        return []

    ids = [row["character"] for row in month_rows]
    characters = {
        row.id: row
        async for row in Character.objects.filter(id__in=ids).select_related("player")
    }
    return [
        (characters[row["character"]], row["total"])
        for row in month_rows
        if row["character"] in characters
    ]

