"""3D house viewer API — the club-side replacement for mt-map-extract's `house-server` POC.

The amc-web `/house/[housingKey]` page talks to three endpoints here (see its
`HANDOFF-house-viewer.md` for the spec, and the POC's `house-server/README.md` for how a model is
assembled):

- `GET /api/houses/{housingKey}` — plot geometry + every building placed on it, each with a
  model URL.
- `GET /api/houses/models/{buildingKey}/{step}.glb` — the building as a GLB (glTF space:
  meters, Y-up, origin = actor root).
- `GET /api/houses/ground/{color|normal}.png` — the Jeju landscape grass the plot sits on.

Data sources:

- Plot geometry (`coord`, `size`, `cost`, `yaw`) comes from `amc/data/house_plots.json`:
  `coord`/`size`/`cost` are the game's Houses table (byte-identical to the `out_house.json`
  amc-web already ships), and `yaw` is the plot's House_C actor rotation, which
  `out_house.json` drops but the viewer needs — plots are rotated, so a 25 m side can run along
  world Y.
- Ownership and placed buildings come from the world save (`amc.save_file.get_world`), the same
  dump `/api/world/` serves. Placement stays save-driven, but the published model set decides
  which rows can be drawn, so a save row with no Buildings-table entry (an event item, say) is
  left out instead of 404ing the viewer's loader.
- Models and ground textures are static per game version. Extracting them from the pak needs
  CUE4Parse, which is C# only, so this module serves the files instead of generating them:
  publish the mt-map-extract house-server's `out/house-server/glb-v<N>/` cache dir into
  `<DATA_PATH>/house-models/` (default `/srv/www/house-models/`). Bump `HOUSE_MODELS_VERSION`
  when the set is regenerated — it is the `?v=` on every model URL, so browsers drop their
  cached copies.
"""

import json
import re
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.http import FileResponse, Http404
from ninja import Router
from ninja.errors import HttpError

from amc.save_file import get_world, get_world_last_modified

house_router = Router()

PLOTS_PATH = Path(__file__).resolve().parent.parent / "data" / "house_plots.json"

# Building keys and ground texture kinds arrive in URLs; only ever open names that pass these.
BUILDING_KEY_RE = re.compile(r"^[A-Za-z0-9_]+$")
GROUND_KINDS = {"color", "normal"}

# The world save is a ~7 MB encrypted JSON; /world/ re-reads it per request, but this endpoint is
# called once per page view, so keep the parsed copy until the save file changes.
_world_cache: tuple[float, dict] | None = None
# Published model names (`<buildingKey>_<step>`), keyed by the models dir's mtime.
_models_cache: tuple[float, set[str]] | None = None


def load_plots() -> dict[str, dict]:
    """The plot table keyed by lowercased housing key (save keys differ in case)."""
    with PLOTS_PATH.open() as f:
        plots = json.load(f)
    return {plot["housingKey"].lower(): plot for plot in plots}


def load_world() -> dict:
    global _world_cache

    modified = get_world_last_modified()
    if _world_cache is not None and _world_cache[0] == modified:
        return _world_cache[1]

    world = get_world()
    _world_cache = (modified, world)
    return world


def published_models() -> set[str] | None:
    """`<buildingKey>_<step>` of every published GLB, or None when nothing is published yet."""
    global _models_cache

    directory = Path(settings.HOUSE_MODELS_PATH)
    try:
        modified = directory.stat().st_mtime
    except OSError:
        return None

    if _models_cache is not None and _models_cache[0] == modified:
        return _models_cache[1]

    models = {path.stem for path in directory.glob("*.glb")}
    if not models:
        return None

    _models_cache = (modified, models)
    return models


def _same(left: object, right: str) -> bool:
    return isinstance(left, str) and left.lower() == right.lower()


def _models_url() -> str:
    return settings.HOUSE_MODELS_URL.rstrip("/")


def _model_url(building_key: str, step: int) -> str:
    version = settings.HOUSE_MODELS_VERSION
    return (
        f"{_models_url()}/models/{quote(building_key, safe='')}/{step}.glb?v={version}"
    )


def _ground_url(kind: str) -> str:
    version = settings.HOUSE_MODELS_VERSION
    return f"{_models_url()}/ground/{kind}.png?v={version}"


def _building(building: dict) -> dict | None:
    building_key = building.get("buildingKey")
    if not building_key:
        return None

    step = building.get("buildingStep") or 0
    return {
        "guid": building.get("guid"),
        "buildingKey": building_key,
        "buildingStep": step,
        "location": building.get("location"),
        "rotation": building.get("rotation"),
        "modelUrl": _model_url(building_key, step),
    }


@house_router.get("/models/{building_key}/{step}.glb")
def get_model(request, building_key: str, step: int):
    if not BUILDING_KEY_RE.match(building_key):
        raise Http404()

    path = Path(settings.HOUSE_MODELS_PATH) / f"{building_key}_{step}.glb"
    if not path.is_file():
        raise Http404()

    response = FileResponse(path.open("rb"), content_type="model/gltf-binary")
    # Models only change with the pak; let the browser keep them (`?v=` busts after a regenerate).
    response["Cache-Control"] = "public, max-age=86400"
    return response


@house_router.get("/ground/{kind}.png")
def get_ground(request, kind: str):
    if kind not in GROUND_KINDS:
        raise Http404()

    path = Path(settings.HOUSE_MODELS_PATH) / f"ground_{kind}.png"
    if not path.is_file():
        raise Http404()

    response = FileResponse(path.open("rb"), content_type="image/png")
    response["Cache-Control"] = "public, max-age=86400"
    return response


@house_router.get("/{housing_key}", response=dict)
def get_house(request, housing_key: str):
    plot = load_plots().get(housing_key.lower())
    if plot is None:
        raise HttpError(404, f"unknown housing key: {housing_key}")

    key = plot["housingKey"]
    world = load_world()
    models = published_models()

    owner_name = None
    for housing in (world.get("housings") or {}).values():
        if _same(housing.get("housingKey"), key):
            owner_name = housing.get("ownerName")
            break

    buildings = []
    for row in world.get("buildings") or []:
        if not _same(row.get("housingKey"), key):
            continue
        building = _building(row)
        if building is None:
            continue
        name = f"{building['buildingKey']}_{building['buildingStep']}"
        if models is not None and name not in models:
            continue
        buildings.append(building)

    return {
        "housingKey": key,
        "coord": plot["coord"],
        "size": plot["size"],
        "yaw": plot["yaw"],
        "cost": plot["cost"],
        "ownerName": owner_name,
        "groundTextures": {
            "color": _ground_url("color"),
            "normal": _ground_url("normal"),
        },
        "buildings": buildings,
    }
