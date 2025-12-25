from django.test import TestCase
from django.contrib.gis.geos import Point, Polygon
from decimal import Decimal
from amc.models import SubsidyRule, SubsidyArea, Cargo, DeliveryPoint
from amc.subsidies import get_subsidy_for_cargo
from unittest.mock import MagicMock

class SubsidyLogicTest(TestCase):
    def setUp(self):
        # Create Cargos
        self.cargo_coal, _ = Cargo.objects.get_or_create(key="Coal", defaults={"label": "Coal"})
        self.cargo_burger, _ = Cargo.objects.get_or_create(key="Burger_01_Signature", defaults={"label": "Burger"})
        
        # Create Areas
        # Polygon around (0,0) to (10,10)
        self.area_gwangjin = SubsidyArea.objects.create(
            name="Gwangjin Area",
            polygon=Polygon(((0,0), (0,10), (10,10), (10,0), (0,0)), srid=3857)
        )
        
        # Create Points (srid=3857)
        self.point_in = DeliveryPoint.objects.create(
            guid="p_in", name="In Point", type="T", 
            coord=Point(5, 5, 0, srid=3857)
        )
        self.point_out = DeliveryPoint.objects.create(
            guid="p_out", name="Out Point", type="T", 
            coord=Point(20, 20, 0, srid=3857)
        )

    async def test_basic_cargo_rule(self):
        # Rule: Coal gets 150% (1.5)
        rule = await SubsidyRule.objects.acreate(
            name="Coal Subsidy",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("1.50"),
            priority=10
        )
        await rule.cargos.aadd(self.cargo_coal)
        
        # Test Coal
        mock_cargo = MagicMock()
        mock_cargo.cargo_key = "Coal"
        mock_cargo.payment = 1000
        mock_cargo.sender_point = self.point_in
        mock_cargo.destination_point = self.point_out
        mock_cargo.data = {}
        mock_cargo.damage = 0.0

        amount, factor = await get_subsidy_for_cargo(mock_cargo)
        self.assertEqual(factor, 1.5)
        self.assertEqual(amount, 1500)

        # Test Burger (should not match)
        mock_cargo.cargo_key = "Burger_01_Signature"
        amount, factor = await get_subsidy_for_cargo(mock_cargo)
        self.assertEqual(amount, 0)
    
    async def test_source_area_restriction(self):
        # Rule: Any cargo from Gwangjin gets 2.0
        rule = await SubsidyRule.objects.acreate(
            name="Gwangjin Export",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("2.00"),
            priority=10
        )
        await rule.source_areas.aadd(self.area_gwangjin)
        
        # Test from IN point
        mock_cargo = MagicMock()
        mock_cargo.cargo_key = "Coal"
        mock_cargo.payment = 1000
        mock_cargo.sender_point = self.point_in
        mock_cargo.destination_point = self.point_out
        mock_cargo.data = {}
        mock_cargo.damage = 0.0

        amount, factor = await get_subsidy_for_cargo(mock_cargo)
        self.assertEqual(factor, 2.0)

        # Test from OUT point
        mock_cargo.sender_point = self.point_out
        amount, factor = await get_subsidy_for_cargo(mock_cargo)
        self.assertEqual(amount, 0) # No match

    async def test_priority(self):
        # Low priority global rule: 1.1
        r1 = await SubsidyRule.objects.acreate(
            name="Global Low",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("1.10"),
            priority=1
        )
        
        # High priority specific rule: 2.0
        r2 = await SubsidyRule.objects.acreate(
            name="Specific High",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("2.00"),
            priority=10
        )
        await r2.cargos.aadd(self.cargo_coal)
        
        mock_cargo = MagicMock()
        mock_cargo.cargo_key = "Coal"
        mock_cargo.payment = 1000
        mock_cargo.sender_point = self.point_out
        mock_cargo.destination_point = self.point_out
        mock_cargo.data = {}
        mock_cargo.damage = 0.0

        amount, factor = await get_subsidy_for_cargo(mock_cargo)
        self.assertEqual(factor, 2.0) # High priority wins

    async def test_damage_scaling(self):
        # Rule with damage scaling
        rule = await SubsidyRule.objects.acreate(
            name="Fragile",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("2.00"),
            priority=10,
            scales_with_damage=True
        )
        
        mock_cargo = MagicMock()
        mock_cargo.cargo_key = "Glass"
        mock_cargo.payment = 1000
        mock_cargo.sender_point = None
        mock_cargo.destination_point = None
        mock_cargo.data = {}
        mock_cargo.damage = 0.1 # 10% damage

        amount, factor = await get_subsidy_for_cargo(mock_cargo)
        # Expected: 2.0 * (1.0 - 0.1) = 1.8
        self.assertAlmostEqual(factor, 1.8)
        self.assertEqual(amount, 1800)

    async def test_get_subsidies_text(self):
        from amc.subsidies import get_subsidies_text
        
        # Create active rule
        r1 = await SubsidyRule.objects.acreate(
            name="Active Rule",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("3.00"),
            active=True,
            priority=10
        )
        await r1.cargos.aadd(self.cargo_burger)
        
        # Create inactive rule
        r2 = await SubsidyRule.objects.acreate(
            name="Inactive Rule",
            reward_type=SubsidyRule.RewardType.FLAT,
            reward_value=Decimal("5000"),
            active=False,
            priority=10
        )
        
        # Create rule with areas
        r3 = await SubsidyRule.objects.acreate(
            name="Area Rule",
            reward_type=SubsidyRule.RewardType.FLAT,
            reward_value=Decimal("1000"),
            active=True,
            priority=5
        )
        await r3.source_areas.aadd(self.area_gwangjin)
        
        text = await get_subsidies_text()
        
        # Note: Name isn't in text, but Cargo is.
        self.assertIn("Burger", text) 
        self.assertIn("300%", text)
        
        self.assertNotIn("Inactive Rule", text)
        self.assertNotIn("5000 coins", text)
        
        self.assertIn("Any Cargo", text) # From r3
        self.assertIn("1000 coins", text)
        self.assertIn("From: Gwangjin Area", text)
