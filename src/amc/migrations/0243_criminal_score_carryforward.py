"""Carry existing criminal achievements forward into criminal_score.

freeman's requirement: players keep their exact criminal level on day one.
Level is derived today as floor(criminal_laundered_total / 50_000) + 1 and the
rework derives it from criminal_score with the same step, so seeding
criminal_score = criminal_laundered_total preserves every level.

The decay clock is anchored at the character's last illicit delivery so the
48h grace window starts from real activity, not from the migration date.
"""

from django.db import migrations

# ILLICIT_CARGO_KEYS snapshot (amc.special_cargo) — inlined so this migration
# stays self-contained against future refactors of the live module.
ILLICIT_CARGO_KEYS = [
    "Money",
    "Ganja",
    "CocaLeavesPallet",
    "GanjaPallet",
    "Cocaine",
    "MoneyPallet",
    "Moonshine",
    "CocaPaste",
    "CocaineBricks",
]


def carry_forward(apps, schema_editor):
    Character = apps.get_model("amc", "Character")
    Delivery = apps.get_model("amc", "Delivery")

    for character in Character.objects.filter(criminal_laundered_total__gt=0):
        character.criminal_score = character.criminal_laundered_total
        character.last_illicit_delivery_at = (
            Delivery.objects.filter(
                character=character, cargo_key__in=ILLICIT_CARGO_KEYS
            )
            .order_by("-timestamp")
            .values_list("timestamp", flat=True)
            .first()
        )
        character.save(
            update_fields=["criminal_score", "last_illicit_delivery_at"]
        )


def reverse_carry_forward(apps, schema_editor):
    Character = apps.get_model("amc", "Character")
    Character.objects.filter(criminal_score__gt=0).update(
        criminal_score=0, last_illicit_delivery_at=None
    )


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0242_criminal_score_fields"),
    ]

    operations = [
        migrations.RunPython(carry_forward, reverse_carry_forward),
    ]
