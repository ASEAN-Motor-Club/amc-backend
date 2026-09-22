"""Tests for the compass tuning dashboard + multi-config model."""

import json
import sys

import pytest
from django.test import Client

from amc.models import CompassTuningConfig

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _reset_ninja_router_attachments():
    """Detach module-level routers from ninja test-client throwaway APIs.

    Other test modules build ninja TestAsyncClients around the shared
    Router objects (amc.api.routes.*), which sets router.api. The first
    Django URLConf import then fails with ConfigError ("Router has
    already been attached"). Reset attachment state + ninja's API
    registry and drop any cached URLConf modules so each of these tests
    imports amc_backend.urls cleanly.
    """
    from ninja import Router
    from ninja.main import NinjaAPI

    import amc.api as api_pkg
    import importlib
    import pkgutil

    def _detach(router: Router) -> None:
        router.api = None
        for _, sub in getattr(router, "_routers", []):
            _detach(sub)

    # Sweep every route module under amc.api (routes, v1.routes, auction_routes, ...)
    for mod_info in pkgutil.walk_packages(api_pkg.__path__, prefix="amc.api."):
        if mod_info.ispkg:
            continue
        mod = importlib.import_module(mod_info.name)
        for obj in list(vars(mod).values()):
            if isinstance(obj, Router) and obj.api is not None:
                _detach(obj)

    NinjaAPI._registry = [
        ns for ns in NinjaAPI._registry
        if ns not in ("internal", "api-1.0.0")
    ]
    for name in ("amc_backend.urls", "amc_backend.api", "amc_backend.api_v1"):
        sys.modules.pop(name, None)
    yield
    for name in ("amc_backend.urls", "amc_backend.api", "amc_backend.api_v1"):
        sys.modules.pop(name, None)


def _mk(name, **kw):
    defaults = dict(c=3.0e-6, min_interval=3.0, max_interval=15.0,
                    ring_distance=20_000, budget_cap=2)
    defaults.update(kw)
    return CompassTuningConfig.objects.create(config_name=name, **defaults)


class TestCompassTuningDashboard:
    def test_save_active_demotes_other_active_rows(self):
        a = _mk("A", active=True)
        b = _mk("B")
        b.active = True
        b.save()
        a.refresh_from_db()
        assert b.active and not a.active

    def test_unique_active_constraint(self):
        _mk("A", active=True)
        _mk("B", active=False)
        # saving a second active row bypassing save() demotion violates the DB constraint
        import django.db
        with pytest.raises(django.db.IntegrityError):
            CompassTuningConfig.objects.filter(config_name="B").update(active=True)

    @pytest.mark.asyncio
    async def test_aget_active_seeds_defaults(self):
        row = await CompassTuningConfig.aget_active()
        assert row.active
        assert row.c == 3.0e-6 and row.max_interval == 15.0

    @pytest.mark.asyncio
    async def test_tick_reads_active_row(self):
        from amc.models import CompassTuningConfig as C
        await C.objects.acreate(config_name="A", c=3.0e-6, min_interval=3.0,
                                max_interval=15.0, ring_distance=20_000,
                                budget_cap=2, active=True)
        row = await C.aget_active()
        assert row.config_name == "A"

    def test_dashboard_requires_staff(self):
        client = Client()
        resp = client.get("/admin/amc/compasstuningconfig/dashboard/")
        assert resp.status_code in (301, 302)  # redirected to admin login

    def test_configs_list_and_activate(self, admin_user):
        a = _mk("A", active=True)
        b = _mk("B", c=2.0e-6, max_interval=10.0)
        client = Client()
        client.force_login(admin_user)

        resp = client.get("/admin/amc/compasstuningconfig/dashboard/configs/")
        data = resp.json()
        names = {c["config_name"] for c in data["configs"]}
        assert names == {"A", "B"}

        resp = client.post("/admin/amc/compasstuningconfig/dashboard/configs/",
                           data=json.dumps({"action": "activate", "id": b.pk}),
                           content_type="application/json")
        assert resp.status_code == 200
        a.refresh_from_db()
        b.refresh_from_db()
        assert b.active and not a.active

    def test_save_updates_active_row(self, admin_user):
        a = _mk("A", active=True)
        client = Client()
        client.force_login(admin_user)
        resp = client.post("/admin/amc/compasstuningconfig/dashboard/configs/",
                           data=json.dumps({"action": "save", "id": a.pk,
                                            "config_name": "A", "c": 4.0e-6,
                                            "min_interval": 2.0,
                                            "max_interval": 12.0,
                                            "ring_distance": 25000,
                                            "budget_cap": 3}),
                           content_type="application/json")
        assert resp.status_code == 200
        a.refresh_from_db()
        assert a.c == 4.0e-6 and a.min_interval == 2.0 and a.budget_cap == 3

    def test_dashboard_page_renders_for_staff(self, admin_user):
        client = Client()
        client.force_login(admin_user)
        resp = client.get("/admin/amc/compasstuningconfig/dashboard/")
        assert resp.status_code == 200
        assert b"Compass Tuning Dashboard" in resp.content
