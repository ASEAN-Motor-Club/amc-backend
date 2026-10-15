"""Re-hash RaceSetup rows and merge content-identical duplicates.

Operational wrapper around the 0234 data-migration logic
(``amc.migrations.0234_racesetup_rehash_quantize.rehash_and_merge``), kept
runnable AFTER migrations have shipped.  RaceSetup rows can still acquire a
stale stored hash later (hand-created or admin-edited rows whose config was
changed without rehashing); a stale hash makes ``process_event``'s
hash-based scheduled-event match land on a fresh duplicate row, so the
GameEvent's ``scheduled_event`` stays NULL and the start-line DQ silently
falls back from the SE's ``allowed_vehicle_types`` override to the TTClass
defaults (prod: SE 78 "Bus Terminal Sprint Test", 2026-10-15 — the bus-only
override never armed and Bus drivers were DQ'd against {Small, Pickup}).

Default is a dry-run report; pass ``--apply`` to actually rehash + merge
(re-point GameEvent/ScheduledEvent FKs to the lowest-id survivor, delete the
duplicates).  Idempotent — safe to run repeatedly.
"""

from importlib import import_module

from django.apps import apps as global_apps
from django.core.management.base import BaseCommand

from amc.models import RaceSetup

_migration = import_module("amc.migrations.0234_racesetup_rehash_quantize")


def stale_rows():
    """RaceSetup rows whose stored hash no longer matches their config."""
    out = []
    for rs in RaceSetup.objects.filter(config__isnull=False).only(
        "id", "hash", "config"
    ):
        correct = RaceSetup.calculate_hash(rs.config)
        if rs.hash != correct:
            out.append((rs, correct))
    return out


class Command(BaseCommand):
    help = (
        "Report (default) or fix (--apply) RaceSetup rows with a stale stored "
        "hash, merging content-identical duplicates and re-pointing FKs."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply the rehash + merge (default: report only).",
        )

    def handle(self, *args, **options):
        stale = stale_rows()
        if not stale:
            self.stdout.write(self.style.SUCCESS("all RaceSetup hashes clean"))
            return
        for rs, correct in stale:
            self.stdout.write(
                f"stale: RaceSetup {rs.id} ({rs.name}) stored {rs.hash[:12]} "
                f"-> recomputed {correct[:12]}"
            )

        if not options["apply"]:
            self.stdout.write(
                self.style.WARNING(
                    f"{len(stale)} stale row(s); dry run only — pass --apply to fix"
                )
            )
            return

        _migration.rehash_and_merge(global_apps, None)

        remaining = stale_rows()
        if remaining:
            self.stdout.write(
                self.style.ERROR(f"{len(remaining)} row(s) STILL stale after apply")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f"fixed {len(stale)} stale row(s); hashes clean")
            )
