"""
Custom/modded vehicle parts detection.

Compares a vehicle's parts keys against the stock parts catalogue in the game
sqlite database to identify non-stock (custom/modded) parts.

Player part keys encode tuning parameters inline:
  - Damper200_200  = stock Damper200 with Rebound=2.0
  - BrakePower_200 = stock BrakePower with Multiplier=2.0
  - WheelSpacer_50 = stock WheelSpacer with Space=5.0
  - SmallRadiator_100 = stock SmallRadiator with CoolingPower=1.0

The stock key set includes both base RowNames and all valid tuned variants.

All stock keys are stored **lowercased** and player keys are compared in
lowercase to handle casing mismatches (e.g. game sends Bike_i4_160HP
but DataTable RowName is Bike_I4_160HP).
"""

import os
import sqlite3
import logging
from collections import defaultdict
from typing import Optional

from amc.enums import VehiclePartSlot

log = logging.getLogger(__name__)

GAME_DB_PATH = os.environ.get("GAME_DB_PATH", "/var/lib/motortown/gamedata.db")

# Attachment slots (cosmetic) should not count as modded parts
ATTACHMENT_SLOT_MIN = VehiclePartSlot.Attachment0.value  # 148

# Part key prefixes whitelisted for characters on active police duty
POLICE_DUTY_WHITELIST: tuple[str, ...] = (
    "apf_",
)

# Module-level caches — loaded once on first call
_stock_part_keys: Optional[set[str]] = None
_part_compatible_types: Optional[dict[str, set[str]]] = None
_vehicle_type_map: Optional[dict[str, str]] = None


# Suffix encoding rules per part_type.
# Each rule: (struct_type, field_name, multiplier)
# Generates: f"{base_row_name}_{int(value * multiplier)}" for each unique value
SUFFIX_RULES: dict[str, list[tuple[str, str, int]]] = {
    "Suspension_Damper": [
        ("SuspensionDamper", "ReboundDampingRateMultiplier", 100),
    ],
    "BrakePower": [
        ("BrakePower", "BrakePowerMultiplier", 100),
    ],
    "CoolantRadiator": [
        ("CoolantRadiator", "CoolingPower", 100),
    ],
    "WheelSpacer": [
        ("WheelSpacer", "Space", 10),
    ],
    "AntiRollBar": [
        ("AntiRollBar", "AntiRollBarRateMultiplier", 100),
    ],
    "AngleKit": [
        ("AngleKit", "AngleIncreaseInDegree", 1),
    ],
}


def get_stock_part_keys() -> set[str]:
    """
    Load all stock part keys from the game database, including
    all valid tuned-name variants.

    For suffix-encoded parts (Damper, BrakePower, etc.), generates
    valid keys by combining each base RowName with ALL known tuning
    values of that part type.

    For tire/LSD parts, generates valid keys by combining each base
    RowName with blueprint variant suffixes from the game pak.

    Results are cached in-memory after first call.
    """
    global _stock_part_keys
    if _stock_part_keys is not None:
        return _stock_part_keys

    try:
        conn = sqlite3.connect(
            f"file:{GAME_DB_PATH}?mode=ro",
            uri=True,
            timeout=5,
        )
        cursor = conn.cursor()

        # Load base part IDs and types
        parts_by_type: dict[str, set[str]] = defaultdict(set)
        all_part_ids = set()
        for row in cursor.execute("SELECT id, part_type FROM vehicle_parts"):
            part_id, part_type = row[0], row[1] or ""
            all_part_ids.add(part_id)
            parts_by_type[part_type].add(part_id)

        # Collect all unique tuning values per (part_type, struct_type, field_name)
        # Uses part_type_values table which preserves all values from duplicate RowNames
        values_by_type: dict[str, dict[tuple[str, str], set[float]]] = defaultdict(
            lambda: defaultdict(set)
        )
        try:
            for row in cursor.execute(
                "SELECT part_type, struct_type, field_name, field_value FROM part_type_values"
            ):
                part_type, struct_type, field_name, value = row
                values_by_type[part_type][(struct_type, field_name)].add(value)
        except sqlite3.OperationalError:
            # Fallback for old DB without part_type_values table
            pass

        # Load blueprint variant suffixes (tire/LSD pak asset variants)
        # Maps (asset_type, base_name) -> set of variant_name
        bp_variants: dict[str, dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        try:
            for row in cursor.execute(
                "SELECT base_name, variant_name, asset_type FROM blueprint_variants"
            ):
                base_name, variant_name, asset_type = row
                bp_variants[asset_type][base_name].add(variant_name)
        except sqlite3.OperationalError:
            pass

        conn.close()

        # Build valid keys
        _stock_part_keys = {k.lower() for k in all_part_ids}

        # Suffix rules for tuning-encoded parts (Damper, BrakePower, etc.)
        for part_type, rules in SUFFIX_RULES.items():
            base_ids = parts_by_type.get(part_type, set())
            type_values = values_by_type.get(part_type, {})
            for base_id in base_ids:
                for struct_type, field_name, multiplier in rules:
                    values = type_values.get((struct_type, field_name), set())
                    for value in values:
                        suffix_val = int(value * multiplier)
                        _stock_part_keys.add(f"{base_id}_{suffix_val}".lower())

        # Tire blueprint variant suffixes
        # Extract unique suffix values from TirePhysics variants only
        tire_suffix_values: set[int] = set()
        for base_name, variants in bp_variants.get("TirePhysics", {}).items():
            for variant_name in variants:
                suffix = variant_name[len(base_name):]
                for part in suffix.lstrip("_").split("_"):
                    if part.isdigit():
                        tire_suffix_values.add(int(part))

        for part_id in parts_by_type.get("Tire", set()):
            for val in tire_suffix_values:
                _stock_part_keys.add(f"{part_id}_{val}".lower())

        # LSD blueprint variant suffixes
        # Extract unique suffix values from LSD variants only
        lsd_suffix_values: set[int] = set()
        for base_name, variants in bp_variants.get("LSD", {}).items():
            for variant_name in variants:
                suffix = variant_name[len(base_name):]
                for part in suffix.split("_"):
                    if part.isdigit():
                        lsd_suffix_values.add(int(part))

        for part_id in parts_by_type.get("LSD", set()):
            for val in lsd_suffix_values:
                _stock_part_keys.add(f"{part_id}_{val}".lower())
                # Double suffix for 1.5-way (accel + brake)
                for val2 in lsd_suffix_values:
                    _stock_part_keys.add(f"{part_id}_{val}_{val2}".lower())

        tuned_count = len(_stock_part_keys) - len(all_part_ids)
        log.info(
            "Loaded %d stock part keys (%d base + %d tuned variants) from game database",
            len(_stock_part_keys),
            len(all_part_ids),
            tuned_count,
        )
    except Exception as e:
        log.error(f"Failed to load stock part keys: {e}")
        _stock_part_keys = set()

    return _stock_part_keys


def _slot_name(slot_value: int) -> str:
    """Get human-readable slot name from slot enum value."""
    try:
        return VehiclePartSlot(slot_value).name
    except ValueError:
        return f"Unknown({slot_value})"


def detect_custom_parts(
    parts: list[dict],
    whitelist: Optional[tuple[str, ...]] = None,
) -> list[dict]:
    """
    Detect custom/modded parts in a vehicle's parts list.

    Args:
        parts: List of part dicts with at least 'Key' and 'Slot' fields.
        whitelist: Optional tuple of lowercased key prefixes to skip.

    Returns:
        List of dicts for each custom part found:
        [{'key': str, 'slot': str, 'slot_value': int}]
    """
    stock_keys = get_stock_part_keys()
    if not stock_keys:
        log.warning("Stock parts cache is empty — skipping detection")
        return []

    custom = []
    for part in parts:
        key = part.get("Key", "")
        slot_value = part.get("Slot", 0)
        if not key:
            continue
        # Skip attachment slots — cosmetic items, not performance mods
        if slot_value >= ATTACHMENT_SLOT_MIN:
            continue
        key_lower = key.lower()
        if key_lower in stock_keys:
            continue
        if whitelist and key_lower.startswith(whitelist):
            continue
        custom.append({
            "key": key,
            "slot": _slot_name(slot_value),
            "slot_value": slot_value,
        })

    return custom


def _load_compatibility_data():
    """Load part-vehicle type compatibility data from the game database.

    Populates two caches:
    - _part_compatible_types: part_id -> set of allowed vehicle_type strings
    - _vehicle_type_map: vehicle blueprint name -> vehicle_type
    """
    global _part_compatible_types, _vehicle_type_map
    if _part_compatible_types is not None:
        return

    try:
        conn = sqlite3.connect(
            f"file:{GAME_DB_PATH}?mode=ro",
            uri=True,
            timeout=5,
        )
        cursor = conn.cursor()

        # part_id -> set of allowed vehicle types
        compat: dict[str, set[str]] = defaultdict(set)
        try:
            for row in cursor.execute(
                "SELECT part_id, vehicle_type FROM part_compatible_types"
            ):
                compat[row[0].lower()].add(row[1])
        except sqlite3.OperationalError:
            pass

        # vehicle blueprint name -> vehicle_type
        vtype_map: dict[str, str] = {}
        try:
            for row in cursor.execute(
                "SELECT id, vehicle_type FROM vehicles"
            ):
                vtype_map[row[0]] = row[1]
        except sqlite3.OperationalError:
            pass

        conn.close()

        _part_compatible_types = dict(compat)
        _vehicle_type_map = vtype_map
        log.info(
            "Loaded compatibility data: %d parts with type restrictions, %d vehicles",
            len(_part_compatible_types),
            len(_vehicle_type_map),
        )
    except Exception as e:
        log.error(f"Failed to load compatibility data: {e}")
        _part_compatible_types = {}
        _vehicle_type_map = {}


def _strip_part_suffix(key: str, stock_keys: set[str]) -> str:
    """Strip tuning suffixes from a player part key to get the base part ID.

    Player keys like 'Damper200_200' or 'BasicTire_65' encode tuning values
    as trailing underscore-separated numbers. This progressively strips
    trailing _N segments until a stock base ID is found.

    Both key and candidates are compared in lowercase since stock_keys
    are stored lowercased.
    """
    key_lower = key.lower()
    if key_lower in stock_keys:
        return key_lower
    parts = key_lower.split("_")
    # Try removing trailing numeric segments one at a time
    for i in range(len(parts) - 1, 0, -1):
        if not parts[i].lstrip("-").isdigit():
            break
        candidate = "_".join(parts[:i])
        if candidate in stock_keys:
            return candidate
    return key_lower


def detect_incompatible_parts(
    parts: list[dict], vehicle_name: str
) -> list[dict]:
    """Check if parts are compatible with the vehicle type.

    Maps the vehicle blueprint name to a vehicle_type, then checks each
    part's allowed vehicle types. Parts with no compatibility data in the
    DB are skipped (they may be universal or unknown).

    Args:
        parts: List of part dicts with at least 'Key' and 'Slot' fields.
        vehicle_name: The vehicle's fullName from the mod server
                      (e.g. 'Jemusi_C Default__Jemusi').

    Returns:
        List of dicts for each incompatible part found:
        [{'key': str, 'slot': str, 'slot_value': int,
          'vehicle_type': str, 'allowed_types': list[str]}]
    """
    _load_compatibility_data()
    if not _part_compatible_types or not _vehicle_type_map:
        return []

    # Extract blueprint name: 'Jemusi_C Default__Jemusi' -> 'Jemusi'
    blueprint_name = vehicle_name.split(" ")[0].replace("_C", "")
    vehicle_type = _vehicle_type_map.get(blueprint_name)
    if not vehicle_type:
        return []

    stock_keys = get_stock_part_keys()
    incompatible = []
    for part in parts:
        key = part.get("Key", "")
        slot_value = part.get("Slot", 0)
        if not key:
            continue

        base_id = _strip_part_suffix(key, stock_keys)
        allowed = _part_compatible_types.get(base_id)
        if allowed is not None and vehicle_type not in allowed:
            incompatible.append({
                "key": key,
                "slot": _slot_name(slot_value),
                "slot_value": slot_value,
                "vehicle_type": vehicle_type,
                "allowed_types": sorted(allowed),
            })

    return incompatible


def format_custom_parts(custom_parts: list[dict]) -> str:
    """Format custom parts list for display."""
    if not custom_parts:
        return "✅ All stock parts"
    lines = [f"**{p['slot']}**: {p['key']}" for p in custom_parts]
    return "\n".join(lines)


def format_custom_parts_plain(custom_parts: list[dict]) -> str:
    """Format custom parts list for plain text (management command)."""
    if not custom_parts:
        return "All stock parts"
    lines = [f"  {p['slot']}: {p['key']}" for p in custom_parts]
    return "\n".join(lines)


def format_custom_parts_game(custom_parts: list[dict]) -> str:
    """Format custom parts list for in-game popup (Motor Town markup)."""
    if not custom_parts:
        return "All stock parts"
    lines = [f"<Bold>{p['slot']}</> {p['key']}" for p in custom_parts]
    return "\n".join(lines)


def format_incompatible_parts(incompatible_parts: list[dict]) -> str:
    """Format incompatible parts list for Discord display."""
    if not incompatible_parts:
        return ""
    lines = [
        f"**{p['slot']}**: {p['key']} (allowed: {', '.join(p['allowed_types'])})"
        for p in incompatible_parts
    ]
    return "\n".join(lines)


def format_incompatible_parts_plain(incompatible_parts: list[dict]) -> str:
    """Format incompatible parts list for plain text (management command)."""
    if not incompatible_parts:
        return ""
    lines = [
        f"  {p['slot']}: {p['key']} (allowed: {', '.join(p['allowed_types'])})"
        for p in incompatible_parts
    ]
    return "\n".join(lines)


def format_incompatible_parts_game(incompatible_parts: list[dict]) -> str:
    """Format incompatible parts list for in-game popup (Motor Town markup)."""
    if not incompatible_parts:
        return ""
    lines = [
        f"<Bold>{p['slot']}</> {p['key']} (allowed: {', '.join(p['allowed_types'])})"
        for p in incompatible_parts
    ]
    return "\n".join(lines)
