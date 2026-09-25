"""Cargo-category capacity defaults for economy-dashboard storage math.

The game reports capacity=0/NULL on storage rows it doesn't meter (e.g.
warehouse stock of palletized food). Those cargos still have a real
capacity: the game's per-category default (Pallet = 50). Resolve the
effective capacity as: reported capacity if set, else the cargo's
category default via amc_cargo.type_id, else None (row stays unmetered).
"""

# game default cargo-category capacities (key = amc_cargo.type_id)
CATEGORY_DEFAULT_CAPACITY: dict[str, int] = {
    "T::LargePackage": 50,  # Pallet
}

# cargos whose amc_cargo.type_id is blank but are pallets by name are
# caught by a substring match on "allet" in effective_capacity()

_caps: dict[str, int | None] = {}


async def effective_capacity(cargo_key: str, row_capacity: int | None) -> int | None:
    if row_capacity and row_capacity > 0:
        return row_capacity
    if cargo_key not in _caps:
        from amc.models import Cargo

        type_id = (
            await Cargo.objects.filter(key=cargo_key)
            .values_list("type_id", flat=True)
            .afirst()
        )
        if type_id and type_id in CATEGORY_DEFAULT_CAPACITY:
            _caps[cargo_key] = CATEGORY_DEFAULT_CAPACITY[type_id]
        elif "allet" in cargo_key.lower():  # *Pallet* / *Pallete* box cargo
            _caps[cargo_key] = CATEGORY_DEFAULT_CAPACITY["T::LargePackage"]
        else:
            _caps[cargo_key] = None
    return _caps[cargo_key]
