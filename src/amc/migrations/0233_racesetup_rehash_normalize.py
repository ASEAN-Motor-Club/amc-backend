"""Re-hash RaceSetup rows with int/float normalization and merge duplicates.

The game re-emits race setup configs with float leaves (``0.0``, ``10.0``)
while older pipelines stored int leaves (``0``, ``10``).
``RaceSetup.calculate_hash`` hashed the raw structure, so content-identical
setups landed in separate rows with different hashes and never matched the
``race_setup`` reference held by ``ScheduledEvent`` rows -- silently breaking
time-trial grouping, points, and prize payouts.

This migration recomputes every hash with the normalized (int -> float)
scheme and merges rows that collapse together: foreign keys are re-pointed to
the lowest-id survivor and duplicates are deleted.

Self-contained on purpose: historical-model access only, no live-model
imports, so the logic must not drift from the module it mirrors.
"""

from django.db import migrations
from deepdiff import DeepHash


def _normalize(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        return value
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value


def _normalized_hash(config):
    normalized = _normalize(config)
    return DeepHash(normalized)[normalized]


def rehash_and_merge(apps, schema_editor):
    RaceSetup = apps.get_model("amc", "RaceSetup")
    GameEvent = apps.get_model("amc", "GameEvent")
    ScheduledEvent = apps.get_model("amc", "ScheduledEvent")

    rows = list(
        RaceSetup.objects.filter(config__isnull=False).values("id", "config", "hash")
    )

    # Group content-identical setups by their normalized hash.
    groups: dict[str, list[int]] = {}
    for row in rows:
        groups.setdefault(_normalized_hash(row["config"]), []).append(row["id"])

    # Merge each group down to its lowest-id survivor: re-point foreign keys,
    # delete duplicates.
    for ids in groups.values():
        survivor_id = min(ids)
        for dupe_id in ids:
            if dupe_id == survivor_id:
                continue
            GameEvent.objects.filter(race_setup_id=dupe_id).update(
                race_setup_id=survivor_id
            )
            ScheduledEvent.objects.filter(race_setup_id=dupe_id).update(
                race_setup_id=survivor_id
            )
            RaceSetup.objects.filter(id=dupe_id).delete()

    # Two-phase update so rows crossing hash values can't trip the unique
    # constraint against a not-yet-updated row.
    survivors = []
    for new_hash, ids in groups.items():
        survivor_id = min(ids)
        current = next(r["hash"] for r in rows if r["id"] == survivor_id)
        if current != new_hash:
            survivors.append((survivor_id, new_hash))

    for survivor_id, _ in survivors:
        RaceSetup.objects.filter(id=survivor_id).update(
            hash=f"rehash-{survivor_id}"
        )
    for survivor_id, new_hash in survivors:
        RaceSetup.objects.filter(id=survivor_id).update(hash=new_hash)


def un_rehash_and_merge(apps, schema_editor):
    # Data migration: the merged duplicates cannot be reconstructed.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0232_player_muted_until"),
    ]

    operations = [
        migrations.RunPython(rehash_and_merge, un_rehash_and_merge),
    ]
