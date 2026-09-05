"""RaceSetup hash normalization tests.

Background: the game re-emits race setup configs with float leaves (``0.0``,
``10.0``) while older pipelines stored int leaves (``0``, ``10``). The raw
DeepHash treated those as different setups, so content-identical configs
landed in separate ``RaceSetup`` rows, and time-trial sessions never matched
their scheduled event's ``race_setup`` -- silently breaking TT grouping,
points, and payouts (prod evidence 2026-09-05: setups 807 vs 848).
"""

from datetime import timedelta
from importlib import import_module

from deepdiff import DeepHash
from django.apps import apps
from django.test import TestCase
from django.utils import timezone

from amc.models import GameEvent, RaceSetup, ScheduledEvent

_migration = import_module("amc.migrations.0233_racesetup_rehash_normalize")
mig_normalize = _migration._normalize
mig_normalized_hash = _migration._normalized_hash
rehash_and_merge = _migration.rehash_and_merge

_migration_0234 = import_module("amc.migrations.0234_racesetup_rehash_quantize")
mig234_normalize = _migration_0234._normalize
mig234_normalized_hash = _migration_0234._normalized_hash
rehash_and_merge_quantize = _migration_0234.rehash_and_merge

INT_CONFIG = {
    "NumLaps": 0,
    "Route": {
        "RouteName": "Electric Speed Trap",
        "Waypoints": [
            {
                "Rotation": {"Z": -0.78710683018658, "Y": 0, "X": 0, "W": 0.61681669714238},
                "Location": {"Z": -21100, "Y": 1313667.6959572, "X": -163487.55692178},
                "Scale3D": {"Z": 10, "Y": 14, "X": 1},
            },
            {
                "Rotation": {"Z": 0.99144486142194, "Y": 0, "X": 0, "W": 0.1305261918545},
                "Location": {"Z": -21100.0, "Y": 1316685.0110511, "X": -161572.9122317},
                "Scale3D": {"Z": 10, "Y": 14, "X": 1},
            },
        ],
    },
    "EngineKeys": ["Electric_300HP"],
    "VehicleKeys": ["Pulse"],
}

FLOAT_CONFIG = {
    "NumLaps": 0.0,
    "Route": {
        "RouteName": "Electric Speed Trap",
        "Waypoints": [
            {
                "Rotation": {"Z": -0.78710683018658, "Y": 0.0, "X": 0.0, "W": 0.61681669714238},
                "Location": {"Z": -21100.0, "Y": 1313667.6959572, "X": -163487.55692178},
                "Scale3D": {"Z": 10.0, "Y": 14.0, "X": 1.0},
            },
            {
                "Rotation": {"Z": 0.99144486142194, "Y": 0.0, "X": 0.0, "W": 0.1305261918545},
                "Location": {"Z": -21100.0, "Y": 1316685.0110511, "X": -161572.9122317},
                "Scale3D": {"Z": 10.0, "Y": 14.0, "X": 1.0},
            },
        ],
    },
    "EngineKeys": ["Electric_300HP"],
    "VehicleKeys": ["Pulse"],
}


class RaceSetupHashNormalizationTestCase(TestCase):
    def test_int_and_float_configs_hash_identically(self):
        assert RaceSetup.calculate_hash(INT_CONFIG) == RaceSetup.calculate_hash(
            FLOAT_CONFIG
        )

    def test_raw_deepdiff_still_separates_int_and_float(self):
        """Guards the premise: without normalization DeepHash splits them."""
        h_int = DeepHash(INT_CONFIG)[INT_CONFIG]
        h_float = DeepHash(FLOAT_CONFIG)[FLOAT_CONFIG]
        assert h_int != h_float

    def test_bool_leaves_are_not_coerced(self):
        with_bool = {"a": True}
        with_int = {"a": 1}
        assert RaceSetup.calculate_hash(with_bool) != RaceSetup.calculate_hash(
            with_int
        )

    def test_genuinely_different_configs_still_differ(self):
        other = {**FLOAT_CONFIG, "NumLaps": 3.0}
        assert RaceSetup.calculate_hash(FLOAT_CONFIG) != RaceSetup.calculate_hash(
            other
        )

    def test_model_and_migration_normalize_agree(self):
        # The agree-test targets the LATEST hash scheme (0234, quantized):
        # the model's calculate_hash must match the newest migration's
        # normalizer, or a re-hash pass would store hashes the live code
        # never reproduces.
        assert RaceSetup.normalize_config(INT_CONFIG) == mig234_normalize(INT_CONFIG)
        assert RaceSetup.calculate_hash(INT_CONFIG) == mig234_normalized_hash(
            INT_CONFIG
        )
        assert RaceSetup.calculate_hash(FLOAT_CONFIG) == mig234_normalized_hash(
            FLOAT_CONFIG
        )

    def test_quaternion_float64_noise_hashes_identically(self):
        """The game re-emits Rotation quaternions with ~1e-14 run-to-run
        double-precision jitter (live evidence 2026-09-05: Rotation.W
        0.22719833571551 vs 0.22719833571552617 for the same route).  The
        quantizing normalizer must collapse that noise."""
        noisy = {
            **FLOAT_CONFIG,
            "Route": {
                **FLOAT_CONFIG["Route"],
                "Waypoints": [
                    {
                        **FLOAT_CONFIG["Route"]["Waypoints"][0],
                        "Rotation": {
                            **FLOAT_CONFIG["Route"]["Waypoints"][0]["Rotation"],
                            "W": 0.61681669714239123,
                        },
                    },
                    *FLOAT_CONFIG["Route"]["Waypoints"][1:],
                ],
            },
        }
        assert RaceSetup.calculate_hash(FLOAT_CONFIG) == RaceSetup.calculate_hash(
            noisy
        )
        assert RaceSetup.calculate_hash(noisy) == mig234_normalized_hash(noisy)

    def test_raw_deephash_still_separates_quaternion_noise(self):
        """Guards the premise: without quantization DeepHash splits them."""
        noisy = {
            **FLOAT_CONFIG,
            "Route": {
                **FLOAT_CONFIG["Route"],
                "Waypoints": [
                    {
                        **FLOAT_CONFIG["Route"]["Waypoints"][0],
                        "Rotation": {
                            **FLOAT_CONFIG["Route"]["Waypoints"][0]["Rotation"],
                            "W": 0.61681669714239123,
                        },
                    },
                    *FLOAT_CONFIG["Route"]["Waypoints"][1:],
                ],
            },
        }
        assert DeepHash(FLOAT_CONFIG)[FLOAT_CONFIG] != DeepHash(noisy)[noisy]


class RaceSetupRehashMigrationTestCase(TestCase):
    """Runs the migration's real logic against the test DB."""

    def _make_setup(self, config, name):
        return RaceSetup.objects.create(
            config=config,
            hash=DeepHash(config)[config],
            name=name,
        )

    def test_merge_collapses_drifted_duplicates_and_repoints_fks(self):
        # Simulate prod: ScheduledEvent holds the int-config setup (807), the
        # game re-emits the float variant which created a second row (848),
        # and sessions (GameEvent) linked to the float row.
        survivor = self._make_setup(FLOAT_CONFIG, "Electric Speed Trap")
        drifted = self._make_setup(INT_CONFIG, "Electric Speed Trap")
        assert survivor.hash != drifted.hash

        scheduled = ScheduledEvent.objects.create(
            name="Electric Speed Trap - TT",
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(days=7),
            race_setup=drifted,
        )
        game_event = GameEvent.objects.create(
            name="session",
            guid="ABCDEF0123456789ABCDEF0123456789",
            state=0,
            auto_created=False,
            race_setup=drifted,
        )

        rehash_and_merge(apps, None)

        assert not RaceSetup.objects.filter(id=drifted.id).exists()
        survivor.refresh_from_db()
        # 0233's own scheme (int->float, no quantization): the stored hash
        # must match the migration's normalizer, not the newer model code.
        assert survivor.hash == mig_normalized_hash(FLOAT_CONFIG)
        scheduled.refresh_from_db()
        assert scheduled.race_setup_id == survivor.id
        game_event.refresh_from_db()
        assert game_event.race_setup_id == survivor.id

    def test_null_config_rows_are_untouched(self):
        null_row = RaceSetup.objects.create(config=None, hash="legacy-null-hash")
        rehash_and_merge(apps, None)
        null_row.refresh_from_db()
        assert null_row.hash == "legacy-null-hash"
        assert RaceSetup.objects.filter(id=null_row.id).exists()

    def test_idempotent_on_second_run(self):
        self._make_setup(FLOAT_CONFIG, "Electric Speed Trap")
        rehash_and_merge(apps, None)
        count_before = RaceSetup.objects.filter(
            config__isnull=False, name="Electric Speed Trap"
        ).count()
        rehash_and_merge(apps, None)
        count_after = RaceSetup.objects.filter(
            config__isnull=False, name="Electric Speed Trap"
        ).count()
        assert count_before == 1
        assert count_after == count_before


class RaceSetupQuantizeMigrationTestCase(TestCase):
    """Runs migration 0234's real logic (float quantization + merge)."""

    def _make_setup(self, config, name):
        return RaceSetup.objects.create(
            config=config,
            hash=DeepHash(config)[config],
            name=name,
        )

    @staticmethod
    def _with_quaternion_noise(config):
        return {
            **config,
            "Route": {
                **config["Route"],
                "Waypoints": [
                    {
                        **config["Route"]["Waypoints"][0],
                        "Rotation": {
                            **config["Route"]["Waypoints"][0]["Rotation"],
                            "W": 0.61681669714239123,
                        },
                    },
                    *config["Route"]["Waypoints"][1:],
                ],
            },
        }

    def test_merge_collapses_quaternion_noise_and_repoints_fks(self):
        survivor = self._make_setup(FLOAT_CONFIG, "Electric Speed Trap")
        noisy = self._make_setup(
            self._with_quaternion_noise(FLOAT_CONFIG), "Electric Speed Trap"
        )
        assert survivor.hash != noisy.hash

        scheduled = ScheduledEvent.objects.create(
            name="Electric Speed Trap - TT",
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(days=7),
            race_setup=noisy,
        )
        game_event = GameEvent.objects.create(
            name="session",
            guid="FEDCBA9876543210FEDCBA9876543210",
            state=0,
            auto_created=False,
            race_setup=noisy,
        )

        rehash_and_merge_quantize(apps, None)

        assert not RaceSetup.objects.filter(id=noisy.id).exists()
        survivor.refresh_from_db()
        assert survivor.hash == mig234_normalized_hash(FLOAT_CONFIG)
        scheduled.refresh_from_db()
        assert scheduled.race_setup_id == survivor.id
        game_event.refresh_from_db()
        assert game_event.race_setup_id == survivor.id

    def test_idempotent_on_second_run(self):
        self._make_setup(FLOAT_CONFIG, "Electric Speed Trap")
        rehash_and_merge_quantize(apps, None)
        count_before = RaceSetup.objects.filter(
            config__isnull=False, name="Electric Speed Trap"
        ).count()
        rehash_and_merge_quantize(apps, None)
        count_after = RaceSetup.objects.filter(
            config__isnull=False, name="Electric Speed Trap"
        ).count()
        assert count_before == 1
        assert count_after == count_before
