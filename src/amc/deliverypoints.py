import re

from django.utils import timezone
from django.contrib.gis.geos import Point
from amc.models import Cargo, DeliveryPoint, DeliveryPointStorage, DeliveryJobTemplate
from amc.game_server import get_deliverypoints
from amc.enums import CargoKey
from amc.utils import skip_if_running

cargo_key_by_label = {v: k for k, v in CargoKey.choices}


def normalise_inventory(inventory):
    cargo = inventory["cargo"]
    cargo_key = cargo_key_by_label.get(cargo["name"], cargo["name"])
    return {**inventory, "cargoKey": cargo_key}


def normalise_delivery(delivery):
    cargo_key = cargo_key_by_label.get(delivery["cargo_type"], delivery["cargo_type"])
    return {**delivery, "cargoKey": cargo_key}


def parse_location(location_str):
    """Parse 'X=... Y=... Z=...' into a Point(x, y, z)."""
    match = re.match(r"X=([-\d.]+)\s+Y=([-\d.]+)\s+Z=([-\d.]+)", location_str)
    if not match:
        return None
    return Point(
        float(match.group(1)), float(match.group(2)), float(match.group(3)), srid=3857
    )


@skip_if_running
async def monitor_deliverypoints(ctx):
    session = ctx["http_client"]

    dps_info = await get_deliverypoints(session)
    dps_data = dps_info.get("data", {})

    cargo_by_key = {
        cargo.key: cargo async for cargo in Cargo.objects.select_related("type").all()
    }

    # Prefetch all existing delivery points in one query
    api_guids = {dp_info["guid"].lower() for dp_info in dps_data.values()}
    existing_dps = {
        dp.guid: dp async for dp in DeliveryPoint.objects.filter(guid__in=api_guids)
    }

    seen_guids = set()
    # Collect storage objects for batch upsert
    storage_upserts = []
    # Collect delivery points that need their data field updated
    dps_to_update_data = []
    # Collect delivery points that need name/removed synced
    dps_to_sync = []

    for dp_info in dps_data.values():
        guid = dp_info["guid"].lower()
        seen_guids.add(guid)

        dp = existing_dps.get(guid)
        if dp:
            # Sync name, coord, removed if changed
            updated = False
            if dp.name != dp_info["name"]:
                dp.name = dp_info["name"]
                updated = True
            if dp.removed:
                dp.removed = False
                updated = True
            new_coord = parse_location(dp_info.get("location", ""))
            if new_coord and dp.coord.coords != new_coord.coords:
                dp.coord = new_coord
                updated = True
            if updated:
                dps_to_sync.append(dp)
        else:
            # Auto-create new delivery point
            coord = parse_location(dp_info.get("location", ""))
            if not coord:
                print(
                    f"Could not parse location for {dp_info['guid']}: {dp_info.get('location')}"
                )
                continue
            dp = await DeliveryPoint.objects.acreate(
                guid=guid,
                name=dp_info["name"],
                coord=coord,
            )
            # Create storages from API inventory
            new_storages = []
            for inventory in dp_info.get("InputInventory", {}).values():
                if "cargo" not in inventory:
                    continue
                cargo_key = cargo_key_by_label.get(
                    inventory["cargo"]["name"], inventory["cargo"].get("cargo_key", "")
                )
                cargo = cargo_by_key.get(cargo_key)
                new_storages.append(
                    DeliveryPointStorage(
                        delivery_point=dp,
                        kind=DeliveryPointStorage.Kind.INPUT,
                        cargo_key=cargo_key,
                        cargo=cargo,
                        amount=inventory["amount"],
                    )
                )
            for inventory in dp_info.get("OutputInventory", {}).values():
                if "cargo" not in inventory:
                    continue
                cargo_key = cargo_key_by_label.get(
                    inventory["cargo"]["name"], inventory["cargo"].get("cargo_key", "")
                )
                cargo = cargo_by_key.get(cargo_key)
                new_storages.append(
                    DeliveryPointStorage(
                        delivery_point=dp,
                        kind=DeliveryPointStorage.Kind.OUTPUT,
                        cargo_key=cargo_key,
                        cargo=cargo,
                        amount=inventory["amount"],
                    )
                )
            if new_storages:
                await DeliveryPointStorage.objects.abulk_create(new_storages)
            print(f"Created delivery point: {dp_info['name']} ({guid})")

        # Update live data
        dp.data = {
            "inputInventory": [
                normalise_inventory(inv) for inv in dp_info.get("InputInventory", {}).values() if "cargo" in inv
            ],
            "outputInventory": [
                normalise_inventory(inv) for inv in dp_info.get("OutputInventory", {}).values() if "cargo" in inv
            ],
            "deliveries": list(
                map(normalise_delivery, dp_info.get("Deliveries", {}).values())
            ),
        }
        dps_to_update_data.append(dp)

        # Collect storage upserts for batch operation
        for inventory in dp.data["inputInventory"]:
            cargo = cargo_by_key.get(inventory["cargoKey"])
            storage_upserts.append(
                DeliveryPointStorage(
                    delivery_point=dp,
                    kind=DeliveryPointStorage.Kind.INPUT,
                    cargo_key=inventory["cargoKey"],
                    cargo=cargo,
                    amount=inventory["amount"],
                )
            )

        for inventory in dp.data["outputInventory"]:
            cargo = cargo_by_key.get(inventory["cargoKey"])
            storage_upserts.append(
                DeliveryPointStorage(
                    delivery_point=dp,
                    kind=DeliveryPointStorage.Kind.OUTPUT,
                    cargo_key=inventory["cargoKey"],
                    cargo=cargo,
                    amount=inventory["amount"],
                )
            )

    # Batch sync name/removed changes
    if dps_to_sync:
        await DeliveryPoint.objects.abulk_update(
            dps_to_sync, ["name", "coord", "removed", "last_updated"]
        )

    # Batch save data field updates
    if dps_to_update_data:
        now = timezone.now()
        for dp in dps_to_update_data:
            dp.last_updated = now
        await DeliveryPoint.objects.abulk_update(
            dps_to_update_data, ["data", "last_updated"]
        )

    # Batch upsert all storage records
    if storage_upserts:
        await DeliveryPointStorage.objects.abulk_create(
            storage_upserts,
            update_conflicts=True,
            unique_fields=["delivery_point", "kind", "cargo_key"],
            update_fields=["cargo", "amount"],
        )

    # Mark removed: DB entries not seen in the API
    newly_removed = []
    async for dp in DeliveryPoint.objects.filter(removed=False).exclude(
        guid__in=seen_guids
    ):
        dp.removed = True
        await dp.asave(update_fields=["removed", "last_updated"])
        newly_removed.append(dp)

    # Disable templates referencing newly removed points
    if newly_removed:
        removed_guids = [dp.guid for dp in newly_removed]
        templates_to_disable = DeliveryJobTemplate.objects.filter(
            enabled=True,
        ).filter(
            source_points__guid__in=removed_guids,
        ) | DeliveryJobTemplate.objects.filter(
            enabled=True,
        ).filter(
            destination_points__guid__in=removed_guids,
        )
        async for template in templates_to_disable.distinct():
            template.enabled = False
            await template.asave(update_fields=["enabled"])
            print(
                f"Disabled template: {template.name} (id={template.id}) — references removed delivery point"
            )
