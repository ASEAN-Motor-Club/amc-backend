from django.test import TestCase
from django.utils import timezone
from amc.models import DeliveryJob, Cargo, Player, Character, DeliveryPoint
from amc.webhook import atomic_process_delivery

class SubsidyBugTest(TestCase):
    def setUp(self):
        self.player = Player.objects.create(unique_id=123)
        self.character = Character.objects.create(player=self.player, name="TestChar")
        self.cargo, _ = Cargo.objects.get_or_create(key="SunflowerSeed", defaults={"label": "Sunflower Seed"})
        self.point, _ = DeliveryPoint.objects.get_or_create(guid="dasa", defaults={"name": "Dasa", "type": "T", "coord": "POINT(0 0 0)"})

    def test_insane_bonus_repro(self):
        # Create a job with a multiplier
        job = DeliveryJob.objects.create(
            name="Sunflower for Dasa",
            cargo_key="SunflowerSeed",
            quantity_requested=100,
            quantity_fulfilled=0,
            bonus_multiplier=2.0, # 2x bonus? 
            expired_at=timezone.now() + timezone.timedelta(days=1)
        )
        
        # Simulate a delivery of 10 sunflower seeds
        # payment is PER ITEM in the webhook's 'payment' variable, 
        # but delivery_data['payment'] is total payment.
        
        item_payment = 1000
        quantity = 10
        total_payment = item_payment * quantity
        
        delivery_data = {
            'timestamp': timezone.now(),
            'character': self.character,
            'cargo_key': "SunflowerSeed",
            'quantity': quantity,
            'payment': total_payment,
            'subsidy': 0, # Initial subsidy from rules
            'rp_mode': False,
        }
        
        # Call the bugged function
        atomic_process_delivery(job.id, quantity, delivery_data)
        
        # Check the subsidy in delivery_data (it's modified in place)
        # Current bugged logic: bonus = quantity_to_add * job.bonus_multiplier * delivery_data['payment']
        # bonus = 10 * 2.0 * 10000 = 200,000
        
        self.assertEqual(delivery_data['subsidy'], 10000)
        # Wait, if the user wanted 2x payment, they should get 10,000 extra, not 200,000.
        # 200,000 is 20x the base payment.

    def test_rp_mode_repro(self):
        # RP mode logic in handle_cargo_arrived:
        # delivery_data['subsidy'] = (cargo_subsidy * 1.5) + (payment * quantity * 0.5)
        
        # Suppose cargo_subsidy is 5000 (from some rule)
        # base_pay is 10000
        # RP subsidy = 5000 * 1.5 + 10000 * 0.5 = 7500 + 5000 = 12500
        
        # This part happens in handle_cargo_arrived, not atomic_process_delivery.
        # Let's test atomic_process_delivery with pre-existing RP subsidy.
        
        job = DeliveryJob.objects.create(
            name="Job with lower bonus",
            cargo_key="SunflowerSeed",
            quantity_requested=100,
            quantity_fulfilled=0,
            bonus_multiplier=1.2, # +20% bonus = 2000
            expired_at=timezone.now() + timezone.timedelta(days=1)
        )
        
        quantity = 10
        total_payment = 10000
        rp_subsidy = 12500 # Pre-calculated RP subsidy
        
        delivery_data = {
            'timestamp': timezone.now(),
            'character': self.character,
            'cargo_key': "SunflowerSeed",
            'quantity': quantity,
            'payment': total_payment,
            'subsidy': rp_subsidy,
            'rp_mode': True,
        }
        
        atomic_process_delivery(job.id, quantity, delivery_data)
        
        # Job bonus is 10000 * 0.2 = 2000.
        # Existing subsidy is 12500.
        # 12500 > 2000, so it should stay 12500.
        self.assertEqual(delivery_data['subsidy'], 12500)

    def test_job_wins_over_subsidy(self):
        job = DeliveryJob.objects.create(
            name="High bonus job",
            cargo_key="SunflowerSeed",
            quantity_requested=100,
            quantity_fulfilled=0,
            bonus_multiplier=5.0, # +400% bonus = 40000
            expired_at=timezone.now() + timezone.timedelta(days=1)
        )
        
        quantity = 10
        total_payment = 10000
        subsidy = 5000
        
        delivery_data = {
            'timestamp': timezone.now(),
            'character': self.character,
            'cargo_key': "SunflowerSeed",
            'quantity': quantity,
            'payment': total_payment,
            'subsidy': subsidy,
            'rp_mode': False,
        }
        
        atomic_process_delivery(job.id, quantity, delivery_data)
        
        # Job bonus = 10000 * (5.0 - 1) = 40000
        # 40000 > 5000, so it should be 40000
        self.assertEqual(delivery_data['subsidy'], 40000)
