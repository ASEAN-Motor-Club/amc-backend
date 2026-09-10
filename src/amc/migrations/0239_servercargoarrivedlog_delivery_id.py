from django.db import migrations, models


def backfill_delivery_ids(apps, schema_editor):
    """Populate delivery_id from the raw event payload (Net_DeliveryId)."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "UPDATE amc_servercargoarrivedlog SET delivery_id = "
            "(data->>'Net_DeliveryId')::bigint "
            "WHERE delivery_id IS NULL "
            "AND jsonb_typeof(data->'Net_DeliveryId') = 'number'"
        )


class Migration(migrations.Migration):

    dependencies = [
        ("amc", "0238_govcontributionlog"),
    ]

    operations = [
        migrations.AddField(
            model_name="servercargoarrivedlog",
            name="delivery_id",
            field=models.BigIntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(backfill_delivery_ids, migrations.RunPython.noop),
    ]
