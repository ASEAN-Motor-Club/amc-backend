"""Economy contribution weights for the State-of-the-Economy dashboard.

GENERATED from gamedata.db (production_configs / production_inputs /
production_outputs) by a BOM rollup: a cargo's weight = the recipe-propagated
value of the high-value outputs it enables, discounted per production hop and
capped per hop. Params (freeman-approved 2026-09-24):

- hop discount 0.3 (deeper chains are exalted, bounded)
- propagated credit capped at 5x the cargo's own market value per hop
  (keeps the mine/log booster chains from letting fuel dominate the board)
- normalized so GroceryBag = 1.0 (the deliberate floor: pizza/grocery-class
  consumer cargo contributes least to the economy)

Regenerate by re-running the rollup script in the dashboard plan
(.hermes/plans/2026-09-24_economy-dashboard.md) when gamedata.db updates.
Unknown cargo keys score 0 — classify new cargo before adding recipes.
"""

CARGO_WEIGHTS: dict[str, float] = {
    "Terra": 245000,
    "Raven": 50000,
    "FormulaSCM": 50000,
    "Container_40ft_01": 18879,
    "SteelCoil_10t": 13332,
    "Log_20ft": 7635.0,
    "Container_20ft_01": 7338,
    "WoodPlank_14ft_5t": 6684.8,
    "lHBeam_6m": 5653.8,
    "CircuitBoardPallet": 5653.8,
    "Transformer_50MVA": 5000,
    "Tank_250kL": 5000,
    "Log_Oak_12ft": 4010.8,
    "Fuel": 3635.0,
    "IronOre": 2971,
    "PlasticPallete": 2500.0,
    "CopperOre": 2500,
    "Coal": 2167,
    "Sofa_01": 2063,
    "ToyBoxes": 2000,
    "Transformer_5MVA": 2000,
    "TrashBag": 2000,
    "Transformer_20MVA": 2000,
    "BoxPallete_01": 1666.7,
    "SmallBox": 1500.0,
    "QuicklimePallet": 1155,
    "Cement": 1155,
    "BottlePallete": 800,
    "LiveFish_01": 800,
    "CrudeOil": 703,
    "Milk": 600.0,
    "MeatBox": 600,
    "Oil": 500.0,
    "CheesePallet": 500,
    "AirlineMealPallet": 500,
    "SunflowerSeed": 452,
    "GlassBottleBox": 400,
    "Pizza_01": 400,
    "CheeseBox": 400,
    "BreadBox": 400,
    "BreadPallet": 400,
    "Sand": 380,
    "Limestone": 346.5,
    "OrangeBoxes": 300,
    "PumpkinPallet": 300,
    "BeanPallet": 300,
    "ChilliPallet": 300,
    "RicePallet": 300,
    "CornPallet": 300,
    "PotatoPallet": 300,
    "CabbagePallet": 300,
    "LimestoneRock": 248,
    "SnackBox": 200,
    "GroceryBag": 188,
}

SECTORS: dict[str, list[str]] = {
    "energy": ["Fuel", "CrudeOil", "Oil"],
    "mining": ["IronOre", "CopperOre", "Coal", "LimestoneRock", "Limestone", "Sand"],
    "logging": ["Log_20ft", "Log_Oak_12ft", "Log_Oak_24ft", "Log_30ft_30t", "WoodPlank_14ft_5t"],
    "metal": ["SteelCoil_10t", "lHBeam_6m", "CopperRodCoil_2t", "CircuitBoardPallet", "Transformer_50MVA", "Transformer_20MVA", "Transformer_5MVA"],
    "construction": ["Cement", "Concrete", "QuicklimePallet", "PlasticPipes_6m", "Container_20ft_01", "Container_40ft_01", "GlassBottleBox", "BottlePallete"],
    "food": ["Milk", "MeatBox", "CheesePallet", "CheeseBox", "BreadPallet", "BreadBox", "SunflowerSeed", "CornPallet", "RicePallet", "PumpkinPallet", "CabbagePallet", "PotatoPallet", "BeanPallet", "OrangeBoxes", "ChilliPallet"],
    "retail": ["Pizza_01", "Pizza_01_Premium", "Burger_01", "Burger_01_Signature", "GroceryBag", "SnackBox", "Sofa_01", "Sofa_02", "Sofa_03", "Sofa_04", "Bed_01", "Bed_02", "Bed_03", "SmallBox", "BoxPallete_01", "ToyBoxes"],
    "vehicles": ["Terra", "Raven", "FormulaSCM"],
}


def sector_of(cargo_key: str) -> str:
    for sector, keys in SECTORS.items():
        if cargo_key in keys:
            return sector
    return "other"


def weight_of(cargo_key: str) -> float:
    """Contribution weight per unit. Unknown cargo = 0 (surface for classification)."""
    return CARGO_WEIGHTS.get(cargo_key, 0.0)
