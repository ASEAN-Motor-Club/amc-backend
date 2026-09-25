# Generated manually — merge the two 0253 leaves:
#   0253_character_cargo_ignore_until (concurrent sibling PR)
#   0253_wanted_initial_heat (wanted stars scale with delivery)

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0253_character_cargo_ignore_until"),
        ("amc", "0253_wanted_initial_heat"),
    ]

    operations = []
