"""Seed the underground TT class ladder (Yuuka 2026-09-29).

Keeps the four original classes (140/270/350/480 — history FKs reference
them) and adds the six missing rungs so the ladder reads 110, 160, 220,
270, 340, 410, 480, 540, 590, 620. Idempotent.
"""

from django.core.management.base import BaseCommand

from amc.models import TTClass

LADDER: list[tuple[str, int]] = [
    ("TT-110", 110),
    ("TT-160", 160),
    ("TT-220", 220),
    ("TT-270", 270),
    ("TT-340", 340),
    ("TT-410", 410),
    ("TT-480", 480),
    ("TT-540", 540),
    ("TT-590", 590),
    ("TT-620", 620),
]


class Command(BaseCommand):
    help = "Create the underground TT class ladder (idempotent, keeps history)."

    def handle(self, *args, **options):
        for name, max_hp in LADDER:
            obj, created = TTClass.objects.get_or_create(
                name=name, defaults={"max_hp": max_hp}
            )
            if created:
                self.stdout.write(f"created {obj}")
            elif obj.max_hp != max_hp:
                self.stdout.write(
                    self.style.WARNING(
                        f"existing {name} has max_hp={obj.max_hp} (ladder says {max_hp}) — left untouched"
                    )
                )
            else:
                self.stdout.write(f"exists  {obj}")
        self.stdout.write(self.style.SUCCESS(f"TTClass count: {TTClass.objects.count()}"))
