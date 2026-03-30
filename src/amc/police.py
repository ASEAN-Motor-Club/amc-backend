"""Police session management.

Similar to gov_employee.py — provides session-based police duty
instead of permanent faction membership.
"""

from django.db.models import F
from django.utils import timezone
from amc.player_tags import refresh_player_name

POLICE_LEVEL_STEP = 10_000

# Risk premium: extra payout on Money deliveries per active online police officer.
SECURITY_BONUS_RATE = 0.20  # 20% per officer
SECURITY_BONUS_MAX = 1.0  # capped at 100%


async def get_active_police_count() -> int:
    """Count police officers who are on duty AND currently online.

    An officer counts if they have an active PoliceSession (ended_at is null)
    AND their character.last_online is within the last 60 seconds.
    """
    from datetime import timedelta
    from amc.models import PoliceSession

    online_threshold = timezone.now() - timedelta(seconds=60)
    return await PoliceSession.objects.filter(
        ended_at__isnull=True,
        character__last_online__gte=online_threshold,
    ).acount()


def calculate_police_level(confiscated_total: int) -> int:
    """Calculate police level from cumulative confiscated amount.
    Level scales infinitely: floor(total / step) + 1"""
    return (confiscated_total // POLICE_LEVEL_STEP) + 1


async def is_police(character) -> bool:
    """Check if a character has an active police session."""
    from amc.models import PoliceSession

    return await PoliceSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aexists()


async def activate_police(character, session):
    """Start a new police session and refresh player tag."""
    from amc.models import PoliceSession

    # End any existing active session first
    await PoliceSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aupdate(ended_at=timezone.now())

    await PoliceSession.objects.acreate(character=character)
    await refresh_player_name(character, session)


async def deactivate_police(character, session):
    """End the active police session and refresh player tag."""
    from amc.models import PoliceSession

    await PoliceSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aupdate(ended_at=timezone.now())

    await refresh_player_name(character, session)


async def record_confiscation_for_level(character, amount, http_client=None, session=None):
    """Increment confiscated total and check for level-up.

    Args:
        character: The officer's Character model.
        amount: Amount confiscated.
        http_client: HTTP client for announcements.
        session: HTTP client for mod server (name refresh).
    """
    old_level = calculate_police_level(character.police_confiscated_total)

    character.police_confiscated_total = F("police_confiscated_total") + int(amount)
    await character.asave(update_fields=["police_confiscated_total"])
    await character.arefresh_from_db(fields=["police_confiscated_total"])

    new_level = calculate_police_level(character.police_confiscated_total)
    if new_level != old_level:
        await refresh_player_name(character, session)

        if http_client:
            import asyncio
            from amc.game_server import announce

            asyncio.create_task(
                announce(
                    f"🎉 {character.name} has been promoted to Police Level {new_level}!",
                    http_client,
                    color="4A90D9",
                )
            )
