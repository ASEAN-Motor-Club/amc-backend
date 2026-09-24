from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.contrib.gis.geos import Point
from django.utils import timezone

from amc.economy_dashboard import (
    contribution_leaderboard,
    sector_health,
    snapshot_storages,
)
from amc.economy_weights import CARGO_WEIGHTS, SECTORS, sector_of, weight_of
from amc.factories import CharacterFactory, PlayerFactory
from amc.models import (
    Character,
    Delivery,
    DeliveryPoint,
    DeliveryPointStorage,
    StorageSnapshot,
)

pytestmark = pytest.mark.asyncio


async def _point(guid, name):
    return await DeliveryPoint.objects.acreate(
        guid=guid, name=name, type="Factory", coord=Point(1, 2, 3, srid=3857)
    )


async def _cleanup(character, *points):
    """Async tests leak rows in this sandbox — remove our artifacts so later
    test files (leaderboard/webhooks/progression) don't see them."""
    await Delivery.objects.filter(character=character).adelete()
    await StorageSnapshot.objects.all().adelete()
    await DeliveryPointStorage.objects.all().adelete()
    await DeliveryPoint.objects.filter(guid__in=[p.guid for p in points]).adelete()
    # async DB tests in this sandbox leak rows across test files — wipe our
    # factory artifacts entirely so later suites (e.g. exclusive progression's
    # Character.objects.all() count) see a clean DB.
    await Character.objects.all().adelete()
    from amc.models import Player

    await Player.objects.all().adelete()


def test_weights_table_sanity():
    """Ordered sanity: high-value chain cargo > raw ore > consumer floor."""
    assert CARGO_WEIGHTS["SteelCoil_10t"] > CARGO_WEIGHTS["Log_20ft"]
    assert CARGO_WEIGHTS["Log_20ft"] > CARGO_WEIGHTS["IronOre"]
    assert CARGO_WEIGHTS["IronOre"] > CARGO_WEIGHTS["Coal"]
    assert CARGO_WEIGHTS["Coal"] > CARGO_WEIGHTS["Pizza_01"]
    assert CARGO_WEIGHTS["Pizza_01"] >= CARGO_WEIGHTS["GroceryBag"]
    assert weight_of("TotallyUnknownCargo") == 0.0
    # fuel must NOT dominate (freeman): fuel weight well under containers
    assert CARGO_WEIGHTS["Fuel"] < CARGO_WEIGHTS["Container_40ft_01"]


def test_sector_mapping():
    assert sector_of("Fuel") == "energy"
    assert sector_of("Log_20ft") == "logging"
    assert sector_of("SteelCoil_10t") == "metal"
    assert sector_of("Pizza_01") == "retail"
    assert sector_of("UnknownThing") == "other"
    for sector, keys in SECTORS.items():
        assert keys, sector
        assert all(sector_of(k) == sector for k in keys)


@pytest.mark.django_db
async def test_snapshot_storages_and_leaderboard():
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
    point = await _point("dp-econ-1", "Steel Mill")
    await DeliveryPointStorage.objects.acreate(
        delivery_point=point,
        kind=DeliveryPointStorage.Kind.INPUT,
        cargo_key="Coal",
        amount=20,
        capacity=200,
    )
    await DeliveryPointStorage.objects.acreate(
        delivery_point=point,
        kind=DeliveryPointStorage.Kind.INPUT,
        cargo_key="UnknownCargo",
        amount=1,
        capacity=10,
    )

    n = await snapshot_storages()
    assert n == 2
    now = timezone.now()

    # delivery INTO the starved coal bay (deficit_before = 180)
    await Delivery.objects.acreate(
        timestamp=now,
        character=character,
        cargo_key="Coal",
        quantity=50,
        payment=100000,
        destination_point=point,
    )
    # unknown cargo delivery: units counted, score 0
    await Delivery.objects.acreate(
        timestamp=now,
        character=character,
        cargo_key="UnknownCargo",
        quantity=10,
        payment=100,
        destination_point=point,
    )

    board = await contribution_leaderboard(
        now - timedelta(hours=1), now + timedelta(minutes=1)
    )
    row = next(r for r in board if r["character_id"] == character.id)
    assert row["units"] == 60
    assert row["payment"] == 100100
    assert row["score"] == 50 * CARGO_WEIGHTS["Coal"]  # min(50, deficit 180)

    await _cleanup(character, point)


@pytest.mark.django_db
async def test_deficit_cap_overfull_delivery():
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
    point = await _point("dp-econ-3", "Site C")
    await DeliveryPointStorage.objects.acreate(
        delivery_point=point,
        kind=DeliveryPointStorage.Kind.INPUT,
        cargo_key="IronOre",
        amount=120,
        capacity=200,  # deficit 80
    )
    await snapshot_storages()
    now = timezone.now()
    await Delivery.objects.acreate(
        timestamp=now,
        character=character,
        cargo_key="IronOre",
        quantity=500,
        payment=1000,
        destination_point=point,
    )
    board = await contribution_leaderboard(now - timedelta(hours=1), now + timedelta(minutes=1))
    own = next(r for r in board if r["character_id"] == character.id)
    assert own["score"] == 80 * CARGO_WEIGHTS["IronOre"]  # capped at deficit

    await _cleanup(character, point)


@pytest.mark.django_db
async def test_no_snapshots_neutral_full_credit():
    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(player=player)
    point = await _point("dp-econ-2", "Site B")
    now = timezone.now()
    await Delivery.objects.acreate(
        timestamp=now,
        character=character,
        cargo_key="IronOre",
        quantity=7,
        payment=21000,
        destination_point=point,
    )
    board = await contribution_leaderboard(
        now - timedelta(hours=1), now + timedelta(minutes=1)
    )
    own = next(r for r in board if r["character_id"] == character.id)
    assert own["score"] == 7 * CARGO_WEIGHTS["IronOre"]

    await _cleanup(character, point)


@pytest.mark.django_db
async def test_sector_health():
    # wipe leftover test storages/points from earlier (leaky) tests first
    await DeliveryPointStorage.objects.all().adelete()
    await StorageSnapshot.objects.all().adelete()
    point = await _point("dp-sector-1", "Mill")
    await DeliveryPointStorage.objects.acreate(
        delivery_point=point,
        kind=DeliveryPointStorage.Kind.INPUT,
        cargo_key="Coal",
        amount=10,
        capacity=200,  # 5% -> starved
    )
    point2 = await _point("dp-sector-2", "Store")
    await DeliveryPointStorage.objects.acreate(
        delivery_point=point2,
        kind=DeliveryPointStorage.Kind.INPUT,
        cargo_key="Sofa_01",
        amount=900,
        capacity=1000,  # 90%
    )
    # capacity-less row excluded from the sector fill
    await DeliveryPointStorage.objects.acreate(
        delivery_point=point2,
        kind=DeliveryPointStorage.Kind.INPUT,
        cargo_key="GroceryBag",
        amount=0,
        capacity=0,
    )

    sectors = {s["sector"]: s for s in await sector_health()}
    assert sectors["mining"]["fill"] == 10 / 200
    assert sectors["mining"]["starved_sites"] == 1
    assert sectors["retail"]["fill"] == 0.9
    assert sectors["retail"]["starved_sites"] == 0
    fills = [s["fill"] for s in await sector_health()]
    assert fills == sorted(fills)  # sorted worst-first

    await DeliveryPointStorage.objects.all().adelete()
    for p in (point, point2):
        await DeliveryPoint.objects.filter(guid=p.guid).adelete()
