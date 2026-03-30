import typing
import re
import logging
from amc.models import CharacterVehicle, PoliceSession
from amc.mod_server import list_player_vehicles, spawn_vehicle, show_popup
from amc.enums import VehiclePartSlot
from amc.mod_detection import (
    detect_custom_parts, detect_incompatible_parts,
    format_custom_parts_plain, format_incompatible_parts_plain,
    POLICE_DUTY_WHITELIST,
)

logger = logging.getLogger(__name__)

# Reverse lookup: slot name -> numeric value from VehiclePartSlot enum
_SLOT_NAME_TO_INT = {slot.name: slot.value for slot in VehiclePartSlot}




def workshop_export_to_db_config(json_data: dict) -> dict:
    """Convert workshop API json_data (FMTGarageVehicleExport) to
    CharacterVehicle.config-compatible format.

    Workshop API (racers.co.kr) uses:
      - camelCase keys (bHasPaint, paint, decal, parts)
      - Named slot strings from EMTVehiclePartSlot (e.g. "Engine", "Tire0")

    DB (CharacterVehicle.config) uses:
      - PascalCase keys (Customization, Decal, Parts)
      - Numeric slot indices as ints (e.g. 2, 19)

    Returns a dict with "Parts", "Decal", and "Customization" keys matching
    the DB format, suitable for direct comparison with CharacterVehicle.config.
    """

    result: typing.Dict[str, typing.Any] = {}

    # ── Parts ──
    # Workshop: {"parts": {"parts": [{"key": ..., "slot": "Engine", ...}]}}
    # DB:       {"Parts": [{"Key": ..., "Slot": 2, ...}]}
    ws_parts_wrapper = json_data.get("parts", {})
    ws_parts = (
        ws_parts_wrapper.get("parts", [])
        if isinstance(ws_parts_wrapper, dict)
        else ws_parts_wrapper
    )
    db_parts = []
    for p in ws_parts:
        slot_name = p.get("slot", "")
        slot_int = _SLOT_NAME_TO_INT.get(slot_name, -1)
        db_part = {
            "Key": p.get("key", ""),
            "Slot": slot_int,
            "FloatValues": list(p.get("floatValues", {}).values()),
            "Int64Values": list(p.get("int64Values", {}).values()),
            "StringValues": list(p.get("stringValues", {}).values()),
            "VectorValues": list(p.get("vectorValues", {}).values()),
        }
        db_parts.append(db_part)
    result["Parts"] = db_parts

    # ── Decal ──
    # Workshop: {"decal": {"decal": {"decalLayers": [...]}}}
    # DB:       {"Decal": {"DecalLayers": [...]}}
    ws_decal = json_data.get("decal", {})
    ws_decal_inner = ws_decal.get("decal", ws_decal) if isinstance(ws_decal, dict) else {}
    ws_layers = ws_decal_inner.get("decalLayers", []) if isinstance(ws_decal_inner, dict) else []

    db_layers = []
    for layer in ws_layers:
        db_layer = {
            "DecalKey": layer.get("decalKey", ""),
            "DecalScale": layer.get("decalScale", 0),
            "Stretch": layer.get("stretch", 1),
            "Coverage": layer.get("coverage", 1),
            "Flags": layer.get("flags", 0),
        }
        # Color: {r,g,b,a} -> {R,G,B,A}
        if color := layer.get("color"):
            db_layer["Color"] = {
                "R": color.get("r", 0),
                "G": color.get("g", 0),
                "B": color.get("b", 0),
                "A": color.get("a", 255),
            }
        # Position: {x,y} -> {X,Y}
        if pos := layer.get("position"):
            db_layer["Position"] = {"X": pos.get("x", 0), "Y": pos.get("y", 0)}
        # Rotation: {pitch,yaw,roll} -> {Pitch,Yaw,Roll}
        if rot := layer.get("rotation"):
            db_layer["Rotation"] = {
                "Pitch": rot.get("pitch", 0),
                "Yaw": rot.get("yaw", 0),
                "Roll": rot.get("roll", 0),
            }
        db_layers.append(db_layer)
    result["Decal"] = {"DecalLayers": db_layers}

    # ── Customization (Paint) ──
    # Workshop: {"paint": {"bodyMaterialIndex": 0, "bodyColors": [...]}}
    # DB:       {"Customization": {"BodyMaterialIndex": 0, "BodyColors": [...]}}
    ws_paint = json_data.get("paint", {})
    if isinstance(ws_paint, dict):
        db_colors = []
        for c in ws_paint.get("bodyColors", []):
            db_color = {"MaterialSlotName": c.get("materialSlotName", "")}
            if color := c.get("color"):
                db_color["Color"] = {
                    "R": color.get("r", 0),
                    "G": color.get("g", 0),
                    "B": color.get("b", 0),
                    "A": color.get("a", 255),
                }
            db_colors.append(db_color)
        result["Customization"] = {
            "BodyMaterialIndex": ws_paint.get("bodyMaterialIndex", 0),
            "BodyColors": db_colors,
        }

    return result


async def register_player_vehicles(http_client_mod, character, player, active=None):
    player_vehicles = await list_player_vehicles(
        http_client_mod, player.unique_id, active=active, complete=True
    )
    if not player_vehicles:
        return

    if not isinstance(player_vehicles, dict):
        return []

    # Whitelist police parts for officers on active duty
    whitelist = None
    is_on_duty = await PoliceSession.objects.filter(
        character=character, ended_at__isnull=True
    ).aexists()
    if is_on_duty:
        whitelist = POLICE_DUTY_WHITELIST

    results = []
    for vehicle_id, vehicle in player_vehicles.items():
        if int(vehicle_id) < 0:
            continue
        owner = character
        if len(vehicle["companyName"]) > 0:
            owner = None

        config = {
            "CompanyGuid": vehicle["companyGuid"],
            "CompanyName": vehicle["companyName"],
            "Customization": vehicle["customization"],
            "Decal": vehicle["decal"],
            "Parts": vehicle["parts"],
            "Location": vehicle["position"],
            "Rotation": vehicle["rotation"],
            "Net_VehicleOwnerSetting": vehicle.get("Net_VehicleOwnerSetting", None),
        }
        vehicle_name = vehicle["fullName"].split(" ")[0].replace("_C", "")
        config["VehicleName"] = vehicle_name
        asset_path = vehicle["classFullName"].split(" ")[1]
        config["AssetPath"] = asset_path

        if owner:
            v, _ = await CharacterVehicle.objects.aupdate_or_create(
                character=owner, vehicle_id=int(vehicle_id), defaults={"config": config}
            )
        else:
            v, _ = await CharacterVehicle.objects.aupdate_or_create(
                company_guid=vehicle["companyGuid"],
                vehicle_id=int(vehicle_id),
                defaults={"config": config},
            )
        results.append(v)

        # Check main vehicle for custom/modded parts
        if vehicle.get("isLastVehicle") and vehicle.get("index", -1) == 0:
            custom = detect_custom_parts(vehicle.get("parts", []), whitelist=whitelist)
            if custom:
                logger.warning(
                    "Custom parts detected on %s's %s (#%s):\n%s",
                    character.name,
                    vehicle_name,
                    vehicle_id,
                    format_custom_parts_plain(custom),
                )
            incompatible = detect_incompatible_parts(
                vehicle.get("parts", []), vehicle["fullName"]
            )
            if incompatible:
                logger.warning(
                    "Incompatible parts detected on %s's %s (#%s):\n%s",
                    character.name,
                    vehicle_name,
                    vehicle_id,
                    format_incompatible_parts_plain(incompatible),
                )

    return results


def format_key_string(key_str):
    """
    Converts a key string into a more readable format.
    - Replaces underscores with spaces.
    - Splits CamelCase words by inserting a space before uppercase letters
      (unless it's the start of the string or already preceded by a space).

    Args:
        key_str (str): The input key string (e.g., "LSD_Clutch_2_100" or "HeavyMachineOffRoadFrontTire").

    Returns:
        str: The formatted, readable string.
    """
    if not key_str:
        return ""

    # 1. Replace all underscores with spaces
    s1 = key_str.replace("_", " ")

    # 2. Use regex to insert a space before uppercase letters that follow a non-space character
    # (?<!^) ensures it's not the beginning of the string
    # (?<! ) ensures it's not already preceded by a space
    # ([A-Z]) captures the uppercase letter
    # r' \1' inserts a space before the captured letter
    s2 = re.sub(r"(?<!^)(?<! )([A-Z])", r" \1", s1)

    return s2


def format_vehicle_part_game(part):
    key = format_key_string(part["Key"])
    slot = VehiclePartSlot(part["Slot"])
    return f"{slot.name}: {key}"


def format_vehicle_part(part):
    key = format_key_string(part["Key"])
    slot = VehiclePartSlot(part["Slot"])
    return f"**{slot.name}**: {key}"


def format_vehicle_parts(parts):
    sorted_parts = sorted(parts, key=lambda p: p["Slot"])
    return "\n".join([format_vehicle_part(p) for p in sorted_parts])


def format_vehicle_name(vehicle_full_name):
    vehicle_name = vehicle_full_name.split(" ")[0].replace("_C", "")
    return vehicle_name


async def spawn_player_vehicle(
    http_client_mod,
    character,
    vehicle_id,
    location,
    for_sale=False,
):
    try:
        vehicle = await CharacterVehicle.objects.aget(
            character=character,
            vehicle_id=vehicle_id,
        )
    except CharacterVehicle.DoesNotExist:
        await show_popup(
            http_client_mod,
            "Unrecognised vehicle ID. Please spawn it on the server at least once.",
            character_guid=character.guid,
        )
        return

    if (
        "Raven" in vehicle.config["AssetPath"]
        or "Terra" in vehicle.config["AssetPath"]
        or "Formula" in vehicle.config["AssetPath"]
    ):
        raise Exception("You may not sell this vehicle")

    await spawn_registered_vehicle(
        http_client_mod,
        vehicle,
        location=location,
        tag=character.name,
        driver_guid=character.guid,
        for_sale=for_sale,
    )


async def spawn_registered_vehicle(
    http_client_mod,
    vehicle,
    location=None,
    rotation={},
    tag="player_vehicles",
    tags=[],
    driver_guid=None,
    for_sale=None,
    extra_data={},
):
    if not location:
        location = vehicle.config["Location"]
    if not rotation:
        rotation = vehicle.config.get("Rotation", {})

    extra_data = {
        **extra_data,
        "profitShare": 0,
        "tags": tags,
    }
    if owner_setting := vehicle.config.get("Net_VehicleOwnerSetting"):
        extra_data["profitShare"] = owner_setting.get("VehicleOwnerProfitShare", 0)

    if vehicle.config.get("CompanyGuid") and vehicle.config.get("CompanyName"):
        extra_data["companyGuid"] = vehicle.config.get("CompanyGuid")
        extra_data["companyName"] = vehicle.config.get("CompanyName")
    if for_sale is not None:
        extra_data["forSale"] = for_sale
    else:
        extra_data["forSale"] = vehicle.for_sale

    await spawn_vehicle(
        http_client_mod,
        vehicle.config["AssetPath"],
        location,
        rotation=rotation,
        customization=vehicle.config["Customization"],
        decal=vehicle.config["Decal"],
        parts=[{**p, "partKey": p["Key"]} for p in vehicle.config["Parts"]],
        extra_data=extra_data,
        driver_guid=driver_guid,
        tag=tag,
    )
