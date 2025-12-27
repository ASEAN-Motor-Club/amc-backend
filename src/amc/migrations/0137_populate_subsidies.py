from django.db import migrations
from django.contrib.gis.geos import Point
from decimal import Decimal

# Helper to resolve DeliveryPoint or fall back to Area
def get_point_or_area(SubsidyArea, DeliveryPoint, point_name, area_name=None, create_area_if_missing=False, buffer_dist=100):
    # Try to find DeliveryPoint
    dp = DeliveryPoint.objects.filter(name=point_name).first()
    if dp:
        return ('POINT', dp)

    # Fallback to Area
    if area_name is None:
         area_name = point_name
         
    # Check if area exists
    area = SubsidyArea.objects.filter(name=area_name).first()
    if area:
        return ('AREA', area)
        
    if create_area_if_missing:
         print(f"Warning: DeliveryPoint '{point_name}' not found. Cannot create area without coordinates.")
         return None

    return None

def populate_subsidies(apps, schema_editor):
    SubsidyRule = apps.get_model('amc', 'SubsidyRule')
    SubsidyArea = apps.get_model('amc', 'SubsidyArea')
    DeliveryPoint = apps.get_model('amc', 'DeliveryPoint')
    Cargo = apps.get_model('amc', 'Cargo')

    # ensure cargos exist
    cargos = {
        'Burger_01_Signature': 'Signature Burger',
        'Pizza_01_Premium': 'Premium Pizza',
        'LiveFish_01': 'Live Fish',
        'AirlineMealPallet': 'Airline Meal Pallet',
        'Log_Oak_12ft': '12ft Oak Log',
        'Coal': 'Coal',
        'Iron Ore': 'Iron Ore',
        'WoodPlank_14ft_5t': 'Wood Plank 14ft 5t',
        'Fuel': 'Fuel',
        'BottlePallete': 'Water Bottle Pallete',
        'MeatBox': 'Meat Box',
        'TrashBag': 'Trash Bag',
        'Trash_Big': 'Big Trash',
    }
    
    cargo_objs = {}
    for key, label in cargos.items():
        obj, _ = Cargo.objects.get_or_create(key=key, defaults={'label': label})
        cargo_objs[key] = obj

    # 1. Burger/Pizza/Fish - 300% on time
    rule = SubsidyRule.objects.create(
        name="Burger/Pizza/Fish Priority",
        reward_type='PERCENTAGE',
        reward_value=Decimal('3.0'),
        active=True,
        requires_on_time=True,
        priority=10
    )
    rule.cargos.add(cargo_objs['Burger_01_Signature'])
    rule.cargos.add(cargo_objs['Pizza_01_Premium'])
    rule.cargos.add(cargo_objs['LiveFish_01'])

    # 2. Airline Meals - 200% on time
    rule = SubsidyRule.objects.create(
        name="Airline Meals",
        reward_type='PERCENTAGE',
        reward_value=Decimal('2.0'),
        active=True,
        requires_on_time=True,
        priority=10
    )
    rule.cargos.add(cargo_objs['AirlineMealPallet'])

    # 3. Oak Log - 250% damage scale
    rule = SubsidyRule.objects.create(
        name="Oak Logs",
        reward_type='PERCENTAGE',
        reward_value=Decimal('2.5'),
        active=True,
        scales_with_damage=True,
        priority=10
    )
    rule.cargos.add(cargo_objs['Log_Oak_12ft'])

    # Helper function to add source/dest based on type
    def add_location(rule, location, is_source=True):
        if not location:
            return
        type, obj = location
        if type == 'POINT':
            if is_source:
                rule.source_delivery_points.add(obj)
            else:
                rule.destination_delivery_points.add(obj)
        elif type == 'AREA':
            if is_source:
                rule.source_areas.add(obj)
            else:
                rule.destination_areas.add(obj)

    # 4. Coal/Iron Ore - 150% (Gwangjin Mine -> Gwangjin Storage)
    loc_coal = get_point_or_area(SubsidyArea, DeliveryPoint, "Gwangjin Coal")
    loc_iron_mine = get_point_or_area(SubsidyArea, DeliveryPoint, "Gwangjin Iron Ore Mine")
    loc_iron_storage = get_point_or_area(SubsidyArea, DeliveryPoint, "Gwangjin Iron Ore Storage")
    loc_coal_storage = get_point_or_area(SubsidyArea, DeliveryPoint, "Gwangjin Coal Storage")
    
    if (loc_coal or loc_iron_mine) and (loc_iron_storage or loc_coal_storage):
        rule = SubsidyRule.objects.create(
            name="Gwangjin Coal/Iron Logistics",
            reward_type='PERCENTAGE',
            reward_value=Decimal('1.5'),
            priority=15
        )
        rule.cargos.add(cargo_objs['Coal'])
        rule.cargos.add(cargo_objs['Iron Ore'])
        add_location(rule, loc_coal, is_source=True)
        add_location(rule, loc_iron_mine, is_source=True)
        add_location(rule, loc_iron_storage, is_source=False)
        add_location(rule, loc_coal_storage, is_source=False)

    # 5. Planks - 250%
    loc_plank_storage = get_point_or_area(SubsidyArea, DeliveryPoint, "Gwangjin Plank Storage")
    loc_oak1 = get_point_or_area(SubsidyArea, DeliveryPoint, "Migeum Oak 1")
    loc_oak2 = get_point_or_area(SubsidyArea, DeliveryPoint, "Migeum Oak 2")
    loc_oak3 = get_point_or_area(SubsidyArea, DeliveryPoint, "Migeum Oak 3")

    if loc_plank_storage:
        rule = SubsidyRule.objects.create(
            name="Gwangjin Planks Logistic",
            reward_type='PERCENTAGE',
            reward_value=Decimal('2.5'),
            priority=15
        )
        rule.cargos.add(cargo_objs['WoodPlank_14ft_5t'])
        add_location(rule, loc_plank_storage, is_source=True)
        for loc in [loc_coal, loc_iron_mine, loc_oak1, loc_oak2, loc_oak3]:
            add_location(rule, loc, is_source=False)

    # 6. Fuel - 150%
    loc_fuel_storage = get_point_or_area(SubsidyArea, DeliveryPoint, "Gwangjin Fuel Storage")
    if loc_fuel_storage:
         rule = SubsidyRule.objects.create(
            name="Gwangjin Fuel Supply",
            reward_type='PERCENTAGE',
            reward_value=Decimal('1.5'),
            priority=15
        )
         rule.cargos.add(cargo_objs['Fuel'])
         add_location(rule, loc_fuel_storage, is_source=True)
         add_location(rule, loc_coal, is_source=False)
         add_location(rule, loc_iron_mine, is_source=False)

    loc_log_warehouse = get_point_or_area(SubsidyArea, DeliveryPoint, "Migeum Log Warehouse")
    if loc_log_warehouse:
         rule = SubsidyRule.objects.create(
            name="Migeum Fuel Supply",
            reward_type='PERCENTAGE',
            reward_value=Decimal('1.5'),
            priority=15
        )
         rule.cargos.add(cargo_objs['Fuel'])
         add_location(rule, loc_log_warehouse, is_source=True)
         for loc in [loc_oak1, loc_oak2, loc_oak3]:
             add_location(rule, loc, is_source=False)

    # 7. Water Bottle Pallets 
    loc_gwangjin_market = get_point_or_area(SubsidyArea, DeliveryPoint, "Gwangjin Supermarket")
    loc_ara_market = get_point_or_area(SubsidyArea, DeliveryPoint, "Ara Supermarket")
    
    if loc_gwangjin_market or loc_ara_market:
        rule_wb_300 = SubsidyRule.objects.create(
            name="Water Global Shortage (High)",
            reward_type='PERCENTAGE',
            reward_value=Decimal('3.0'),
            priority=12
        )
        rule_wb_300.cargos.add(cargo_objs['BottlePallete'])
        add_location(rule_wb_300, loc_gwangjin_market, is_source=False)
        add_location(rule_wb_300, loc_ara_market, is_source=False)
    
    supermarket_points = DeliveryPoint.objects.filter(name__icontains="Supermarket")
    if supermarket_points.exists():
         rule_wb_200 = SubsidyRule.objects.create(
            name="Water Supply (Standard)",
            reward_type='PERCENTAGE',
            reward_value=Decimal('2.0'),
            priority=10
         )
         rule_wb_200.cargos.add(cargo_objs['BottlePallete'])
         
         rule_meat = SubsidyRule.objects.create(
            name="Meat Supply",
            reward_type='PERCENTAGE',
            reward_value=Decimal('2.0'),
            priority=10
         )
         rule_meat.cargos.add(cargo_objs['MeatBox'])

         for dp in supermarket_points:
             loc = ('POINT', dp)
             add_location(rule_wb_200, loc, is_source=False)
             add_location(rule_meat, loc, is_source=False)
                 
    # 8. Trash (Areas)
    ara_p = Point(x=329486.94, y=1293697.78, z=-18594.89, srid=3857)
    ara_trash_area = SubsidyArea.objects.filter(name="Ara Trash Zone").first()
    if not ara_trash_area:
        ara_trash_area = SubsidyArea.objects.create(
            name="Ara Trash Zone",
            polygon=ara_p.buffer(180_000), 
            description="Legacy trash zone for Ara (radius 180000)"
        )

    rule_trash_ara = SubsidyRule.objects.create(
        name="Ara Trash Subsidy",
        reward_type='PERCENTAGE',
        reward_value=Decimal('2.0'),
        priority=12
    )
    rule_trash_ara.cargos.add(cargo_objs['TrashBag'])
    rule_trash_ara.cargos.add(cargo_objs['Trash_Big'])
    rule_trash_ara.destination_areas.add(ara_trash_area)
    
    gwangjin_p = Point(x=318700.36, y=816972.24, z=-1636.26, srid=3857)
    gwangjin_trash_area = SubsidyArea.objects.filter(name="Gwangjin Trash Zone").first()
    if not gwangjin_trash_area:
        gwangjin_trash_area = SubsidyArea.objects.create(
            name="Gwangjin Trash Zone",
            polygon=gwangjin_p.buffer(200_000),
            description="Legacy trash zone for Gwangjin (radius 200000)"
        )

    rule_trash_gw = SubsidyRule.objects.create(
        name="Gwangjin Trash Subsidy",
        reward_type='PERCENTAGE',
        reward_value=Decimal('2.5'),
        priority=13
    )
    rule_trash_gw.cargos.add(cargo_objs['TrashBag'])
    rule_trash_gw.cargos.add(cargo_objs['Trash_Big'])
    rule_trash_gw.destination_areas.add(gwangjin_trash_area)
    
    # 9. Gwangjin Supermarket General Catch-all (300%)
    if loc_gwangjin_market:
        rule_catchall = SubsidyRule.objects.create(
            name="Gwangjin Supermarket Stimulus",
            reward_type='PERCENTAGE',
            reward_value=Decimal('3.0'),
            priority=5 
        )
        add_location(rule_catchall, loc_gwangjin_market, is_source=False)
        
        loc_gs = get_point_or_area(SubsidyArea, DeliveryPoint, "Gwangjin Supermarket Gas Station")
        add_location(rule_catchall, loc_gs, is_source=False)


def remove_subsidies(apps, schema_editor):
    SubsidyRule = apps.get_model('amc', 'SubsidyRule')
    names = [
        "Burger/Pizza/Fish Priority",
        "Airline Meals",
        "Oak Logs",
        "Gwangjin Coal/Iron Logistics",
        "Gwangjin Planks Logistic",
        "Gwangjin Fuel Supply",
        "Migeum Fuel Supply",
        "Water Global Shortage (High)",
        "Water Supply (Standard)",
        "Meat Supply",
        "Ara Trash Subsidy",
        "Gwangjin Trash Subsidy",
        "Gwangjin Supermarket Stimulus"
    ]
    SubsidyRule.objects.filter(name__in=names).delete()

class Migration(migrations.Migration):

    dependencies = [
        ('amc', '0136_subsidyrule_destination_delivery_points_and_more'),
    ]

    operations = [
        migrations.RunPython(populate_subsidies, reverse_code=remove_subsidies),
    ]
