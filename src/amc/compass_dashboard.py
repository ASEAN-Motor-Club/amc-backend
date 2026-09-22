"""Compass tuning dashboard — embedded in the Django admin site.

Page: GET /admin/amc/compasstuningconfig/dashboard/
API:  GET/POST /admin/amc/compasstuningconfig/dashboard/configs/

Registered via CompassTuningConfigAdmin.get_urls() with
admin_site.admin_view(), so Django-admin session auth + staff gating
apply automatically.

Sliders re-implement the compass law client-side so the graph and matrix
update live while dragging; "Activate" / "Save as new" / "Update active"
POST back to the API. The compass tick reads the row marked active=True
(singleton enforced by a partial unique constraint).
"""

import json
import logging

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render

from amc.models import CompassTuningConfig

log = logging.getLogger(__name__)

_CONFIG_FIELDS = (
    "config_name",
    "c",
    "min_interval",
    "max_interval",
    "ring_distance",
    "budget_cap",
)



def compass_tuning_dashboard(request: HttpRequest) -> HttpResponse:
    return render(request, "amc/compass_tuning_dashboard.html", {})


def _serialize(cfg: CompassTuningConfig) -> dict:
    return {
        "id": cfg.pk,
        "config_name": cfg.config_name,
        "c": cfg.c,
        "min_interval": cfg.min_interval,
        "max_interval": cfg.max_interval,
        "ring_distance": cfg.ring_distance,
        "budget_cap": cfg.budget_cap,
        "active": cfg.active,
    }


def compass_tuning_configs(request: HttpRequest) -> JsonResponse:
    """List / save / create / activate compass tuning configs (JSON)."""
    if request.method == "GET":
        rows = list(CompassTuningConfig.objects.all().order_by("config_name"))
        return JsonResponse({"configs": [_serialize(c) for c in rows]})

    data = json.loads(request.body or "{}")
    action = data.get("action")

    if action == "save":
        try:
            cfg = CompassTuningConfig.objects.get(pk=data["id"])
        except CompassTuningConfig.DoesNotExist:
            return JsonResponse({"error": "config not found"}, status=404)
        try:
            _apply_fields(cfg, data)
        except (ValueError, TypeError, KeyError) as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        cfg.save()
        return JsonResponse({"config": _serialize(cfg)})

    if action == "create":
        cfg = CompassTuningConfig(config_name=data.get("config_name", "?").strip()[:8])
        _apply_fields(cfg, data)
        try:
            cfg.save()
        except Exception:
            log.exception("create compass config failed")
            return JsonResponse({"error": "config_name already exists?"}, status=400)
        return JsonResponse({"config": _serialize(cfg)})

    if action == "activate":
        try:
            cfg = CompassTuningConfig.objects.get(pk=data["id"])
        except CompassTuningConfig.DoesNotExist:
            return JsonResponse({"error": "config not found"}, status=404)
        cfg.active = True
        cfg.save()  # demotes every other active row
        return JsonResponse({"config": _serialize(cfg)})

    return JsonResponse({"error": f"unknown action {action!r}"}, status=400)


def _apply_fields(cfg: CompassTuningConfig, data: dict) -> None:
    for field in _CONFIG_FIELDS:
        if field == "config_name":
            cfg.config_name = str(data.get("config_name", cfg.config_name))[:8]
            continue
        if field in data:
            setattr(cfg, field, data[field])
    if cfg.min_interval <= 0 or cfg.max_interval < cfg.min_interval:
        raise ValueError("max_interval must be >= min_interval > 0")
    if cfg.c <= 0 or cfg.budget_cap < 1:
        raise ValueError("c must be > 0 and budget_cap >= 1")
