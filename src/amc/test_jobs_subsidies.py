from django.test import TestCase
from django.utils import timezone
from amc.models import DeliveryJob, Cargo, Player, Character, DeliveryPoint
from amc.webhook import atomic_process_delivery


class SubsidyBugTest(TestCase):
    def setUp(self):
        self.player = Player.objects.create(unique_id=123)
        self.character = Character.objects.create(player=self.player, name="TestChar")
        self.cargo, _ = Cargo.objects.get_or_create(
            key="SunflowerSeed", defaults={"label": "Sunflower Seed"}
        )
        self.point, _ = DeliveryPoint.objects.get_or_create(
            guid="dasa", defaults={"name": "Dasa", "coord": "POINT(0 0 0)"}
        )

    def _make_job(self, bonus_multiplier=0):
        return DeliveryJob.objects.create(
            name="Sunflower for Dasa",
            cargo_key="SunflowerSeed",
            quantity_requested=100,
            quantity_fulfilled=0,
            bonus_multiplier=bonus_multiplier,
            expired_at=timezone.now() + timezone.timedelta(days=1),
        )

    def _make_delivery_data(self, quantity=10, payment=10000, subsidy=0, rp_mode=False):
        return {
            "timestamp": timezone.now(),
            "character": self.character,
            "cargo_key": "SunflowerSeed",
            "quantity": quantity,
            "payment": payment,
            "subsidy": subsidy,
            "rp_mode": rp_mode,
        }

    def test_bonus_multiplier_does_not_affect_subsidy(self):
        """bonus_multiplier is deprecated and no longer modifies subsidy."""
        job = self._make_job(bonus_multiplier=0.5)
        delivery_data = self._make_delivery_data(subsidy=0)

        atomic_process_delivery(job.id, 10, delivery_data)

        self.assertEqual(delivery_data["subsidy"], 0)

    def test_bonus_multiplier_does_not_add_to_existing_subsidy(self):
        """bonus_multiplier does not add to existing cargo subsidy."""
        job = self._make_job(bonus_multiplier=0.5)
        cargo_subsidy = 5000
        delivery_data = self._make_delivery_data(subsidy=cargo_subsidy)

        atomic_process_delivery(job.id, 10, delivery_data)

        self.assertEqual(delivery_data["subsidy"], 5000)

    def test_bonus_multiplier_does_not_add_to_rp_subsidy(self):
        """bonus_multiplier does not stack on top of RP-mode subsidy."""
        job = self._make_job(bonus_multiplier=0.2)
        rp_subsidy = 12500
        delivery_data = self._make_delivery_data(subsidy=rp_subsidy, rp_mode=True)

        atomic_process_delivery(job.id, 10, delivery_data)

        self.assertEqual(delivery_data["subsidy"], 12500)

    def test_high_bonus_multiplier_ignored(self):
        """High bonus multiplier is still ignored."""
        job = self._make_job(bonus_multiplier=4.0)
        cargo_subsidy = 5000
        delivery_data = self._make_delivery_data(subsidy=cargo_subsidy)

        atomic_process_delivery(job.id, 10, delivery_data)

        self.assertEqual(delivery_data["subsidy"], 5000)

    def test_no_bonus_without_job(self):
        """Without a job, subsidy stays unchanged."""
        delivery_data = self._make_delivery_data(subsidy=8000)

        atomic_process_delivery(None, 10, delivery_data)

        self.assertEqual(delivery_data["subsidy"], 8000)
