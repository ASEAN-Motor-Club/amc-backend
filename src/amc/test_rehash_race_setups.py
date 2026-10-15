"""Tests for the rehash_race_setups management command.

The command wraps the 0234 migration's rehash_and_merge for post-migration
use: RaceSetup rows can still acquire a stale stored hash after the migration
has run (hand-created/admin-edited config without rehashing), and a stale
hash breaks `process_event`'s hash-based ScheduledEvent match — the prod SE
78 failure (2026-10-15) left GameEvents unlinked and the start-line DQ
enforcing the TTClass defaults instead of the SE override.
"""

from datetime import timedelta
from importlib import import_module
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from amc.management.commands.rehash_race_setups import stale_rows
from amc.models import GameEvent, RaceSetup, ScheduledEvent
from amc.test_racesetup_hash import FLOAT_CONFIG, INT_CONFIG

_migration = import_module("amc.migrations.0234_racesetup_rehash_quantize")


class RehashRaceSetupsCommandTestCase(TestCase):
    def _make_setup(self, config, name, hash_override=None):
        return RaceSetup.objects.create(
            config=config,
            # Default to the LIVE scheme — a raw DeepHash is already stale by
            # the model's definition (calculate_hash normalizes).
            hash=hash_override or RaceSetup.calculate_hash(config),
            name=name,
        )

    def _seed_stale_pair(self):
        """A correctly-hashed survivor + a stale twin + dependents on the
        stale row (the prod SE 78 shape)."""
        survivor = self._make_setup(FLOAT_CONFIG, "GjJJTerminalSprint")
        stale = self._make_setup(
            INT_CONFIG, "GjJJTerminalSprint", hash_override="legacy-hash-value"
        )
        assert survivor.hash != stale.hash
        scheduled = ScheduledEvent.objects.create(
            name="Bus Terminal Sprint Test",
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(days=7),
            race_setup=stale,
        )
        game_event = GameEvent.objects.create(
            name="session",
            guid="ABCDEF0123456789ABCDEF0123456789",
            state=0,
            auto_created=False,
            race_setup=stale,
        )
        return survivor, stale, scheduled, game_event

    def test_stale_rows_detects_mismatched_hash(self):
        survivor, stale, _, _ = self._seed_stale_pair()
        found = {rs.id for rs, _ in stale_rows()}
        assert stale.id in found
        assert survivor.id not in found

    def test_dry_run_reports_without_touching_data(self):
        _, stale, scheduled, game_event = self._seed_stale_pair()
        call_command("rehash_race_setups")
        stale.refresh_from_db()
        scheduled.refresh_from_db()
        game_event.refresh_from_db()
        assert stale.hash == "legacy-hash-value"
        assert scheduled.race_setup_id == stale.id
        assert game_event.race_setup_id == stale.id

    def test_apply_rehashes_merges_and_repoints_fks(self):
        survivor, stale, scheduled, game_event = self._seed_stale_pair()
        call_command("rehash_race_setups", apply=True)
        assert not RaceSetup.objects.filter(id=stale.id).exists()
        survivor.refresh_from_db()
        assert survivor.hash == RaceSetup.calculate_hash(FLOAT_CONFIG)
        scheduled.refresh_from_db()
        assert scheduled.race_setup_id == survivor.id
        game_event.refresh_from_db()
        assert game_event.race_setup_id == survivor.id
        # Scope to this test's rows: other modules' fixtures may legitimately
        # carry raw (stale) hashes that leak into the shared suite DB.
        assert stale.id not in {rs.id for rs, _ in stale_rows()}

    def test_dry_run_does_not_report_a_clean_row(self):
        survivor = self._make_setup(FLOAT_CONFIG, "Electric Speed Trap")
        out = StringIO()
        call_command("rehash_race_setups", stdout=out)
        assert str(survivor.id) not in out.getvalue()

    def test_apply_is_idempotent(self):
        self._seed_stale_pair()
        call_command("rehash_race_setups", apply=True)
        count = RaceSetup.objects.count()
        call_command("rehash_race_setups", apply=True)
        assert RaceSetup.objects.count() == count
        # Global emptiness can't be asserted (other fixtures leak raw-hash
        # rows); idempotency means nothing NEW became stale and the count held.
        assert all(
            rs.hash == RaceSetup.calculate_hash(rs.config)
            for rs in RaceSetup.objects.filter(config__isnull=False)
        )
