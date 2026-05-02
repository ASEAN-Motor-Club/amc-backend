from decimal import Decimal

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0216_taxarea_taxrule"),
    ]

    operations = [
        migrations.CreateModel(
            name="VehiclePropertyTaxConfig",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "active",
                    models.BooleanField(
                        default=False,
                        help_text=(
                            "Master switch. Cron task is a no-op when False."
                        ),
                    ),
                ),
                (
                    "rate_pct",
                    models.DecimalField(
                        decimal_places=6,
                        default=Decimal("0.001"),
                        help_text=(
                            "Daily property tax rate as a fraction of "
                            "FVehicleRow.Cost (e.g. 0.001 = 0.1% of "
                            "purchase price per day)."
                        ),
                        max_digits=8,
                    ),
                ),
                (
                    "flat_fallback",
                    models.PositiveIntegerField(
                        default=5000,
                        help_text=(
                            "Charged per vehicle when the dedimod cannot "
                            "resolve a Cost (unknown class, /vehicle_rows "
                            "endpoint unavailable, etc.)."
                        ),
                    ),
                ),
                (
                    "min_tax_per_vehicle",
                    models.PositiveIntegerField(
                        default=0,
                        help_text=(
                            "Floor applied after rate calculation "
                            "(0 = no floor)."
                        ),
                    ),
                ),
                (
                    "max_tax_per_vehicle",
                    models.PositiveIntegerField(
                        default=0,
                        help_text=(
                            "Ceiling applied after rate calculation "
                            "(0 = no cap)."
                        ),
                    ),
                ),
                (
                    "exempt_balance_threshold",
                    models.PositiveIntegerField(
                        default=0,
                        help_text=(
                            "If > 0, owner accounts with balance below "
                            "this are skipped (prevents driving "
                            "low-balance players negative on idle assets)."
                        ),
                    ),
                ),
                (
                    "frequency_hours",
                    models.PositiveIntegerField(
                        default=24,
                        help_text=(
                            "Minimum hours between successive bills for "
                            "the same vehicle."
                        ),
                    ),
                ),
                (
                    "last_run_at",
                    models.DateTimeField(blank=True, null=True),
                ),
            ],
            options={
                "verbose_name": "Vehicle Property Tax Config",
                "verbose_name_plural": "Vehicle Property Tax Config",
            },
        ),
        migrations.CreateModel(
            name="VehiclePropertyTaxLog",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("amount", models.PositiveIntegerField()),
                (
                    "vehicle_cost",
                    models.PositiveIntegerField(
                        default=0,
                        help_text=(
                            "FVehicleRow.Cost used at billing time "
                            "(0 = fallback used)."
                        ),
                    ),
                ),
                ("used_fallback", models.BooleanField(default=False)),
                (
                    "billed_at",
                    models.DateTimeField(
                        db_index=True,
                        default=django.utils.timezone.now,
                    ),
                ),
                (
                    "billed_character",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="vehicle_property_tax_logs",
                        to="amc.character",
                    ),
                ),
                (
                    "vehicle",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="property_tax_logs",
                        to="amc.charactervehicle",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="vehiclepropertytaxlog",
            index=models.Index(
                fields=["vehicle", "-billed_at"],
                name="amc_vehicle_vehicle_5b5e8f_idx",
            ),
        ),
    ]