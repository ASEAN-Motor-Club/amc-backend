"""Compass tuning dashboard — staff-only interactive tuner.

Page: GET /staff/compass-tuning/  (sliders, realtime graph + interval matrix)
API:  GET/POST /staff/compass-tuning/configs/  (list / save / create / activate)

Sliders re-implement the compass law client-side so the graph and matrix
update live while dragging; "Activate" / "Save as new" / "Update active"
POST back to the API. The compass tick reads the row marked active=True
(singleton enforced by a partial unique constraint).
"""

import json
import logging

from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

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



@staff_member_required
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


@staff_member_required
@require_http_methods(["GET", "POST"])
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
