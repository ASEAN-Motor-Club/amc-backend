from django.db import migrations, models
import django.db.models.deletion


def seed_tt_classes(apps, schema_editor):
    TTClass = apps.get_model("amc", "TTClass")
    for name, max_hp in [
        ("TT-140", 140),
        ("TT-270", 270),
        ("TT-350", 350),
        ("TT-480", 480),
    ]:
        TTClass.objects.get_or_create(name=name, defaults={"max_hp": max_hp})


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0249_compass_tuning_multi_config"),
    ]

    operations = [
        migrations.CreateModel(
            name="TTClass",
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
                ("name", models.CharField(max_length=60, unique=True)),
                ("max_hp", models.PositiveIntegerField()),
            ],
            options={
                "ordering": ["max_hp"],
            },
        ),
        migrations.AddField(
            model_name="scheduledevent",
            name="tt_class",
            field=models.ForeignKey(
                blank=True,
                help_text="Optional pinned TT power class; auto-posted events override per instance",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="scheduled_events",
                to="amc.ttclass",
            ),
        ),
        migrations.AddField(
            model_name="gameevent",
            name="tt_class",
            field=models.ForeignKey(
                blank=True,
                help_text="TT power class for this event instance (parsed from the [TT-…] name tag)",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="game_events",
                to="amc.ttclass",
            ),
        ),
        migrations.RunPython(seed_tt_classes, migrations.RunPython.noop),
    ]
