"""Alias → asset-path mapping for the /spawn_object admin command.

Asset paths are game PAK paths resolvable by the mod's spawn primitive
(``AssetManager.SpawnActor`` → ``StaticFindObject``):

- Blueprint classes: ``/Game/<pkg>.<Pkg>_C`` — full actors (interaction,
  vendor logic, etc.).
- Raw static meshes: ``/Game/<dir>/<MeshName>.<MeshName>`` — each workshop
  mesh is its own cooked package (``.../Props/SM_Prop_X``), so the package
  part of the path ends with ``/<MeshName>`` and the object part repeats
  the mesh name. Spawned as an ``AStaticMeshActor`` with the mesh applied
  (decor only, no interaction).

Every mesh path here was verified present in the v0.7.19 client PAK via
``mt-pak-extract --search-all`` (sm_prop_meshes.txt index). The mesh-path
form is load-bearing: a dot instead of a slash before the package's mesh
name (``Props.SM_Prop_X``) names a package that does not exist and makes
the mod's ``LoadAsset`` + re-find fail with "Failed to spawn asset" — hit
live on production 2026-09-10, every mesh alias 500'd until this was
corrected (test: ``test_mesh_paths_use_package_slash_form``).

Spawned objects are session-only; the
command persists them as ``WorldObject`` rows so the restart-time
``spawn_world_assets`` task respawns them.
"""

import dataclasses
from typing import Final


@dataclasses.dataclass(frozen=True)
class SpawnableObject:
    """A game asset spawnable via the mod's generic /assets/spawn endpoint."""

    alias: str
    asset_path: str
    description: str
    category: str


GarageProps: Final[str] = "/Game/Models/PolygonStreetRacer/Meshes/Props"
OfficeProps: Final[str] = "/Game/AssetsvilleTown/Meshes/InteriorProps/Office"

CAT_EQUIPMENT: Final[str] = "Garage equipment"
CAT_PARTS: Final[str] = "Car parts & fluids"
CAT_STRUCTURE: Final[str] = "Garage structure"
CAT_SHELVING: Final[str] = "Shelving"
CAT_INTERACTIVE: Final[str] = "Interactive"

SPAWNABLE_OBJECT_CATEGORIES: Final[list[str]] = [
    CAT_EQUIPMENT,
    CAT_PARTS,
    CAT_STRUCTURE,
    CAT_SHELVING,
    CAT_INTERACTIVE,
]

SPAWNABLE_OBJECTS: Final[dict[str, SpawnableObject]] = {
    obj.alias: obj
    for obj in (
        # Garage equipment
        SpawnableObject(
            "engine_crane",
            f"{GarageProps}/SM_Prop_EngineCrane_01_Preset.SM_Prop_EngineCrane_01_Preset",
            "Engine crane (engine hoist)",
            CAT_EQUIPMENT,
        ),
        SpawnableObject(
            "engine_stand",
            f"{GarageProps}/SM_Prop_EngineStand_01_Preset.SM_Prop_EngineStand_01_Preset",
            "Engine stand",
            CAT_EQUIPMENT,
        ),
        SpawnableObject(
            "mechanic_lift",
            f"{GarageProps}/SM_Prop_MechanicLift_01.SM_Prop_MechanicLift_01",
            "Mechanic vehicle lift",
            CAT_EQUIPMENT,
        ),
        SpawnableObject(
            "car_jack",
            f"{GarageProps}/SM_Prop_CarJack_01_Preset.SM_Prop_CarJack_01_Preset",
            "Car jack",
            CAT_EQUIPMENT,
        ),
        SpawnableObject(
            "car_ramp",
            f"{GarageProps}/SM_Prop_CarRamp_01.SM_Prop_CarRamp_01",
            "Car ramp",
            CAT_EQUIPMENT,
        ),
        SpawnableObject(
            "creeper",
            f"{GarageProps}/SM_Prop_Garage_Creeper_01.SM_Prop_Garage_Creeper_01",
            "Mechanic creeper board",
            CAT_EQUIPMENT,
        ),
        SpawnableObject(
            "workbench",
            f"{GarageProps}/SM_Prop_WorkBench_02_Preset.SM_Prop_WorkBench_02_Preset",
            "Workbench with drawers",
            CAT_EQUIPMENT,
        ),
        SpawnableObject(
            "workbench_small",
            f"{GarageProps}/SM_Prop_WorkBench_01.SM_Prop_WorkBench_01",
            "Small workbench",
            CAT_EQUIPMENT,
        ),
        SpawnableObject(
            "work_table",
            f"{GarageProps}/SM_Prop_WorkTable_01_Preset.SM_Prop_WorkTable_01_Preset",
            "Work table with vice",
            CAT_EQUIPMENT,
        ),
        SpawnableObject(
            "tool_cabinet",
            f"{GarageProps}/SM_Prop_ToolCabinet_01_Preset.SM_Prop_ToolCabinet_01_Preset",
            "Tool cabinet",
            CAT_EQUIPMENT,
        ),
        SpawnableObject(
            "tool_wall",
            f"{GarageProps}/SM_Prop_ToolWall_01.SM_Prop_ToolWall_01",
            "Tool wall panel",
            CAT_EQUIPMENT,
        ),
        SpawnableObject(
            "tool_box",
            f"{OfficeProps}/SM_ToolBox_01.SM_ToolBox_01",
            "Toolbox",
            CAT_EQUIPMENT,
        ),
        SpawnableObject(
            "lift_tool",
            f"{OfficeProps}/SM_LiftTool_01.SM_LiftTool_01",
            "Lift tool",
            CAT_EQUIPMENT,
        ),
        SpawnableObject(
            "tyre_rack",
            f"{GarageProps}/SM_Prop_TyreRack_01.SM_Prop_TyreRack_01",
            "Tyre rack (empty)",
            CAT_EQUIPMENT,
        ),
        SpawnableObject(
            "tyre_rack_full",
            f"{GarageProps}/SM_Prop_TyreRack_Tyres_01.SM_Prop_TyreRack_Tyres_01",
            "Tyre rack with tyres",
            CAT_EQUIPMENT,
        ),
        SpawnableObject(
            "tyre_stack",
            f"{GarageProps}/SM_Prop_TyreStack_01.SM_Prop_TyreStack_01",
            "Tyre stack",
            CAT_EQUIPMENT,
        ),
        SpawnableObject(
            "tyre_single",
            f"{GarageProps}/SM_Prop_TyreSingle_01.SM_Prop_TyreSingle_01",
            "Single tyre",
            CAT_EQUIPMENT,
        ),
        SpawnableObject(
            "parts_washer",
            f"{GarageProps}/SM_Prop_PartsWasher_01.SM_Prop_PartsWasher_01",
            "Parts washer",
            CAT_EQUIPMENT,
        ),
        SpawnableObject(
            "air_compressor",
            f"{GarageProps}/SM_Prop_AirCompressorTank_01.SM_Prop_AirCompressorTank_01",
            "Air compressor tank",
            CAT_EQUIPMENT,
        ),
        # Car parts & fluids
        SpawnableObject(
            "car_battery",
            f"{GarageProps}/SM_Prop_CarBattery_01.SM_Prop_CarBattery_01",
            "Car battery",
            CAT_PARTS,
        ),
        SpawnableObject(
            "radiator",
            f"{GarageProps}/SM_Prop_Radiator_01.SM_Prop_Radiator_01",
            "Radiator",
            CAT_PARTS,
        ),
        SpawnableObject(
            "turbo",
            f"{GarageProps}/SM_Prop_Turbo_01.SM_Prop_Turbo_01",
            "Turbocharger",
            CAT_PARTS,
        ),
        SpawnableObject(
            "piston",
            f"{GarageProps}/SM_Prop_Piston_01.SM_Prop_Piston_01",
            "Piston",
            CAT_PARTS,
        ),
        SpawnableObject(
            "camshaft",
            f"{GarageProps}/SM_Prop_Camshaft_01.SM_Prop_Camshaft_01",
            "Camshaft",
            CAT_PARTS,
        ),
        SpawnableObject(
            "spark_plug",
            f"{GarageProps}/SM_Prop_SparkPlug_01.SM_Prop_SparkPlug_01",
            "Spark plug",
            CAT_PARTS,
        ),
        SpawnableObject(
            "suspension",
            f"{GarageProps}/SM_Prop_Suspension_01.SM_Prop_Suspension_01",
            "Suspension strut",
            CAT_PARTS,
        ),
        SpawnableObject(
            "brake_pad",
            f"{GarageProps}/SM_Prop_BrakePad_01.SM_Prop_BrakePad_01",
            "Brake pad",
            CAT_PARTS,
        ),
        SpawnableObject(
            "air_filter",
            f"{GarageProps}/SM_Prop_AirFilter_01.SM_Prop_AirFilter_01",
            "Air filter",
            CAT_PARTS,
        ),
        SpawnableObject(
            "engine_block",
            f"{GarageProps}/SM_Prop_EngineRusted_01.SM_Prop_EngineRusted_01",
            "Old engine block",
            CAT_PARTS,
        ),
        SpawnableObject(
            "oil_can",
            f"{GarageProps}/SM_Prop_OilCan_01.SM_Prop_OilCan_01",
            "Oil can",
            CAT_PARTS,
        ),
        SpawnableObject(
            "gas_can",
            f"{GarageProps}/SM_Prop_GasCan_01.SM_Prop_GasCan_01",
            "Fuel can",
            CAT_PARTS,
        ),
        SpawnableObject(
            "barrel_pump",
            f"{GarageProps}/SM_Prop_Barrel_Pump_01.SM_Prop_Barrel_Pump_01",
            "Barrel hand pump",
            CAT_PARTS,
        ),
        # Garage structure
        SpawnableObject(
            "garage_shell",
            "/Game/Env/Meshes/Garage/SM_Garage_01.SM_Garage_01",
            "Garage building shell (decor)",
            CAT_STRUCTURE,
        ),
        SpawnableObject(
            "garage_door",
            "/Game/Env/Meshes/Garage/SM_GarageDoor_01.SM_GarageDoor_01",
            "Garage door",
            CAT_STRUCTURE,
        ),
        SpawnableObject(
            "garage_floor",
            "/Game/Env/Meshes/Garage/SM_GarageFloor_01.SM_GarageFloor_01",
            "Garage floor",
            CAT_STRUCTURE,
        ),
        SpawnableObject(
            "garage_wall",
            "/Game/Env/Meshes/Garage/SM_GarageWall_01.SM_GarageWall_01",
            "Garage wall",
            CAT_STRUCTURE,
        ),
        # Shelving
        SpawnableObject(
            "shelves_tall",
            f"{GarageProps}/SM_Prop_Shelves_Tall_01.SM_Prop_Shelves_Tall_01",
            "Tall shelving unit",
            CAT_SHELVING,
        ),
        SpawnableObject(
            "wall_shelves",
            f"{GarageProps}/SM_Prop_WallShelves_01.SM_Prop_WallShelves_01",
            "Wall shelves",
            CAT_SHELVING,
        ),
        # Interactive blueprint actors
        SpawnableObject(
            "fuel_pump",
            "/Game/Objects/Fuel/FuelPump_01A.FuelPump_01A_C",
            "Fuel pump (interactive BP)",
            CAT_INTERACTIVE,
        ),
        SpawnableObject(
            "ev_charger",
            "/Game/Objects/Fuel/EVCharger_02.EVCharger_02_C",
            "EV charger (interactive BP)",
            CAT_INTERACTIVE,
        ),
    )
}
