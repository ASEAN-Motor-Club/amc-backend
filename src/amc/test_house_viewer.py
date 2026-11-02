"""Tests for the 3D house viewer API (`/api/houses/{housingKey}`).

The endpoint is the backend of amc-web's `/house/[housingKey]` page: plot geometry plus every
building placed on the plot, each with a GLB URL. Fast tests: the world save is patched, so no
database and no save file are needed for the response tests.
"""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings
from ninja.testing import TestClient

from amc.api import house_routes
from amc.api.house_routes import house_router, load_plots


def _world(
    housing_key: str = "FirstHouse",
    owner: str | None = "freeman",
    with_buildings: bool = True,
):
    return {
        "housings": {
            housing_key.lower(): {
                "housingKey": housing_key,
                "ownerUniqueNetId": "76561198378447512",
                "ownerName": owner,
                "rentLeftTimeSeconds": 1000.0,
            }
        }
        if owner
        else {},
        "buildings": []
        if not with_buildings
        else [
            {
                "guid": "AAAA",
                "buildingKey": "SmallHouse_01",
                "housingKey": housing_key,
                "buildingStep": 0,
                "location": {"x": -54095.906, "y": 152545.518, "z": -20989.915},
                "rotation": {"pitch": 0, "yaw": 91.4501953125, "roll": 0},
            },
            {
                "guid": "BBBB",
                "buildingKey": "Furniture_Sofa_01A",
                "housingKey": housing_key,
                "buildingStep": 2,
                "location": {"x": -54100.0, "y": 152500.0, "z": -20990.0},
                "rotation": {"pitch": 0, "yaw": 0.0, "roll": 0},
            },
            # Another plot's building must never leak into this house.
            {
                "guid": "CCCC",
                "buildingKey": "SmallHouse_01",
                "housingKey": "Aewol_01",
                "buildingStep": 0,
                "location": {"x": 0, "y": 0, "z": 0},
                "rotation": {"pitch": 0, "yaw": 0, "roll": 0},
            },
        ],
    }


class HouseViewerAPITest(TestCase):
    def setUp(self):
        self.client = TestClient(house_router)

    @patch("amc.api.house_routes.load_world")
    def test_returns_plot_geometry_and_buildings(self, mock_world):
        mock_world.return_value = _world()

        response = self.client.get("/FirstHouse")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["housingKey"], "FirstHouse")
        self.assertEqual(data["ownerName"], "freeman")
        # Houses-table values, in Unreal world cm.
        self.assertEqual(data["size"], {"x": 2500.0, "y": 2300.0})
        self.assertEqual(data["cost"], 300000)
        self.assertIn("x", data["coord"])
        # Rotated plot: the viewer builds its slab from this yaw.
        self.assertAlmostEqual(data["yaw"], 96.06678009033203)

        self.assertEqual(len(data["buildings"]), 2)
        house, sofa = data["buildings"]
        self.assertEqual(house["buildingKey"], "SmallHouse_01")
        self.assertEqual(house["buildingStep"], 0)
        self.assertEqual(house["rotation"]["yaw"], 91.4501953125)
        self.assertEqual(
            house["modelUrl"], "/api/houses/models/SmallHouse_01/0.glb?v=1"
        )
        # A non-zero building step is part of the model identity.
        self.assertEqual(sofa["buildingStep"], 2)
        self.assertEqual(
            sofa["modelUrl"], "/api/houses/models/Furniture_Sofa_01A/2.glb?v=1"
        )

    @patch("amc.api.house_routes.load_world")
    def test_ground_textures_are_alongside_the_models(self, mock_world):
        mock_world.return_value = _world()

        data = self.client.get("/FirstHouse").json()

        self.assertEqual(
            data["groundTextures"],
            {
                "color": "/api/houses/ground/color.png?v=1",
                "normal": "/api/houses/ground/normal.png?v=1",
            },
        )

    @patch("amc.api.house_routes.load_world")
    def test_housing_key_is_case_insensitive(self, mock_world):
        mock_world.return_value = _world()

        data = self.client.get("/firsthouse").json()

        self.assertEqual(data["housingKey"], "FirstHouse")
        self.assertEqual(len(data["buildings"]), 2)

    @patch("amc.api.house_routes.load_world")
    def test_vacant_plot_has_no_owner_and_no_buildings(self, mock_world):
        mock_world.return_value = _world(
            housing_key="Dongsan_01", owner=None, with_buildings=False
        )

        data = self.client.get("/Dongsan_01").json()

        self.assertEqual(data["housingKey"], "Dongsan_01")
        self.assertIsNone(data["ownerName"])
        self.assertEqual(data["buildings"], [])

    @patch("amc.api.house_routes.load_world")
    def test_unknown_housing_key_is_404(self, mock_world):
        mock_world.return_value = _world()

        response = self.client.get("/NotAPlot")

        self.assertEqual(response.status_code, 404)

    @patch("amc.api.house_routes.load_world")
    def test_building_without_a_key_is_skipped(self, mock_world):
        world = _world()
        world["buildings"].append(
            {"guid": "DDDD", "housingKey": "FirstHouse", "location": {}}
        )
        mock_world.return_value = world

        data = self.client.get("/FirstHouse").json()

        self.assertEqual([b["guid"] for b in data["buildings"]], ["AAAA", "BBBB"])


class HouseViewerAssetsTest(TestCase):
    """`HOUSE_MODELS_PATH` is the published model set: it is both what is served and the filter
    on which save rows can be drawn."""

    def setUp(self):
        self.client = TestClient(house_router)
        self.models_dir = tempfile.mkdtemp(prefix="house-models-")
        self.addCleanup(shutil.rmtree, self.models_dir, True)
        house_routes._models_cache = None
        self.addCleanup(setattr, house_routes, "_models_cache", None)
        override = override_settings(HOUSE_MODELS_PATH=self.models_dir)
        override.enable()
        self.addCleanup(override.disable)

    def _publish(self, name, data=b"glTF"):
        Path(self.models_dir, name).write_bytes(data)

    def test_serves_a_published_model(self):
        self._publish("SmallHouse_01_0.glb", b"glTF-payload")

        response = self.client.get("/models/SmallHouse_01/0.glb")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "model/gltf-binary")
        self.assertEqual(response["Cache-Control"], "public, max-age=86400")
        # Streaming bodies are already consumed by the test client; read the handler's own.
        self.assertEqual(
            b"".join(
                house_routes.get_model(None, "SmallHouse_01", 0).streaming_content
            ),
            b"glTF-payload",
        )

    def test_model_step_is_part_of_the_lookup(self):
        self._publish("SmallHouse_01_0.glb")

        self.assertEqual(
            self.client.get("/models/SmallHouse_01/1.glb").status_code, 404
        )

    def test_unknown_or_unsafe_model_names_are_404(self):
        self._publish("SmallHouse_01_0.glb")

        self.assertEqual(self.client.get("/models/NotARow/0.glb").status_code, 404)
        self.assertEqual(
            self.client.get("/models/..%2F..%2Fsecret/0.glb").status_code, 404
        )

    def test_serves_the_ground_textures(self):
        self._publish("ground_color.png", b"png")

        response = self.client.get("/ground/color.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            b"".join(house_routes.get_ground(None, "color").streaming_content), b"png"
        )
        self.assertEqual(self.client.get("/ground/normal.png").status_code, 404)
        self.assertEqual(self.client.get("/ground/nope.png").status_code, 404)

    @patch("amc.api.house_routes.load_world")
    def test_buildings_without_a_published_model_are_dropped(self, mock_world):
        mock_world.return_value = _world()
        self._publish("Furniture_Sofa_01A_2.glb")

        data = self.client.get("/FirstHouse").json()

        self.assertEqual([b["guid"] for b in data["buildings"]], ["BBBB"])

    @patch("amc.api.house_routes.load_world")
    def test_unpublished_set_keeps_every_save_row(self, mock_world):
        """An empty models dir must not blank out every house (fresh install / dev)."""
        mock_world.return_value = _world()

        data = self.client.get("/FirstHouse").json()

        self.assertEqual(len(data["buildings"]), 2)


class HouseViewerWorldCacheTest(TestCase):
    """The save is a ~7 MB encrypted JSON; parse it once per save revision, not per request."""

    def setUp(self):
        house_routes._world_cache = None
        self.addCleanup(setattr, house_routes, "_world_cache", None)

    @patch("amc.api.house_routes.get_world")
    @patch("amc.api.house_routes.get_world_last_modified")
    def test_reparses_only_when_the_save_changes(self, mock_mtime, mock_get_world):
        mock_get_world.return_value = _world()
        mock_mtime.return_value = 1000.0

        self.assertIs(house_routes.load_world(), house_routes.load_world())
        mock_get_world.assert_called_once()

        mock_mtime.return_value = 2000.0
        house_routes.load_world()
        self.assertEqual(mock_get_world.call_count, 2)


class HousePlotTableTest(TestCase):
    """`data/house_plots.json` is generated from the game data; a bad row breaks the viewer."""

    def test_every_plot_is_complete(self):
        plots = load_plots()

        self.assertGreaterEqual(len(plots), 30)
        for key, plot in plots.items():
            self.assertEqual(plot["housingKey"].lower(), key)
            self.assertIsInstance(plot["yaw"], (int, float))
            self.assertIsInstance(plot["cost"], int)
            self.assertEqual(set(plot["coord"]), {"x", "y", "z"})
            self.assertEqual(set(plot["size"]), {"x", "y"})
            self.assertGreater(plot["size"]["x"], 0)
            self.assertGreater(plot["size"]["y"], 0)
