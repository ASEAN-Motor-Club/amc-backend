from datetime import date, timedelta

from django.db.models import Sum, Count, Q
from django.utils import timezone
from ninja import Router

from amc.models import Delivery
from amc_finance.treasury_summary import (
    get_treasury_summary,
    get_treasury_trend,
    get_snapshot_or_live,
    get_snapshot_archive,
)

treasury_router = Router()


def _decimal_safe(obj: object) -> object:  # pyrefly: ignore
    """Recursively convert Decimal values to float for JSON serialization."""
    from decimal import Decimal

    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _decimal_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_safe(i) for i in obj]
    return obj


@treasury_router.get("/summary/")
def treasury_summary(
    request, days: int = 1, target_date: str | None = None
):  # pyrefly: ignore
    """Daily treasury income/expense summary with category breakdowns.

    Uses persisted snapshots for historical dates, live calculation otherwise.

    Args:
        days: Number of days to aggregate (default: 1 = yesterday only).
        target_date: Optional ISO date string (YYYY-MM-DD) to query.
    """
    parsed_date = date.fromisoformat(target_date) if target_date else None

    if parsed_date and days == 1:
        # For single-day queries, prefer snapshot if available
        data, is_snapshot = get_snapshot_or_live(target_date=parsed_date)
        result = _decimal_safe(data)
        result["is_snapshot"] = is_snapshot
        return result

    data = get_treasury_summary(target_date=parsed_date, days=days)
    result = _decimal_safe(data)
    result["is_snapshot"] = False
    return result


@treasury_router.get("/trend/")
def treasury_trend(request, days: int = 7):
    """Time-series treasury data for charting (daily income, expenses, balances).

    Args:
        days: Number of days of trend data (default: 7).
    """
    data = get_treasury_trend(days=days)
    return _decimal_safe(data)


@treasury_router.get("/archive/")
def treasury_archive(request, limit: int = 30):
    """List of available daily treasury snapshots for archive browsing.

    Args:
        limit: Maximum number of snapshots to return (default: 30).
    """
    return get_snapshot_archive(limit=limit)


@treasury_router.get("/spending/")
def treasury_spending(request, days: int = 7):
    """Detailed spending breakdown: subsidies by cargo, job bonuses, and active rules.

    Args:
        days: Number of days to look back (default: 7).
    """
    from amc.models import DeliveryJob, SubsidyRule

    end_date = timezone.now().date() - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)

    subsidy_by_cargo = list(
        Delivery.objects.filter(
            timestamp__date__range=[start_date, end_date],
        )
        .values("cargo_key")
        .annotate(
            total_subsidy=Sum("subsidy"),
            total_payment=Sum("payment"),
            total_quantity=Sum("quantity"),
            delivery_count=Count("id"),
        )
        .order_by("-total_subsidy")[:25]
    )

    job_bonuses = list(
        DeliveryJob.objects.filter(
            fulfilled_at__date__range=[start_date, end_date],
            completion_bonus__gt=0,
        )
        .values("name", "completion_bonus", "quantity_requested", "quantity_fulfilled", "escrowed_amount")
        .order_by("-completion_bonus")[:20]
    )

    total_bonuses = DeliveryJob.objects.filter(
        fulfilled_at__date__range=[start_date, end_date],
    ).aggregate(
        total_bonus=Sum("completion_bonus"),
        total_escrowed=Sum("escrowed_amount"),
        fulfilled_count=Count("id", filter=Q(fulfilled=True)),
        expired_count=Count("id", filter=Q(expired_at__isnull=False, fulfilled=False)),
    )

    active_rules = list(
        SubsidyRule.objects.filter(active=True)
        .prefetch_related("cargos")
        .order_by("-priority")
    )

    # Compute weekly spend per rule by matching cargo types
    cargo_to_subsidy = {
        row["cargo_key"]: row["total_subsidy"] or 0
        for row in subsidy_by_cargo
    }
    weekly_rule_spends: dict[int, int] = {}
    for rule in active_rules:
        rule_cargo_keys = set(rule.cargos.values_list("key", flat=True))
        if not rule_cargo_keys:
            # Rule applies to ALL cargos — sum all cargo subsidies not claimed by other rules
            weekly_rule_spends[rule.pk] = 0  # skip broad rules for now
        else:
            weekly_rule_spends[rule.pk] = sum(
                cargo_to_subsidy.get(k, 0) for k in rule_cargo_keys
            )

    active_rules_data = []
    for rule in active_rules:
        active_rules_data.append({
            "name": rule.name,
            "reward_type": rule.reward_type,
            "reward_value": float(rule.reward_value),
            "allocation": float(rule.allocation),
            "spent": float(rule.spent),
            "weekly_spent": float(weekly_rule_spends.get(rule.pk, 0)),
            "priority": rule.priority,
        })

    return {
        "period": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "days": days,
        },
        "subsidy_by_cargo": [
            {
                "cargo_key": row["cargo_key"],
                "total_subsidy": row["total_subsidy"] or 0,
                "total_payment": row["total_payment"] or 0,
                "total_quantity": row["total_quantity"] or 0,
                "delivery_count": row["delivery_count"],
                "subsidy_ratio": round(
                    (row["total_subsidy"] or 0) / max(row["total_payment"] or 1, 1), 2
                ),
            }
            for row in subsidy_by_cargo
        ],
        "job_bonuses": [
            {
                "name": row["name"] or "Unnamed",
                "completion_bonus": row["completion_bonus"],
                "quantity_requested": row["quantity_requested"],
                "quantity_fulfilled": row["quantity_fulfilled"],
                "escrowed_amount": row["escrowed_amount"],
            }
            for row in job_bonuses
        ],
        "total_bonuses": {
            "total_bonus": total_bonuses["total_bonus"] or 0,
            "total_escrowed": total_bonuses["total_escrowed"] or 0,
            "fulfilled_count": total_bonuses["fulfilled_count"],
            "expired_count": total_bonuses["expired_count"],
        },
        "active_subsidy_rules": active_rules_data,
    }
