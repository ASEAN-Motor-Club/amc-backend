"""Type-able HP class admin field (Yuuka 2026-10-12).

ScheduledEvent.tt_class renders as a TextInput instead of the fixed ladder
dropdown: type an existing class name (or the round-trip rendering
"<name> (<hp>hp>") or a bare multiple-of-10 HP value; unseen values
get-or-create the class carrying that exact max_hp.
"""

import pytest
from django.core.exceptions import ValidationError

from amc.admin import HPClassFormField
from amc.models import TTClass


@pytest.fixture(autouse=True)
def clean_classes(db):
    TTClass.objects.all().delete()


def _mk(name, hp):
    return TTClass.objects.create(name=name, max_hp=hp)


@pytest.mark.django_db
def test_empty_clears_the_pin():
    field = HPClassFormField(required=False)
    assert field.clean("") is None
    assert field.clean(None) is None
    assert field.clean("   ") is None


@pytest.mark.django_db
def test_bare_hp_value_get_or_creates():
    field = HPClassFormField(required=False)
    row = field.clean("240")
    assert row.max_hp == 240 and row.name == "IR-240"
    again = field.clean("240")
    assert again.pk == row.pk  # one value = one rule set


@pytest.mark.django_db
def test_existing_name_and_roundtrip_are_reused():
    field = HPClassFormField(required=False)
    seeded = _mk("TT-270", 270)
    assert field.clean("TT-270").pk == seeded.pk
    assert field.clean("tt-270").pk == seeded.pk  # case-insensitive
    # str(TTClass) = "TT-270 (270hp)" — an untouched form re-saves clean.
    assert field.clean("TT-270 (270hp)").pk == seeded.pk


@pytest.mark.django_db
def test_off_grid_value_rejected():
    field = HPClassFormField(required=False)
    with pytest.raises(ValidationError):
        field.clean("169")
    with pytest.raises(ValidationError):
        field.clean("267")


@pytest.mark.django_db
def test_garbage_rejected():
    field = HPClassFormField(required=False)
    with pytest.raises(ValidationError):
        field.clean("Not A Class")
    with pytest.raises(ValidationError):
        field.clean("-40")


def test_prepare_value_renders_the_name():
    field = HPClassFormField(required=False)
    seeded = _mk("TT-270", 270)
    assert field.prepare_value(seeded) == "TT-270"
    assert field.prepare_value("240") == "240"
    assert field.prepare_value(None) is None
