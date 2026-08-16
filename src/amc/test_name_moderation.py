"""Tests for login-time offensive-name auto-moderation.

Covers: NameModerationLog audit model (Task 2), Stage A deterministic
blocklist (Task 3), Pydantic structured verdicts (Task 4), the OpenRouter LLM
judge (Step 5), the orchestrator + login hook + auto-rename + announcement
(Step 6), and the safe-suggestion guard.

These require PostgreSQL (ArrayField) so they run under the flake's pytest
check / CI, not in a plain venv without Postgres.
"""

import pytest

from amc.factories import CharacterFactory, PlayerFactory
from amc.models import NameModerationLog

pytestmark = pytest.mark.django_db


@pytest.mark.asyncio
async def test_name_moderation_log_row_persists():
    """A decision row records a player + verdict + action (Task 2)."""
    player = await PlayerFactory.acreate()
    character = await CharacterFactory.acreate(player=player)
    await NameModerationLog.objects.acreate(
        player=player,
        character=character,
        base_name="delivyn1gaa",
        verdict_source=NameModerationLog.VerdictSource.BLOCKLIST,
        is_violation=True,
        confidence=1.0,
        categories=["racial_slur"],
        action=NameModerationLog.Action.RENAME,
        suggested_name="FriendlyDriver",
        llm_model="",
    )
    row = await NameModerationLog.objects.aget(player=player)
    assert row.is_violation is True
    assert row.action == NameModerationLog.Action.RENAME
    assert row.suggested_name == "FriendlyDriver"