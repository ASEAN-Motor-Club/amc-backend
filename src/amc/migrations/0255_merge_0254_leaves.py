# Generated manually — merge the two 0254 leaves:
#   0254_merge_0253_leaves (merge of the 0253 siblings, #225)
#   0254_wanted_mark (marked_wanted_until field, #223)

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("amc", "0254_merge_0253_leaves"),
        ("amc", "0254_wanted_mark"),
    ]

    operations = []
