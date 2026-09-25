from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.contrib.gis.geos import Point
from django.utils import timezone

from amc.economy_dashboard import (
    contribution_leaderboard,
    sector_drilldown,
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


async def _point(guid, name, type="Factory"):
    return await DeliveryPoint.objects.acreate(
        guid=guid, name=name, type=type, coord=Point(1, 2, 3, srid=3857)
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
    board = await contribution_leaderboard(
        now - timedelta(hours=1), now + timedelta(minutes=1)
    )
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


async def test_illicit_cargo_excluded(db):
    """Illicit cargo never snapshots, never scores, never fills sectors."""
    player = await sync_to_async(PlayerFactory)()
    char = await sync_to_async(CharacterFactory)(player=player, name="econ-illicit")
    point = await _point("econ-illicit-dp", "Illicit Site")
    try:
        await DeliveryPointStorage.objects.acreate(
            delivery_point=point,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="GanjaPallet",
            amount=100,
            capacity=100,
        )
        await DeliveryPointStorage.objects.acreate(
            delivery_point=point,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="IronOre",
            amount=50,
            capacity=100,
        )
        now = timezone.now()
        await Delivery.objects.acreate(
            character=char,
            cargo_key="Cocaine",
            quantity=10,
            payment=500_000,
            destination_point=point,
            timestamp=now,
        )
        await Delivery.objects.acreate(
            character=char,
            cargo_key="IronOre",
            quantity=10,
            payment=1_000,
            destination_point=point,
            timestamp=now,
        )

        await snapshot_storages()
        # snapshot: only the IronOre row (GanjaPallet excluded)
        snap = await sync_to_async(list)(
            StorageSnapshot.objects.filter(delivery_point_id=point.guid)
        )
        assert [s.cargo_key for s in snap] == ["IronOre"]

        # sector fill: mining = 50/100 only
        sectors = {s["sector"]: s for s in await sector_health()}
        assert sectors["mining"]["amount"] == 50
        assert sectors["mining"]["capacity"] == 100

        # leaderboard: cocaine scores 0, ore scores
        board = await contribution_leaderboard(now - timedelta(hours=1), now)
        me = next(c for c in board if c["name"] == "econ-illicit")
        assert me["units"] == 10  # ore only
    finally:
        await _cleanup(char, point)


async def test_sector_drilldown_groups_and_orders(db):
    """Drilldown: sector membership via INPUT cargo, starved-first, storages list."""
    p_starved = await _point("econ-dd-mine-a", "Alpha Mine")
    p_full = await _point("econ-dd-mine-b", "Beta Mine")
    p_other = await _point("econ-dd-retail", "Retail Depot")
    try:
        # starved mining site
        await DeliveryPointStorage.objects.acreate(
            delivery_point=p_starved,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="IronOre",
            amount=10,
            capacity=100,
        )
        await DeliveryPointStorage.objects.acreate(
            delivery_point=p_starved,
            kind=DeliveryPointStorage.Kind.OUTPUT,
            cargo_key="SteelCoil_10t",
            amount=5,
            capacity=20,
        )
        # healthy mining site
        await DeliveryPointStorage.objects.acreate(
            delivery_point=p_full,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="Coal",
            amount=90,
            capacity=100,
        )
        # retail site (not in mining sector)
        await DeliveryPointStorage.objects.acreate(
            delivery_point=p_other,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="GroceryBag",
            amount=90,
            capacity=100,
        )
        # illicit input row at the starved site — must not appear
        await DeliveryPointStorage.objects.acreate(
            delivery_point=p_starved,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="Ganja",
            amount=100,
            capacity=100,
        )

        detail = await sector_drilldown("mining")
        assert detail["sector"] == "mining"
        guids = [s["guid"] for s in detail["sites"]]
        assert p_starved.guid in guids and p_full.guid in guids
        assert p_other.guid not in guids  # retail cargo doesn't map to mining

        starved_first = detail["sites"][0]
        assert starved_first["guid"] == p_starved.guid
        assert starved_first["starved"] is True
        assert starved_first["fill"] == pytest.approx(0.1)
        # storages: INPUT rows first, illicit Ganja absent, only sector-
        # relevant rows kept (SteelCoil OUT belongs to metal, not mining)
        cargos = [r["cargo"] for r in starved_first["storages"]]
        assert "Ganja" not in cargos
        assert set(cargos) == {"IronOre"}
        assert starved_first["storages"][0]["kind"] == DeliveryPointStorage.Kind.INPUT

        # starved_only filters to just the hungry site
        only = await sector_drilldown("mining", starved_only=True)
        assert [s["guid"] for s in only["sites"]] == [p_starved.guid]

        # unknown sector -> None (route 404s)
        assert await sector_drilldown("not-a-sector") is None
    finally:
        await DeliveryPointStorage.objects.all().adelete()
        for p in (p_starved, p_full, p_other):
            await DeliveryPoint.objects.filter(guid=p.guid).adelete()


async def test_sector_drilldown_other_sector(db):
    """'other' sector is reachable and groups INPUT rows of unmapped cargo."""
    p = await _point("econ-dd-other", "Odd Site")
    try:
        await DeliveryPointStorage.objects.acreate(
            delivery_point=p,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="MysteryCrate",
            amount=3,
            capacity=10,
        )
        detail = await sector_drilldown("other")
        assert detail["sector"] == "other"
        assert [s["guid"] for s in detail["sites"]] == [p.guid]
        assert detail["sites"][0]["fill"] == pytest.approx(0.3)
    finally:
        await DeliveryPointStorage.objects.all().adelete()
        await DeliveryPoint.objects.filter(guid=p.guid).adelete()


def test_illicit_weights_and_sector_guard():
    """weight_of / sector_of refuse illicit cargo even if callers bypass filters."""
    from amc.special_cargo import ILLICIT_CARGO_KEYS

    assert ILLICIT_CARGO_KEYS  # sanity: the guard has teeth
    for key in ILLICIT_CARGO_KEYS:
        assert weight_of(key) == 0.0
        assert sector_of(key) == "other"


async def test_drilldown_excludes_unmetered_sites(db):
    """Capacity-less INPUT rows (e.g. temp 'Building Construction Site' DPs)
    are excluded from the drilldown listing."""
    p_metered = await _point("econ-dd-metered", "Real Site")
    p_unmetered = await _point(
        "econ-dd-temp", "Building Construction Site", type=""
    )  # prod temp sites are type-blank
    try:
        await DeliveryPointStorage.objects.acreate(
            delivery_point=p_metered,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="IronOre",
            amount=10,
            capacity=100,
        )
        await DeliveryPointStorage.objects.acreate(
            delivery_point=p_unmetered,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="IronOre",
            amount=0,
            capacity=0,  # the game never reports demand for temp construction DPs
        )
        detail = await sector_drilldown("mining")
        guids = [s["guid"] for s in detail["sites"]]
        assert p_metered.guid in guids
        assert p_unmetered.guid not in guids

        # a site with one metered row survives even if another row is unmetered
        await DeliveryPointStorage.objects.acreate(
            delivery_point=p_metered,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="Coal",
            amount=0,
            capacity=0,
        )
        detail2 = await sector_drilldown("mining")
        guids2 = [s["guid"] for s in detail2["sites"]]
        assert p_metered.guid in guids2
        assert p_unmetered.guid not in guids2
    finally:
        await DeliveryPointStorage.objects.all().adelete()
        for p in (p_metered, p_unmetered):
            await DeliveryPoint.objects.filter(guid=p.guid).adelete()


async def test_warehouse_out_stock_not_starved(db):
    """A storage warehouse with empty IN but stocked OUT of the same cargo
    (the output IS the storage) is not starved; a factory with empty intake
    and a different-cargo output still is."""
    wh = await _point("econ-wh", "Fuel Storage Warehouse")
    fac = await _point("econ-fac", "Steel Factory")
    try:
        await DeliveryPointStorage.objects.acreate(
            delivery_point=wh,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="Fuel",
            amount=0,
            capacity=100,
        )
        await DeliveryPointStorage.objects.acreate(
            delivery_point=wh,
            kind=DeliveryPointStorage.Kind.OUTPUT,
            cargo_key="Fuel",
            amount=385,
            capacity=500,
        )
        await DeliveryPointStorage.objects.acreate(
            delivery_point=fac,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="IronOre",
            amount=0,
            capacity=100,
        )
        await DeliveryPointStorage.objects.acreate(
            delivery_point=fac,
            kind=DeliveryPointStorage.Kind.OUTPUT,
            cargo_key="SteelCoil_10t",
            amount=200,
            capacity=300,
        )

        detail = await sector_drilldown("energy")
        by_name = {s["name"]: s for s in detail["sites"]}
        assert by_name["Fuel Storage Warehouse"]["starved"] is False

        metal = await sector_drilldown(
            "mining"
        )  # the factory's intake (IronOre) is mining; its output (SteelCoil) is metal
        assert (
            {s["name"]: s["starved"] for s in metal["sites"]}
            == {
                "Steel Factory": True  # different-cargo output (SteelCoil) doesn't rescue an empty intake
            }
        )

        health = {s["sector"]: s for s in await sector_health()}
        # the warehouse's empty Fuel IN row no longer counts as starved
        assert health["energy"]["starved_sites"] == 0
    finally:
        await DeliveryPointStorage.objects.all().adelete()
        for p in (wh, fac):
            await DeliveryPoint.objects.filter(guid=p.guid).adelete()


async def test_sector_mapping_farm_exemption(db):
    """Limestone/LimestoneRock are construction inputs (cement chain); farms
    consume QuicklimePallet as fertilizer so Farm sites don't join
    construction via it; containers are generic logistics cargo, not
    construction."""
    assert sector_of("Limestone") == "construction"
    assert sector_of("LimestoneRock") == "construction"

    farm = await _point("econ-farm", "Aewol Pumpkin Farm", type="Farm")
    cement = await _point("econ-cem", "Cement Factory", type="Factory")
    cont = await _point("econ-cont", "Harbor Warehouse", type="Warehouse")
    try:
        await DeliveryPointStorage.objects.acreate(
            delivery_point=farm,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="QuicklimePallet",
            amount=10,
            capacity=100,
        )
        await DeliveryPointStorage.objects.acreate(
            delivery_point=cement,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="QuicklimePallet",
            amount=10,
            capacity=100,
        )
        await DeliveryPointStorage.objects.acreate(
            delivery_point=cont,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="Container_20ft_01",
            amount=5,
            capacity=100,
        )

        detail = await sector_drilldown("construction")
        guids = {s["guid"] for s in detail["sites"]}
        assert cement.guid in guids  # factory consuming quicklime = construction
        assert farm.guid not in guids  # farm consuming quicklime = fertilizer use
        assert cont.guid not in guids  # containers don't map to construction

        # farms still surface under food via their crop INPUTs
        await DeliveryPointStorage.objects.acreate(
            delivery_point=farm,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="PumpkinPallet",
            amount=20,
            capacity=100,
        )
        food = await sector_drilldown("food")
        assert farm.guid in {s["guid"] for s in food["sites"]}
    finally:
        await DeliveryPointStorage.objects.all().adelete()
        for p in (farm, cement, cont):
            await DeliveryPoint.objects.filter(guid=p.guid).adelete()


async def test_typed_unmetered_sites_stay_listed(db):
    """Warehouse pallet stock is unmetered in DB but resolves to the Pallet
    category default (50), so warehouses list under food with a real fill.
    Anonymous type-blank temp sites (Building Construction Site) stay
    excluded."""
    wh = await _point("econ-wh2", "Gwangjin Warehouse", type="Warehouse")
    temp = await _point("econ-temp2", "Building Construction Site", type="")
    try:
        await DeliveryPointStorage.objects.acreate(
            delivery_point=wh,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="CheesePallet",
            amount=50,
            capacity=0,
        )
        await DeliveryPointStorage.objects.acreate(
            delivery_point=temp,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="Concrete",
            amount=0,
            capacity=0,
        )

        food = await sector_drilldown("food")
        by_name = {s["name"]: s for s in food["sites"]}
        assert by_name["Gwangjin Warehouse"]["fill"] == 1.0  # 50/50 Pallet default
        assert by_name["Gwangjin Warehouse"]["starved"] is False

        con = await sector_drilldown("construction")
        assert "Building Construction Site" not in {s["name"] for s in con["sites"]}
    finally:
        await DeliveryPointStorage.objects.all().adelete()
        for p in (wh, temp):
            await DeliveryPoint.objects.filter(guid=p.guid).adelete()


async def test_depot_storages_active_only(db):
    """Depot listing: active (removed=False) player depots with pallet-50
    storages and delivery inflow; depots the game dropped (removed=True)
    are excluded."""
    from amc.economy_dashboard import depot_storages
    from amc.models import DeliveryPoint

    live = await DeliveryPoint.objects.acreate(
        guid="dp-depot-live",
        name="FreyCo Depot",
        type="",
        coord=Point(1, 2, 3, srid=3857),
        removed=False,
    )
    await DeliveryPoint.objects.acreate(
        guid="dp-depot-dead",
        name="Depot (Gone Co)",
        type="",
        coord=Point(4, 5, 6, srid=3857),
        removed=True,
    )
    try:
        await DeliveryPointStorage.objects.acreate(
            delivery_point=live,
            kind=DeliveryPointStorage.Kind.INPUT,
            cargo_key="BoxPallete_01",
            amount=20,
            capacity=0,
        )
        await Delivery.objects.acreate(
            timestamp=timezone.now(),
            character=await sync_to_async(CharacterFactory)(
                player=await sync_to_async(PlayerFactory)()
            ),
            cargo_key="BoxPallete_01",
            quantity=7,
            payment=1000,
            destination_point=live,
        )

        depots = {d["name"]: d for d in await depot_storages()}
        assert "FreyCo Depot" in depots
        assert "Depot (Gone Co)" not in depots
        d = depots["FreyCo Depot"]
        assert d["units_24h"] == 7
        assert d["deliveries_7d"] == 1
        rows = {r["cargo"]: r for r in d["storages"]}
        assert rows["BoxPallete_01"]["capacity"] == 50  # Pallet category default
    finally:
        await Delivery.objects.all().adelete()
        await DeliveryPointStorage.objects.all().adelete()
        await DeliveryPoint.objects.filter(
            guid__in=("dp-depot-live", "dp-depot-dead")
        ).adelete()
        # factory Player/Character rows leak into later suites (exclusive
        # progression counts Characters) — wipe them like _cleanup does
        await Character.objects.all().adelete()
        from amc.models import Player

        await Player.objects.all().adelete()
