import enum
from django.contrib.gis.db import models


class CargoKey(models.TextChoices):
    SmallBox = "SmallBox", "Small Box"
    CarrotBox = "CarrotBox", "Carrots"
    AppleBox = "AppleBox", "Apples"
    OrangeBox = "OrangeBox", "Oranges"
    GlassBottleBox = "GlassBottleBox", "Glass Bottle"
    BoxPallete_01 = "BoxPallete_01", "Box Pallet A"
    GroceryBox = "GroceryBox", "Grocery Box"
    GroceryBag = "GroceryBag", "Grocery Bag"
    BoxPallete_02 = "BoxPallete_02", "Box Pallet B"
    BoxPallete_03 = "BoxPallete_03", "Box Pallet C"
    PlasticPallete = "PlasticPallete", "Plastic Pallet"
    PowerBox = "PowerBox", "Power Box"
    ToyBoxes = "ToyBoxes", "Toy Pallete"
    BottlePallete = "BottlePallete", "Water Bottle Pallet"
    WoodPlank_14ft_5t = "WoodPlank_14ft_5t", "WoodPlank 14ft 5t"
    Container_30ft_5t = "Container_30ft_5t", "Container 30ft 5t"
    Container_30ft_10t = "Container_30ft_10t", "Container 30ft 10t"
    Container_30ft_20t = "Container_30ft_20t", "Container 30ft 20t"
    Container_20ft_01 = "Container_20ft_01", "Container 20ft"
    Container_40ft_01 = "Container_40ft_01", "Container 40ft"
    Log_30ft_30t = "Log_30ft_30t", "Log 30ft 30t"
    Log_Oak_12ft = "Log_Oak_12ft", "Oak Log 12ft"
    Log_Oak_24ft = "Log_Oak_24ft", "Oak Log 24ft"
    Log_20ft = "Log_20ft", "Log 20ft"
    Sand = "Sand", "Sand"
    FineSand = "FineSand", "Fine Sand"
    Coal = "Coal", "Coal"
    LimestoneRock = "LimestoneRock", "Limestone Rock"
    Limestone = "Limestone", "Limestone"
    QuicklimePallet = "QuicklimePallet", "Quicklime Pallet"
    IronOre = "IronOre", "Iron Ore"
    Concrete = "Concrete", "Concrete"
    Fuel = "Fuel", "Fuel"
    Oil = "Oil", "Oil"
    CrudeOil = "CrudeOil", "Crude Oil"
    LiveFish_01 = "LiveFish_01", "Live Fish"
    TrashBag = "TrashBag", "Trash Bag"
    Trash_Big = "Trash_Big", "Trash Bag"
    Sofa_01 = "Sofa_01", "Sofa A"
    Sofa_02 = "Sofa_02", "Sofa B"
    Sofa_03 = "Sofa_03", "Sofa C"
    Sofa_04 = "Sofa_04", "Sofa D"
    Bed_01 = "Bed_01", "Bed A"
    Bed_02 = "Bed_02", "Bed B"
    Bed_03 = "Bed_03", "Bed C"
    Pizza_01 = "Pizza_01", "Pizza 1 Box"
    Pizza_02 = "Pizza_02", "Pizza 2 Box"
    Pizza_03 = "Pizza_03", "Pizza 3 Box"
    Pizza_04 = "Pizza_04", "Pizza 4 Box"
    Pizza_05 = "Pizza_05", "Pizza 5 Box"
    Pizza_01_Premium = "Pizza_01_Premium", "Premium Pizza 1 Box"
    Burger_01 = "Burger_01", "Burger"
    Burger_01_Signature = "Burger_01_Signature", "Signature Burger"
    MilitarySupplyBox_01_Empty = "MilitarySupplyBox_01_Empty", "Empty Supply Box"
    MilitarySupplyBox_01 = "MilitarySupplyBox_01", "Supply Box"
    Rice = "Rice", "Rice"
    OrangeBoxes = "OrangeBoxes", "Orange Pallet"
    RicePallet = "RicePallet", "Rice Pallet"
    PumpkinBox = "PumpkinBox", "Pumpkin Box"
    PumpkinPallet = "PumpkinPallet", "Pumpkin Pallet"
    CornBox = "CornBox", "Corn Box"
    CornPallet = "CornPallet", "Corn Pallet"
    BeanPallet = "BeanPallet", "Bean Pallet"
    HempPallet = "HempPallet", "Hemp Pallet"
    CabbagePallet = "CabbagePallet", "Cabbage Pallet"
    ChilliPallet = "ChilliPallet", "Chilli Pallet"
    PotatoPallet = "PotatoPallet", "Potato Pallet"
    CheesePallet = "CheesePallet", "Cheese Pallet"
    CheeseBox = "CheeseBox", "Cheese Box"
    Milk = "Milk", "Milk"
    MeatBox = "MeatBox", "Meat Box"
    BreadBox = "BreadBox", "Bread Box"
    BreadPallet = "BreadPallet", "Bread Pallet"
    AirlineMealPallet = "AirlineMealPallet", "Airline Meal Pallet"
    SnackBox = "SnackBox", "Snack Box"
    GiftBox_01 = "GiftBox_01", "Gift Box"
    FormulaSCM = "FormulaSCM", "Formula SCM"
    PlasticPipes_6m = "PlasticPipes_6m", "Plastic Pipes 6m"
    lHBeam_6m = "lHBeam_6m", "H-Beam 6m"
    SteelCoil_10t = "SteelCoil_10t", "Steel Coil"
    Cement = "Cement", "Cement"
    Terra = "Terra", "Terra"
    SunflowerSeed = "SunflowerSeed", "Sunflower Seed"
    Raven = "Raven", "Raven"
    CopperConcentrate = "CopperConcentrate", "Copper Concentrate"
    CopperOre = "CopperOre", "Copper Ore"
    CopperRodCoil_2t = "CopperRodCoil_2t", "Copper Rod Coil 2t"
    Tank_250kL = "Tank_250kL", "Tank 250kL"
    Transformer_5MVA = "Transformer_5MVA", "Transformer 5MVA"
    Transformer_20MVA = "Transformer_20MVA", "Transformer 20MVA"
    Transformer_50MVA = "Transformer_50MVA", "Transformer 50MVA"
    Money = "Money", "Money Stack"
    Ganja = "Ganja", "Ganja Pack"
    CocaLeavesPallet = "CocaLeavesPallet", "Coca Leaves Pallet"
    GanjaPallet = "GanjaPallet", "Ganja Pallet"
    Cocaine = "Cocaine", "Cocaine Pack"
    MoneyPallet = "MoneyPallet", "Money Pallet"
    Moonshine = "Moonshine", "Moonshine Jug"


class VehicleKey(models.TextChoices):
    Hana = "1", "Hana"
    Stinger = "2", "Stinger"
    Maity = "3", "Maity"
    Spider = "4", "Spider"
    Tuscan = "Tuscan", "Tuscan"
    Kart_01 = "Kart_01", "Kart"
    SCM_Kart_One = "SCM_Kart_One", "SCM Kart One"
    Daffy = "Daffy", "Daffy"
    Micky = "Micky", "Micky"
    Stella = "Stella", "Stella"
    Koma = "Koma", "Koma"
    Trophy_Taxi = "Trophy_Taxi", "Trophy Taxi"
    Trophy_Air = "Trophy_Air", "Trophy Air"
    Elisa = "Elisa", "Elisa"
    Taxi_01 = "Taxi_01", "Elisa Taxi"
    Elisa2 = "Elisa2", "Elisa 2"
    Duke = "Duke", "Duke"
    Cervos = "Cervos", "Cervos"
    Nimo = "Nimo", "Nimo"
    Nimo_Taxi = "Nimo_Taxi", "Nimo"
    Stagon = "Stagon", "Stagon"
    Panther = "Panther", "Panther"
    Vista = "Vista", "Vista"
    Zino = "Zino", "Zino"
    Magis = "Magis", "Magis"
    Essam = "Essam", "Essam"
    Cora = "Cora", "Cora"
    EnfoGT = "EnfoGT", "Enfo GT"
    Neo = "Neo", "Neo"
    Fortem = "Fortem", "Fortem"
    Zydro = "Zydro", "Zydro"
    Sports_01 = "Sports_01", "Raton"
    Fox = "Fox", "Fox"
    Mitage = "Mitage", "Mitage"
    Muhan = "Muhan", "Muhan"
    PoliceInterceptor_01 = "PoliceInterceptor_01", "Police Interceptor 1"
    Elisa_Police = "Elisa_Police", "Elisa Police"
    Elisa2_Police = "Elisa2_Police", "Elisa 2 Police"
    Police_01 = "Police_01", "Police"
    Muhan_Police = "Muhan_Police", "Muhan Police"
    Zydro_Police = "Zydro_Police", "Zydro Police"
    Townie = "Townie", "Townie"
    Townie_Bus = "Townie_Bus", "Townie Bus"
    Liliput = "Liliput", "Liliput"
    SchoolBus_01 = "SchoolBus_01", "SV200"
    Bus = "Bus", "Air City"
    Civo = "Civo", "Civo"
    Dumbi = "Dumbi", "Dumbi"
    CheetahMk1 = "CheetahMk1", "Cheetah Mk1"
    Pickup_02 = "Pickup_02", "Ranchy"
    Voltex = "Voltex", "Voltex"
    Mammoth = "Mammoth", "Mammoth"
    Kira_Flatbed = "Kira_Flatbed", "Kira Flatbed"
    Kira_Box = "Kira_Box", "Kira Box"
    Kira_Tanker = "Kira_Tanker", "Kira Tanker"
    Tronko = "Tronko", "Tronko"
    GarbageTruck_01 = "GarbageTruck_01", "Compacty"
    MixerTruck_01 = "MixerTruck_01", "Mixi"
    SRT = "SRT", "SRT"
    SemiTruck_01 = "SemiTruck_01", "FL1"
    Titan = "Titan", "Titan"
    Kuda = "Kuda", "Kuda"
    Campy = "Campy", "Campy"
    FormulaSCM = "FormulaSCM", "Formula SCM"
    Savannah = "Savannah", "Savannah"
    Dory = "Dory", "Dory"
    Jemusi = "Jemusi", "Jemusi"
    Jemusi_Tanker = "Jemusi_Tanker", "Jemusi Tanker"
    Jemusi_Dump = "Jemusi_Dump", "Jemusi Dump"
    Jemusi_Flatbed = "Jemusi_Flatbed", "Jemusi Flatbed"
    Jemusi_Semi = "Jemusi_Semi", "Jemusi Semi"
    Lobo = "Lobo", "Lobo"
    Bora = "Bora", "Bora"
    Dabo = "Dabo", "Dabo"
    DumpTruck_01 = "DumpTruck_01", "Dumpy"
    Atlas_6x2_Garbage = "Atlas_6x2_Garbage", "Atlas 6x2 Garbage"
    Atlas_8x4_Dump = "Atlas_8x4_Dump", "Atlas 8x4 Dump"
    Atlas_6x2_Tanker = "Atlas_6x2_Tanker", "Atlas 6x2 Tanker"
    Atlas_6x2_Dryvan = "Atlas_6x2_Dryvan", "Atlas 6x2 Dry Van"
    Atlas_4x2_Semi = "Atlas_4x2_Semi", "Atlas 4x2 Semi"
    Atlas_6x4_Semi = "Atlas_6x4_Semi", "Atlas 6x4 Semi"
    Atlas_6x2_Semi = "Atlas_6x2_Semi", "Atlas 6x2 Semi"
    Kuda_LiveFishTank_4x2 = "Kuda_LiveFishTank_4x2", "Kuda LFT"
    Kuda_Container_6x2 = "Kuda_Container_6x2", "Kuda Container 6x2"
    Kuda_Dryvan_4x2 = "Kuda_Dryvan_4x2", "Kuda Dry Van"
    Kuda_Flatbed_4x2 = "Kuda_Flatbed_4x2", "Kuda Flatbed"
    Golima_Semi = "Golima_Semi", "Golima Semi"
    Goliath4 = "Goliath4", "Goliath 4x2"
    Goliath6 = "Goliath6", "Goliath 6x4"
    Goliath10 = "Goliath10", "Goliath 10x4"
    Longhorn_Semi = "Longhorn_Semi", "Longhorn Semi"
    Longhorn_Semi_4x2 = "Longhorn_Semi_4x2", "Longhorn Semi 4x2"
    Brutus_Tanker = "Brutus_Tanker", "Brutus Tanker"
    Bongo = "Bongo", "Bongo"
    Bongo_Bus = "Bongo_Bus", "Bongo"
    Roadmaster = "Roadmaster", "Roadmaster"
    Trailer_Flanker3 = "Trailer_Flanker3", "Flanker3"
    Trailer_Flanker3S = "Trailer_Flanker3S", "Flanker3S"
    Trailer_Cotra_20 = "Trailer_Cotra_20", "Cotra 20"
    Trailer_Cotra_20_3 = "Trailer_Cotra_20_3", "Cotra 20-3"
    Trailer_Cotra_20_3L = "Trailer_Cotra_20_3L", "Cotra 20-3L"
    Trailer_Cotra_40 = "Trailer_Cotra_40", "Cotra 40"
    Trailer_Cotra_40_3 = "Trailer_Cotra_40_3", "Cotra 40-3"
    Trailer_Vamos3 = "Trailer_Vamos3", "Vamos3"
    Trailer_Lomax = "Trailer_Lomax", "Lomax"
    Trailer_Carry = "Trailer_Carry", "Carry"
    Trailer_Tanko40 = "Trailer_Tanko40", "Tanko 40"
    Trailer_Eastwood = "Trailer_Eastwood", "Eastwood"
    Trailer_01 = "Trailer_01", "30 Foot Dry Van Trailer"
    Trailer_9m_Flat_01 = "Trailer_9m_Flat_01", "30 Foot Container Trailer"
    Trailer_30ft_Log_01 = "Trailer_30ft_Log_01", "30 Foot Log Trailer"
    Trailer_30ft_Tanker_01 = "Trailer_30ft_Tanker_01", "30Feet Tanker Trailer"
    Trailer_Oldum = "Trailer_Oldum", "Oldum"
    Trailer_Ollok = "Trailer_Ollok", "Ollok"
    Trailer_Olbe = "Trailer_Olbe", "Olbe"
    Trailer_Small_Cage_01 = "Trailer_Small_Cage_01", "Small Cage Trailer"
    Trailer_Middle_Tanker_01 = "Trailer_Middle_Tanker_01", "5t Tanker Trailer"
    Trailer_Dinky_Flatbed = "Trailer_Dinky_Flatbed", "Dinky Flatbed"
    Trailer_Dinky_Dryvan = "Trailer_Dinky_Dryvan", "Dinky Dryvan"
    Trailer_Dinky_Tanker = "Trailer_Dinky_Tanker", "Dinky Tanker"
    Trailer_SPT1 = "Trailer_SPT1", "SPT1"
    Trailer_LoboVan = "Trailer_LoboVan", "LoboVan"
    Trailer_Bulko = "Trailer_Bulko", "Bulko"
    Trailer_Dooly_S1 = "Trailer_Dooly_S1", "Linky S1"
    Trailer_Dooly_D1 = "Trailer_Dooly_D1", "Linky D1"
    Trailer_Dooly_S2 = "Trailer_Dooly_S2", "Linky S2"
    Trailer_Dooly_D2 = "Trailer_Dooly_D2", "Linky D2"
    Trailer_Shovan = "Trailer_Shovan", "Shovan"
    Trailer_Shovan_7 = "Trailer_Shovan_7", "Shovan 7"
    Trailer_Shovan_10 = "Trailer_Shovan_10", "Shovan 10"
    Trailer_Shobed = "Trailer_Shobed", "Shobed"
    Trailer_Shobed_7 = "Trailer_Shobed_7", "Shobed 7"
    Trailer_Shobed_10 = "Trailer_Shobed_10", "Shobed 10"
    Trailer_Shotan = "Trailer_Shotan", "Shotan"
    Trailer_Shotan_7 = "Trailer_Shotan_7", "Shotan 7"
    Trailer_Shotan_10 = "Trailer_Shotan_10", "Shotan 10"
    Trailer_Hobber_Lead = "Trailer_Hobber_Lead", "Hobber Lead"
    Trailer_Hobber_Rear = "Trailer_Hobber_Rear", "Hobber Rear"
    Trailer_Conter_Lead = "Trailer_Conter_Lead", "Conter Lead"
    Trailer_Conter_Rear = "Trailer_Conter_Rear", "Conter Rear"
    Trailer_Conter_Lead_20ft = "Trailer_Conter_Lead_20ft", "Conter Lead 20ft"
    Trailer_Conter_Rear_20ft = "Trailer_Conter_Rear_20ft", "Conter Rear 20ft"
    Trailer_Conter_Lead_40ft = "Trailer_Conter_Lead_40ft", "Conter Lead 40ft"
    Trailer_Conter_Rear_40ft = "Trailer_Conter_Rear_40ft", "Conter Rear 40ft"
    Trailer_Flaber_Lead = "Trailer_Flaber_Lead", "Flaber Lead"
    Trailer_Flaber_Rear = "Trailer_Flaber_Rear", "Flaber Rear"
    Trailer_Taber_Lead = "Trailer_Taber_Lead", "Taber Lead"
    Trailer_Taber_Rear = "Trailer_Taber_Rear", "Taber Rear"
    Raven = "Raven", "Raven"
    EVan = "EVan", "EVan"
    Trophy2 = "Trophy2", "Trophy 2"
    Nuke = "Nuke", "Nuke"
    Nuke_Police = "Nuke_Police", "Nuke Police"
    Nuke_Taxi = "Nuke_Taxi", "Nuke Taxi"
    Pulse = "Pulse", "Pulse"
    Monarch = "Monarch", "Monarch"
    Monarch_Limo = "Monarch_Limo", "Monarch Limo"
    Van_01 = "Van_01", "Vani"
    Boxy = "Boxy", "Boxy"
    Tavan = "Tavan", "Tavan"
    Kira_Van = "Kira_Van", "Kira Van"
    Scooty = "Scooty", "Scooty"
    Gunthoo = "Gunthoo", "Gunthoo"
    Zero = "Zero", "Zero"
    Gunthoo_Police = "Gunthoo_Police", "Gunthoo Police"
    Dory_Wrecker = "Dory_Wrecker", "Dory Wrecker"
    TowTruck_01 = "TowTruck_01", "Pulio"
    Kira_RollbackTow = "Kira_RollbackTow", "Kira Rollback Tow"
    Longhorn_Rollback = "Longhorn_Rollback", "Longhorn Rollback"
    Longhorn_Rollback_UL = "Longhorn_Rollback_UL", "Longhorn Rollback UL"
    Brutus_Wrecker = "Brutus_Wrecker", "Brutus Wrecker"
    GolimaRotator = "GolimaRotator", "Golima Rotator"
    Vulcan = "Vulcan", "Vulcan"
    Terra = "Terra", "Terra"
    Ambi = "Ambi", "Ambi"
    Tavan_Ambulance = "Tavan_Ambulance", "Tavan Ambulance"
    Brutus_Ambulance = "Brutus_Ambulance", "Brutus Ambulance"
    Crany = "Crany", "Crany"
    Brutus_FireEngine = "Brutus_FireEngine", "Brutus Fire Engine"


VehicleKeyByLabel = {label: value for value, label in VehicleKey.choices}

VEHICLE_DATA = {
    "Tuscan": {
        "asset_path": "/Game/Cars/Models/Tuscan/Tuscan.Tuscan_C",
        "object_name": "Tuscan_C",
        "cost": 10000,
    },
    "2": {
        "asset_path": "/Game/Cars/Models/Muscle_01/Stinger.Stinger_C",
        "object_name": "Stinger_C",
        "cost": 10000,
    },
    "Kart_01": {
        "asset_path": "/Game/Cars/Models/Kart_01/Kart_01.Kart_01_C",
        "object_name": "Kart_01_C",
        "cost": 4000,
    },
    "SCM_Kart_One": {
        "asset_path": "/Game/Cars/Models/SCM_Kart_One/SCM_Kart_One.SCM_Kart_One_C",
        "object_name": "SCM_Kart_One_C",
        "cost": 5000,
    },
    "Daffy": {
        "asset_path": "/Game/Cars/Models/Daffy/Daffy.Daffy_C",
        "object_name": "Daffy_C",
        "cost": 8000,
    },
    "Micky": {
        "asset_path": "/Game/Cars/Models/Micky/Micky.Micky_C",
        "object_name": "Micky_C",
        "cost": 9000,
    },
    "Stella": {
        "asset_path": "/Game/Cars/Models/Stella/Stella.Stella_C",
        "object_name": "Stella_C",
        "cost": 10000,
    },
    "Koma": {
        "asset_path": "/Game/Cars/Models/Koma/Koma.Koma_C",
        "object_name": "Koma_C",
        "cost": 8000,
    },
    "Trophy_Taxi": {
        "asset_path": "/Game/Cars/Models/Trophy/Trophy_Taxi.Trophy_Taxi_C",
        "object_name": "Trophy_Taxi_C",
        "cost": 16000,
    },
    "Elisa": {
        "asset_path": "/Game/Cars/Models/Elisa/Elisa.Elisa_C",
        "object_name": "Elisa_C",
        "cost": 20000,
    },
    "Taxi_01": {
        "asset_path": "/Game/Cars/Models/ElisaTaxi/ElisaTaxi.ElisaTaxi_C",
        "object_name": "ElisaTaxi_C",
        "cost": 30000,
    },
    "Elisa2": {
        "asset_path": "/Game/Cars/Models/Elisa2/Elisa2.Elisa2_C",
        "object_name": "Elisa2_C",
        "cost": 20000,
    },
    "Duke": {
        "asset_path": "/Game/Cars/Models/Duke/Duke.Duke_C",
        "object_name": "Duke_C",
        "cost": 25000,
    },
    "Cervos": {
        "asset_path": "/Game/Cars/Models/Cervos/Cervos.Cervos_C",
        "object_name": "Cervos_C",
        "cost": 30000,
    },
    "Nimo": {
        "asset_path": "/Game/Cars/Models/Nimo/Nimo.Nimo_C",
        "object_name": "Nimo_C",
        "cost": 120000,
    },
    "Nimo_Taxi": {
        "asset_path": "/Game/Cars/Models/Nimo/Nimo_Taxi.Nimo_Taxi_C",
        "object_name": "Nimo_Taxi_C",
        "cost": 140000,
    },
    "Stagon": {
        "asset_path": "/Game/Cars/Models/Stagon/Stagon.Stagon_C",
        "object_name": "Stagon_C",
        "cost": 30000,
    },
    "Panther": {
        "asset_path": "/Game/Cars/Models/Panther/Panther.Panther_C",
        "object_name": "Panther_C",
        "cost": 30000,
    },
    "Vista": {
        "asset_path": "/Game/Cars/Models/Vista/Vista.Vista_C",
        "object_name": "Vista_C",
        "cost": 40000,
    },
    "4": {
        "asset_path": "/Game/Cars/Models/Spider/Spider.Spider_C",
        "object_name": "Spider_C",
        "cost": 50000,
    },
    "Zino": {
        "asset_path": "/Game/Cars/Models/Zino/Zino.Zino_C",
        "object_name": "Zino_C",
        "cost": 40000,
    },
    "Magis": {
        "asset_path": "/Game/Cars/Models/Magis/Magis.Magis_C",
        "object_name": "Magis_C",
        "cost": 50000,
    },
    "Essam": {
        "asset_path": "/Game/Cars/Models/Essam/Essam.Essam_C",
        "object_name": "Essam_C",
        "cost": 50000,
    },
    "Cora": {
        "asset_path": "/Game/Cars/Models/Cora/Cora.Cora_C",
        "object_name": "Cora_C",
        "cost": 65000,
    },
    "EnfoGT": {
        "asset_path": "/Game/Cars/Models/EnfoGT/EnfoGT.EnfoGT_C",
        "object_name": "EnfoGT_C",
        "cost": 80000,
    },
    "Neo": {
        "asset_path": "/Game/Cars/Models/Neo/Neo.Neo_C",
        "object_name": "Neo_C",
        "cost": 50000,
    },
    "Fortem": {
        "asset_path": "/Game/Cars/Models/Fortem/Fortem.Fortem_C",
        "object_name": "Fortem_C",
        "cost": 100000,
    },
    "Zydro": {
        "asset_path": "/Game/Cars/Models/Zydro/Zydro.Zydro_C",
        "object_name": "Zydro_C",
        "cost": 110000,
    },
    "Sports_01": {
        "asset_path": "/Game/Cars/Models/Raton/Raton.Raton_C",
        "object_name": "Raton_C",
        "cost": 300000,
    },
    "Fox": {
        "asset_path": "/Game/Cars/Models/Fox/Fox.Fox_C",
        "object_name": "Fox_C",
        "cost": 20000,
    },
    "Mitage": {
        "asset_path": "/Game/Cars/Models/Mitage/Mitage.Mitage_C",
        "object_name": "Mitage_C",
        "cost": 40000,
    },
    "Muhan": {
        "asset_path": "/Game/Cars/Models/Muhan/Muhan.Muhan_C",
        "object_name": "Muhan_C",
        "cost": 80000,
    },
    "PoliceInterceptor_01": {
        "asset_path": "/Game/Cars/Models/PoliceInterceptor1/PoliceInterceptor1.PoliceInterceptor1_C",
        "object_name": "PoliceInterceptor1_C",
        "cost": 30000,
    },
    "Elisa_Police": {
        "asset_path": "/Game/Cars/Models/Elisa/Elisa_Police.Elisa_Police_C",
        "object_name": "Elisa_Police_C",
        "cost": 40000,
    },
    "Elisa2_Police": {
        "asset_path": "/Game/Cars/Models/Elisa2/Elisa2_Police.Elisa2_Police_C",
        "object_name": "Elisa2_Police_C",
        "cost": 50000,
    },
    "Police_01": {
        "asset_path": "/Game/Cars/Models/Police/Police.Police_C",
        "object_name": "Police_C",
        "cost": 80000,
    },
    "Muhan_Police": {
        "asset_path": "/Game/Cars/Models/Muhan/Muhan_Police.Muhan_Police_C",
        "object_name": "Muhan_Police_C",
        "cost": 100000,
    },
    "Zydro_Police": {
        "asset_path": "/Game/Cars/Models/Zydro/Zydro_Police.Zydro_Police_C",
        "object_name": "Zydro_Police_C",
        "cost": 1200000,
    },
    "Townie": {
        "asset_path": "/Game/Cars/Models/Townie/Townie.Townie_C",
        "object_name": "Townie_C",
        "cost": 40000,
    },
    "Townie_Bus": {
        "asset_path": "/Game/Cars/Models/Townie/Townie_Bus.Townie_Bus_C",
        "object_name": "Townie_Bus_C",
        "cost": 40000,
    },
    "Liliput": {
        "asset_path": "/Game/Cars/Models/Liliput/Liliput.Liliput_C",
        "object_name": "Liliput_C",
        "cost": 60000,
    },
    "SchoolBus_01": {
        "asset_path": "/Game/Cars/Models/SV200/SV200.SV200_C",
        "object_name": "SV200_C",
        "cost": 100000,
    },
    "Bus": {
        "asset_path": "/Game/Cars/Models/Bus/AirCity.AirCity_C",
        "object_name": "AirCity_C",
        "cost": 120000,
    },
    "Civo": {
        "asset_path": "/Game/Cars/Models/Civo/Civo.Civo_C",
        "object_name": "Civo_C",
        "cost": 130000,
    },
    "Dumbi": {
        "asset_path": "/Game/Cars/Models/Dumbi/Dumbi.Dumbi_C",
        "object_name": "Dumbi_C",
        "cost": 130000,
    },
    "CheetahMk1": {
        "asset_path": "/Game/Cars/Models/CheetahMk1/CheetahMk1.CheetahMk1_C",
        "object_name": "CheetahMk1_C",
        "cost": 200000,
    },
    "1": {
        "asset_path": "/Game/Cars/Models/Hana/Hana.Hana_C",
        "object_name": "Hana_C",
        "cost": 20000,
    },
    "Pickup_02": {
        "asset_path": "/Game/Cars/Models/Pickup_02/Ranch.Ranch_C",
        "object_name": "Ranch_C",
        "cost": 20000,
    },
    "Voltex": {
        "asset_path": "/Game/Cars/Models/Voltex/Voltex.Voltex_C",
        "object_name": "Voltex_C",
        "cost": 40000,
    },
    "Mammoth": {
        "asset_path": "/Game/Cars/Models/Mammoth/Mammoth.Mammoth_C",
        "object_name": "Mammoth_C",
        "cost": 120000,
    },
    "Kira_Flatbed": {
        "asset_path": "/Game/Cars/Models/Kira/Kira_Flatbed.Kira_Flatbed_C",
        "object_name": "Kira_Flatbed_C",
        "cost": 40000,
    },
    "Kira_Box": {
        "asset_path": "/Game/Cars/Models/Kira/Kira_Box.Kira_Box_C",
        "object_name": "Kira_Box_C",
        "cost": 42000,
    },
    "Kira_Tanker": {
        "asset_path": "/Game/Cars/Models/Kira/Kira_Tanker.Kira_Tanker_C",
        "object_name": "Kira_Tanker_C",
        "cost": 60000,
    },
    "3": {
        "asset_path": "/Game/Cars/Models/Maity/Maity.Maity_C",
        "object_name": "Maity_C",
        "cost": 70000,
    },
    "Tronko": {
        "asset_path": "/Game/Cars/Models/Trunko/Tronko.Tronko_C",
        "object_name": "Tronko_C",
        "cost": 100000,
    },
    "GarbageTruck_01": {
        "asset_path": "/Game/Cars/Models/Maity/Compacty.Compacty_C",
        "object_name": "Compacty_C",
        "cost": 150000,
    },
    "MixerTruck_01": {
        "asset_path": "/Game/Cars/Models/Maity/Mixi.Mixi_C",
        "object_name": "Mixi_C",
        "cost": 150000,
    },
    "SRT": {
        "asset_path": "/Game/Cars/Models/SRT/SRT.SRT_C",
        "object_name": "SRT_C",
        "cost": 120000,
    },
    "SemiTruck_01": {
        "asset_path": "/Game/Cars/Models/FL1/FL1.FL1_C",
        "object_name": "FL1_C",
        "cost": 160000,
    },
    "Titan": {
        "asset_path": "/Game/Cars/Models/Titan/Titan.Titan_C",
        "object_name": "Titan_C",
        "cost": 180000,
    },
    "Kuda": {
        "asset_path": "/Game/Cars/Models/Kuda/Kuda_Semi.Kuda_Semi_C",
        "object_name": "Kuda_Semi_C",
        "cost": 220000,
    },
    "Campy": {
        "asset_path": "/Game/Cars/Models/Campy/Campy.Campy_C",
        "object_name": "Campy_C",
        "cost": 150000,
    },
    "FormulaSCM": {
        "asset_path": "/Game/Cars/Models/FormulaSCM/FormulaSCM.FormulaSCM_C",
        "object_name": "FormulaSCM_C",
        "cost": 100000,
    },
    "Trophy_Air": {
        "asset_path": "/Game/Cars/Models/Trophy/Trophy_Air.Trophy_Air_C",
        "object_name": "Trophy_Air_C",
        "cost": 16000,
    },
    "Savannah": {
        "asset_path": "/Game/Cars/Models/Savannah/Savannah.Savannah_C",
        "object_name": "Savannah_C",
        "cost": 20000,
    },
    "Dory": {
        "asset_path": "/Game/Cars/Models/Dory/Dory.Dory_C",
        "object_name": "Dory_C",
        "cost": 11000,
    },
    "Jemusi": {
        "asset_path": "/Game/Cars/Models/Jemusi/Jemusi.Jemusi_C",
        "object_name": "Jemusi_C",
        "cost": 80000,
    },
    "Jemusi_Tanker": {
        "asset_path": "/Game/Cars/Models/Jemusi/Jemusi_Tanker.Jemusi_Tanker_C",
        "object_name": "Jemusi_Tanker_C",
        "cost": 110000,
    },
    "Jemusi_Dump": {
        "asset_path": "/Game/Cars/Models/Jemusi/Jemusi_Dump.Jemusi_Dump_C",
        "object_name": "Jemusi_Dump_C",
        "cost": 100000,
    },
    "Jemusi_Flatbed": {
        "asset_path": "/Game/Cars/Models/Jemusi/Jemusi_Flatbed.Jemusi_Flatbed_C",
        "object_name": "Jemusi_Flatbed_C",
        "cost": 80000,
    },
    "Jemusi_Semi": {
        "asset_path": "/Game/Cars/Models/Jemusi/Jemusi_Semi.Jemusi_Semi_C",
        "object_name": "Jemusi_Semi_C",
        "cost": 80000,
    },
    "Lobo": {
        "asset_path": "/Game/Cars/Models/Lobo/Lobo.Lobo_C",
        "object_name": "Lobo_C",
        "cost": 200000,
    },
    "Bora": {
        "asset_path": "/Game/Cars/Models/Bora/Bora.Bora_C",
        "object_name": "Bora_C",
        "cost": 10000,
    },
    "Dabo": {
        "asset_path": "/Game/Cars/Models/Dabo/Dabo.Dabo_C",
        "object_name": "Dabo_C",
        "cost": 8000,
    },
    "DumpTruck_01": {
        "asset_path": "/Game/Cars/Models/Maity/Dumpy.Dumpy_C",
        "object_name": "Dumpy_C",
        "cost": 150000,
    },
    "Atlas_6x2_Garbage": {
        "asset_path": "/Game/Cars/Models/Atlas/Atlas_6x2_Garbage.Atlas_6x2_Garbage_C",
        "object_name": "Atlas_6x2_Garbage_C",
        "cost": 280000,
    },
    "Atlas_8x4_Dump": {
        "asset_path": "/Game/Cars/Models/Atlas/Atlas_8x4_Dump.Atlas_8x4_Dump_C",
        "object_name": "Atlas_8x4_Dump_C",
        "cost": 210000,
    },
    "Atlas_6x2_Tanker": {
        "asset_path": "/Game/Cars/Models/Atlas/Atlas_6x2_Tanker.Atlas_6x2_Tanker_C",
        "object_name": "Atlas_6x2_Tanker_C",
        "cost": 160000,
    },
    "Atlas_6x2_Dryvan": {
        "asset_path": "/Game/Cars/Models/Atlas/Atlas_6x2_Dryvan.Atlas_6x2_Dryvan_C",
        "object_name": "Atlas_6x2_Dryvan_C",
        "cost": 150000,
    },
    "Atlas_4x2_Semi": {
        "asset_path": "/Game/Cars/Models/Atlas/Atlas_4x2_Semi.Atlas_4x2_Semi_C",
        "object_name": "Atlas_4x2_Semi_C",
        "cost": 130000,
    },
    "Atlas_6x4_Semi": {
        "asset_path": "/Game/Cars/Models/Atlas/Atlas_6x4_Semi.Atlas_6x4_Semi_C",
        "object_name": "Atlas_6x4_Semi_C",
        "cost": 170000,
    },
    "Atlas_6x2_Semi": {
        "asset_path": "/Game/Cars/Models/Atlas/Atlas_6x2_Semi.Atlas_6x2_Semi_C",
        "object_name": "Atlas_6x2_Semi_C",
        "cost": 200000,
    },
    "Kuda_LiveFishTank_4x2": {
        "asset_path": "/Game/Cars/Models/Kuda/Kuda_LiveFishTank_4x2.Kuda_LiveFishTank_4x2_C",
        "object_name": "Kuda_LiveFishTank_4x2_C",
        "cost": 170000,
    },
    "Kuda_Container_6x2": {
        "asset_path": "/Game/Cars/Models/Kuda/Kuda_Container_6x2.Kuda_Container_6x2_C",
        "object_name": "Kuda_Container_6x2_C",
        "cost": 130000,
    },
    "Kuda_Dryvan_4x2": {
        "asset_path": "/Game/Cars/Models/Kuda/Kuda_Dyrvan_4x2.Kuda_Dyrvan_4x2_C",
        "object_name": "Kuda_Dyrvan_4x2_C",
        "cost": 140000,
    },
    "Kuda_Flatbed_4x2": {
        "asset_path": "/Game/Cars/Models/Kuda/Kuda_Flatbed_4x2.Kuda_Flatbed_4x2_C",
        "object_name": "Kuda_Flatbed_4x2_C",
        "cost": 130000,
    },
    "Golima_Semi": {
        "asset_path": "/Game/Cars/Models/Golima/Golima_Semi.Golima_Semi_C",
        "object_name": "Golima_Semi_C",
        "cost": 300000,
    },
    "Goliath4": {
        "asset_path": "/Game/Cars/Models/Goliath/Goliath4.Goliath4_C",
        "object_name": "Goliath4_C",
        "cost": 300000,
    },
    "Goliath6": {
        "asset_path": "/Game/Cars/Models/Goliath/Goliath6.Goliath6_C",
        "object_name": "Goliath6_C",
        "cost": 500000,
    },
    "Goliath10": {
        "asset_path": "/Game/Cars/Models/Goliath/Goliath10.Goliath10_C",
        "object_name": "Goliath10_C",
        "cost": 800000,
    },
    "Longhorn_Semi": {
        "asset_path": "/Game/Cars/Models/Longhorn/Longhorn_Semi.Longhorn_Semi_C",
        "object_name": "Longhorn_Semi_C",
        "cost": 400000,
    },
    "Longhorn_Semi_4x2": {
        "asset_path": "/Game/Cars/Models/Longhorn_Semi_4x2/Longhorn_Semi_4x2.Longhorn_Semi_4x2_C",
        "object_name": "Longhorn_Semi_4x2_C",
        "cost": 240000,
    },
    "Brutus_Tanker": {
        "asset_path": "/Game/Cars/Models/Brutus/Brutus_Tanker.Brutus_Tanker_C",
        "object_name": "Brutus_Tanker_C",
        "cost": 150000,
    },
    "Bongo": {
        "asset_path": "/Game/Cars/Models/Bongo/Bongo.Bongo_C",
        "object_name": "Bongo_C",
        "cost": 30000,
    },
    "Bongo_Bus": {
        "asset_path": "/Game/Cars/Models/Bongo/Bongo_Bus.Bongo_Bus_C",
        "object_name": "Bongo_Bus_C",
        "cost": 30000,
    },
    "Roadmaster": {
        "asset_path": "/Game/Cars/Models/Roadmaster/Roadmaster.Roadmaster_C",
        "object_name": "Roadmaster_C",
        "cost": 160000,
    },
    "Trailer_Flanker3": {
        "asset_path": "/Game/Cars/Models/Trailer_Flanker/Flanker3.Flanker3_C",
        "object_name": "Flanker3_C",
        "cost": 80000,
    },
    "Trailer_Flanker3S": {
        "asset_path": "/Game/Cars/Models/Trailer_Flanker/Flanker3S.Flanker3S_C",
        "object_name": "Flanker3S_C",
        "cost": 90000,
    },
    "Trailer_Cotra_20_3": {
        "asset_path": "/Game/Cars/Models/Trailer_Cotra/Cotra_20_3S.Cotra_20_3S_C",
        "object_name": "Cotra_20_3S_C",
        "cost": 27000,
    },
    "Trailer_Cotra_20_3L": {
        "asset_path": "/Game/Cars/Models/Trailer_Cotra/Cotra_20_3L.Cotra_20_3L_C",
        "object_name": "Cotra_20_3L_C",
        "cost": 30000,
    },
    "Trailer_Cotra_40_3": {
        "asset_path": "/Game/Cars/Models/Trailer_Cotra/Cotra_40_3.Cotra_40_3_C",
        "object_name": "Cotra_40_3_C",
        "cost": 50000,
    },
    "Trailer_Vamos3": {
        "asset_path": "/Game/Cars/Models/Trailer_Vamos/Vamos3.Vamos3_C",
        "object_name": "Vamos3_C",
        "cost": 100000,
    },
    "Trailer_Lomax": {
        "asset_path": "/Game/Cars/Models/Trailer_Lomax/Lomax.Lomax_C",
        "object_name": "Lomax_C",
        "cost": 190000,
    },
    "Trailer_Carry": {
        "asset_path": "/Game/Cars/Models/Carry/Carry.Carry_C",
        "object_name": "Carry_C",
        "cost": 270000,
    },
    "Trailer_Tanko40": {
        "asset_path": "/Game/Cars/Models/Trailer_Tanko/Tanko40.Tanko40_C",
        "object_name": "Tanko40_C",
        "cost": 250000,
    },
    "Trailer_Eastwood": {
        "asset_path": "/Game/Cars/Models/Trailer_EastWood/Eastwood.Eastwood_C",
        "object_name": "Eastwood_C",
        "cost": 90000,
    },
    "Trailer_01": {
        "asset_path": "/Game/Cars/Models/Trailer_01/Trailer_01.Trailer_01_C",
        "object_name": "Trailer_01_C",
        "cost": 100000,
    },
    "Trailer_9m_Flat_01": {
        "asset_path": "/Game/Cars/Models/Trailer_9m_Flat_01/Trailer_9m_Flat_01.Trailer_9m_Flat_01_C",
        "object_name": "Trailer_9m_Flat_01_C",
        "cost": 50000,
    },
    "Trailer_30ft_Log_01": {
        "asset_path": "/Game/Cars/Models/Trailer_9m_Flat_01/Trailer_9m_Log_01.Trailer_9m_Log_01_C",
        "object_name": "Trailer_9m_Log_01_C",
        "cost": 70000,
    },
    "Trailer_30ft_Tanker_01": {
        "asset_path": "/Game/Cars/Models/Trailer_30ft_Tanker_01/Trailer_30ft_Tanker_01.Trailer_30ft_Tanker_01_C",
        "object_name": "Trailer_30ft_Tanker_01_C",
        "cost": 150000,
    },
    "Trailer_Oldum": {
        "asset_path": "/Game/Cars/Models/Oldum/Oldum.Oldum_C",
        "object_name": "Oldum_C",
        "cost": 120000,
    },
    "Trailer_Ollok": {
        "asset_path": "/Game/Cars/Models/Ollok/Ollok.Ollok_C",
        "object_name": "Ollok_C",
        "cost": 50000,
    },
    "Trailer_Olbe": {
        "asset_path": "/Game/Cars/Models/Olbe/Olbe.Olbe_C",
        "object_name": "Olbe_C",
        "cost": 60000,
    },
    "Trailer_SPT1": {
        "asset_path": "/Game/Cars/Models/Trailer_SPT1/Trailer_SPT1.Trailer_SPT1_C",
        "object_name": "Trailer_SPT1_C",
        "cost": 8000,
    },
    "Trailer_LoboVan": {
        "asset_path": "/Game/Cars/Models/LoboVan/LoboVan.LoboVan_C",
        "object_name": "LoboVan_C",
        "cost": 140000,
    },
    "Trailer_Bulko": {
        "asset_path": "/Game/Cars/Models/Bulko/Bulko.Bulko_C",
        "object_name": "Bulko_C",
        "cost": 130000,
    },
    "Trailer_Dooly_S1": {
        "asset_path": "/Game/Cars/Models/Dooly/Dooly_S1.Dooly_S1_C",
        "object_name": "Dooly_S1_C",
        "cost": 10000,
    },
    "Trailer_Dooly_D1": {
        "asset_path": "/Game/Cars/Models/Dooly/Dooly_D1.Dooly_D1_C",
        "object_name": "Dooly_D1_C",
        "cost": 13000,
    },
    "Trailer_Dooly_S2": {
        "asset_path": "/Game/Cars/Models/Dooly/Dooly_S2.Dooly_S2_C",
        "object_name": "Dooly_S2_C",
        "cost": 17000,
    },
    "Trailer_Dooly_D2": {
        "asset_path": "/Game/Cars/Models/Dooly/Dooly_D2.Dooly_D2_C",
        "object_name": "Dooly_D2_C",
        "cost": 20000,
    },
    "Trailer_Shovan_7": {
        "asset_path": "/Game/Cars/Models/Shovan/Shovan_7.Shovan_7_C",
        "object_name": "Shovan_7_C",
        "cost": 50000,
    },
    "Trailer_Shovan_10": {
        "asset_path": "/Game/Cars/Models/Shovan/Shovan_10.Shovan_10_C",
        "object_name": "Shovan_10_C",
        "cost": 70000,
    },
    "Trailer_Shobed_7": {
        "asset_path": "/Game/Cars/Models/Shobed/Shobed_7.Shobed_7_C",
        "object_name": "Shobed_7_C",
        "cost": 40000,
    },
    "Trailer_Shobed_10": {
        "asset_path": "/Game/Cars/Models/Shobed/Shobed_10.Shobed_10_C",
        "object_name": "Shobed_10_C",
        "cost": 60000,
    },
    "Trailer_Shotan_7": {
        "asset_path": "/Game/Cars/Models/Shotan/Shotan_7.Shotan_7_C",
        "object_name": "Shotan_7_C",
        "cost": 110000,
    },
    "Trailer_Shotan_10": {
        "asset_path": "/Game/Cars/Models/Shotan/Shotan_10.Shotan_10_C",
        "object_name": "Shotan_10_C",
        "cost": 160000,
    },
    "Trailer_Hobber_Lead": {
        "asset_path": "/Game/Cars/Models/Hobber/Hobber_Lead.Hobber_Lead_C",
        "object_name": "Hobber_Lead_C",
        "cost": 150000,
    },
    "Trailer_Hobber_Rear": {
        "asset_path": "/Game/Cars/Models/Hobber/Hobber_Rear.Hobber_Rear_C",
        "object_name": "Hobber_Rear_C",
        "cost": 120000,
    },
    "Trailer_Flaber_Lead": {
        "asset_path": "/Game/Cars/Models/Flaber/Flaber_Lead.Flaber_Lead_C",
        "object_name": "Flaber_Lead_C",
        "cost": 120000,
    },
    "Trailer_Flaber_Rear": {
        "asset_path": "/Game/Cars/Models/Flaber/Flaber_Rear.Flaber_Rear_C",
        "object_name": "Flaber_Rear_C",
        "cost": 60000,
    },
    "Trailer_Taber_Lead": {
        "asset_path": "/Game/Cars/Models/Taber/Taber_Lead.Taber_Lead_C",
        "object_name": "Taber_Lead_C",
        "cost": 180000,
    },
    "Trailer_Taber_Rear": {
        "asset_path": "/Game/Cars/Models/Taber/Taber_Rear.Taber_Rear_C",
        "object_name": "Taber_Rear_C",
        "cost": 120000,
    },
    "Trailer_Conter_Lead_20ft": {
        "asset_path": "/Game/Cars/Models/Conter/Conter_Lead_20ft.Conter_Lead_20ft_C",
        "object_name": "Conter_Lead_20ft_C",
        "cost": 100000,
    },
    "Trailer_Conter_Rear_20ft": {
        "asset_path": "/Game/Cars/Models/Conter/Conter_Rear_20ft.Conter_Rear_20ft_C",
        "object_name": "Conter_Rear_20ft_C",
        "cost": 25000,
    },
    "Trailer_Conter_Lead_40ft": {
        "asset_path": "/Game/Cars/Models/Conter/Conter_Lead_40ft.Conter_Lead_40ft_C",
        "object_name": "Conter_Lead_40ft_C",
        "cost": 150000,
    },
    "Trailer_Conter_Rear_40ft": {
        "asset_path": "/Game/Cars/Models/Conter/Conter_Rear_40ft.Conter_Rear_40ft_C",
        "object_name": "Conter_Rear_40ft_C",
        "cost": 60000,
    },
    "Trailer_Small_Cage_01": {
        "asset_path": "/Game/Cars/Models/Trailer_Small_Cage/Trailer_Small_Cage_01.Trailer_Small_Cage_01_C",
        "object_name": "Trailer_Small_Cage_01_C",
        "cost": 3000,
    },
    "Trailer_Middle_Tanker_01": {
        "asset_path": "/Game/Cars/Models/Trailer_Middle_Tanker/Trailer_Middle_Tanker_01.Trailer_Middle_Tanker_01_C",
        "object_name": "Trailer_Middle_Tanker_01_C",
        "cost": 10000,
    },
    "Trailer_Dinky_Flatbed": {
        "asset_path": "/Game/Cars/Models/Dinky/Dinky_Flatbed.Dinky_Flatbed_C",
        "object_name": "Dinky_Flatbed_C",
        "cost": 1500,
    },
    "Trailer_Dinky_Dryvan": {
        "asset_path": "/Game/Cars/Models/Dinky/Dinky_Dryvan.Dinky_Dryvan_C",
        "object_name": "Dinky_Dryvan_C",
        "cost": 3000,
    },
    "Trailer_Dinky_Tanker": {
        "asset_path": "/Game/Cars/Models/Dinky/Dinky_Tanker.Dinky_Tanker_C",
        "object_name": "Dinky_Tanker_C",
        "cost": 15000,
    },
    "Nuke": {
        "asset_path": "/Game/Cars/Models/Nuke/Nuke.Nuke_C",
        "object_name": "Nuke_C",
        "cost": 28000,
    },
    "Nuke_Police": {
        "asset_path": "/Game/Cars/Models/Nuke/NukePolice.NukePolice_C",
        "object_name": "NukePolice_C",
        "cost": 28000,
    },
    "Nuke_Taxi": {
        "asset_path": "/Game/Cars/Models/Nuke/NukeTaxi.NukeTaxi_C",
        "object_name": "NukeTaxi_C",
        "cost": 38000,
    },
    "Pulse": {
        "asset_path": "/Game/Cars/Models/Pulse/Pulse.Pulse_C",
        "object_name": "Pulse_C",
        "cost": 40000,
    },
    "Monarch": {
        "asset_path": "/Game/Cars/Models/Monarch/Monarch.Monarch_C",
        "object_name": "Monarch_C",
        "cost": 50000,
    },
    "Monarch_Limo": {
        "asset_path": "/Game/Cars/Models/Monarch/Monarch_Limo.Monarch_Limo_C",
        "object_name": "Monarch_Limo_C",
        "cost": 150000,
    },
    "Van_01": {
        "asset_path": "/Game/Cars/Models/Vani/Vani.Vani_C",
        "object_name": "Vani_C",
        "cost": 20000,
    },
    "Boxy": {
        "asset_path": "/Game/Cars/Models/Boxy/Boxy.Boxy_C",
        "object_name": "Boxy_C",
        "cost": 35000,
    },
    "Tavan": {
        "asset_path": "/Game/Cars/Models/Tavan/Tavan.Tavan_C",
        "object_name": "Tavan_C",
        "cost": 40000,
    },
    "Kira_Van": {
        "asset_path": "/Game/Cars/Models/Kira/Kira_Van.Kira_Van_C",
        "object_name": "Kira_Van_C",
        "cost": 45000,
    },
    "Scooty": {
        "asset_path": "/Game/Cars/Models/Bike/Scooty/Scooty.Scooty_C",
        "object_name": "Scooty_C",
        "cost": 2000,
    },
    "Gunthoo": {
        "asset_path": "/Game/Cars/Models/Bike/Gunthoo/Gunthoo.Gunthoo_C",
        "object_name": "Gunthoo_C",
        "cost": 6000,
    },
    "Zero": {
        "asset_path": "/Game/Cars/Models/Bike/Zero/Zero.Zero_C",
        "object_name": "Zero_C",
        "cost": 25000,
    },
    "Gunthoo_Police": {
        "asset_path": "/Game/Cars/Models/Bike/Gunthoo/Gunthoo_Police.Gunthoo_Police_C",
        "object_name": "Gunthoo_Police_C",
        "cost": 6000,
    },
    "Dory_Wrecker": {
        "asset_path": "/Game/Cars/Models/Dory/DoryWrecker.DoryWrecker_C",
        "object_name": "DoryWrecker_C",
        "cost": 30000,
    },
    "TowTruck_01": {
        "asset_path": "/Game/Cars/Models/Pulio/Pulio.Pulio_C",
        "object_name": "Pulio_C",
        "cost": 90000,
    },
    "Kira_RollbackTow": {
        "asset_path": "/Game/Cars/Models/Kira/Kira_Rollback.Kira_Rollback_C",
        "object_name": "Kira_Rollback_C",
        "cost": 140000,
    },
    "Brutus_Wrecker": {
        "asset_path": "/Game/Cars/Models/Brutus/Brutus_Wrecker.Brutus_Wrecker_C",
        "object_name": "Brutus_Wrecker_C",
        "cost": 250000,
    },
    "GolimaRotator": {
        "asset_path": "/Game/Cars/Models/GolimaRotator/GolimaRotator.GolimaRotator_C",
        "object_name": "GolimaRotator_C",
        "cost": 620000,
    },
    "Vulcan": {
        "asset_path": "/Game/Cars/Models/Vulcan/Vulcan.Vulcan_C",
        "object_name": "Vulcan_C",
        "cost": 4500000,
    },
    "Terra": {
        "asset_path": "/Game/Cars/Models/Terra/Terra.Terra_C",
        "object_name": "Terra_C",
        "cost": 490000,
    },
    "Ambi": {
        "asset_path": "/Game/Cars/Models/Ambi/Ambi.Ambi_C",
        "object_name": "Ambi_C",
        "cost": 100000,
    },
    "Tavan_Ambulance": {
        "asset_path": "/Game/Cars/Models/Tavan/Tavan_Ambulance.Tavan_Ambulance_C",
        "object_name": "Tavan_Ambulance_C",
        "cost": 80000,
    },
    "Brutus_Ambulance": {
        "asset_path": "/Game/Cars/Models/Brutus/Brutus_Ambulance.Brutus_Ambulance_C",
        "object_name": "Brutus_Ambulance_C",
        "cost": 220000,
    },
    "Raven": {
        "asset_path": "/Game/Cars/Models/Volture/Raven.Raven_C",
        "object_name": "Raven_C",
        "cost": 100000,
    },
    "EVan": {
        "asset_path": "/Game/Cars/Models/EVan/EVan.EVan_C",
        "object_name": "EVan_C",
        "cost": 80000,
    },
    "Trophy2": {
        "asset_path": "/Game/Cars/Models/Trophy2/Trophy2.Trophy2_C",
        "object_name": "Trophy2_C",
        "cost": 9000,
    },
    "Longhorn_Rollback": {
        "asset_path": "/Game/Cars/Models/Longhorn_Rollback/Longhorn_Rollback.Longhorn_Rollback_C",
        "object_name": "Longhorn_Rollback_C",
        "cost": 300000,
    },
    "Longhorn_Rollback_UL": {
        "asset_path": "/Game/Cars/Models/Longhorn_Rollback/Longhorn_Rollback_UL.Longhorn_Rollback_UL_C",
        "object_name": "Longhorn_Rollback_UL_C",
        "cost": 400000,
    },
    "Trailer_Cotra_20": {
        "asset_path": "/Game/Cars/Models/Trailer_Cotra/Cotra_20_3S.Cotra_20_3S_C",
        "object_name": "Cotra_20_3S_C",
        "cost": 27000,
    },
    "Trailer_Cotra_40": {
        "asset_path": "/Game/Cars/Models/Trailer_Cotra/Cotra_40_3.Cotra_40_3_C",
        "object_name": "Cotra_40_3_C",
        "cost": 50000,
    },
    "Trailer_Shovan": {
        "asset_path": "/Game/Cars/Models/Shovan/Shovan.Shovan_10_C",
        "object_name": "Shovan_10_C",
        "cost": 70000,
    },
    "Trailer_Shobed": {
        "asset_path": "/Game/Cars/Models/Shobed/Shobed.Shobed_10_C",
        "object_name": "Shobed_10_C",
        "cost": 60000,
    },
    "Trailer_Shotan": {
        "asset_path": "/Game/Cars/Models/Shotan/Shotan.Shotan_10_C",
        "object_name": "Shotan_10_C",
        "cost": 160000,
    },
}


class VehiclePartSlot(enum.Enum):
    Invalid = 0
    Body = 1
    Engine = 2
    Transmission = 3
    FinalDriveRatio = 4
    Intake = 5
    CoolantRadiator = 6
    Turbocharger = 7
    LSD0 = 8
    LSD1 = 9
    LSD2 = 10
    LSD3 = 11
    LSD4 = 12
    LSD5 = 13
    LSD6 = 14
    LSD7 = 15
    LSD8 = 16
    LSD9 = 17
    LSDMax = 18
    Tire0 = 19
    Tire1 = 20
    Tire2 = 21
    Tire3 = 22
    Tire4 = 23
    Tire5 = 24
    Tire6 = 25
    Tire7 = 26
    Tire8 = 27
    Tire9 = 28
    Tire10 = 29
    Tire11 = 30
    Tire12 = 31
    Tire13 = 32
    Tire14 = 33
    Tire15 = 34
    Tire16 = 35
    Tire17 = 36
    Tire18 = 37
    Tire19 = 38
    TireMax = 39
    Wheel0 = 40
    Wheel1 = 41
    Wheel2 = 42
    Wheel3 = 43
    Wheel4 = 44
    Wheel5 = 45
    Wheel6 = 46
    Wheel7 = 47
    Wheel8 = 48
    Wheel9 = 49
    Wheel10 = 50
    Wheel11 = 51
    Wheel12 = 52
    Wheel13 = 53
    Wheel14 = 54
    Wheel15 = 55
    Wheel16 = 56
    Wheel17 = 57
    Wheel18 = 58
    Wheel19 = 59
    WheelMax = 60
    WheelSpacer0 = 61
    WheelSpacer1 = 62
    WheelSpacer2 = 63
    WheelSpacer3 = 64
    WheelSpacer4 = 65
    WheelSpacer5 = 66
    WheelSpacer6 = 67
    WheelSpacer7 = 68
    WheelSpacer8 = 69
    WheelSpacer9 = 70
    WheelSpacer10 = 71
    WheelSpacer11 = 72
    WheelSpacer12 = 73
    WheelSpacer13 = 74
    WheelSpacer14 = 75
    WheelSpacer15 = 76
    WheelSpacer16 = 77
    WheelSpacer17 = 78
    WheelSpacer18 = 79
    WheelSpacer19 = 80
    WheelSpacerMax = 81
    BrakePad0 = 82
    BrakePad1 = 83
    BrakePad2 = 84
    BrakePad3 = 85
    BrakePad4 = 86
    BrakePad5 = 87
    BrakePad6 = 88
    BrakePad7 = 89
    BrakePad8 = 90
    BrakePad9 = 91
    BrakePad10 = 92
    BrakePad11 = 93
    BrakePad12 = 94
    BrakePad13 = 95
    BrakePad14 = 96
    BrakePad15 = 97
    BrakePad16 = 98
    BrakePad17 = 99
    BrakePad18 = 100
    BrakePad19 = 101
    BrakePadMax = 102
    AngleKit = 103
    Suspension_Spring0 = 104
    Suspension_Spring1 = 105
    Suspension_Spring2 = 106
    Suspension_Spring3 = 107
    Suspension_Spring4 = 108
    Suspension_Spring5 = 109
    Suspension_Spring6 = 110
    Suspension_Spring7 = 111
    Suspension_Spring8 = 112
    Suspension_Spring9 = 113
    Suspension_Spring10 = 114
    Suspension_Spring11 = 115
    Suspension_Spring12 = 116
    Suspension_Spring13 = 117
    Suspension_Spring14 = 118
    Suspension_Spring15 = 119
    Suspension_Spring16 = 120
    Suspension_Spring17 = 121
    Suspension_Spring18 = 122
    Suspension_Spring19 = 123
    Suspension_SpringMax = 124
    Suspension_Damper0 = 125
    Suspension_Damper1 = 126
    Suspension_Damper2 = 127
    Suspension_Damper3 = 128
    Suspension_Damper4 = 129
    Suspension_Damper5 = 130
    Suspension_Damper6 = 131
    Suspension_Damper7 = 132
    Suspension_Damper8 = 133
    Suspension_Damper9 = 134
    Suspension_Damper10 = 135
    Suspension_Damper11 = 136
    Suspension_Damper12 = 137
    Suspension_Damper13 = 138
    Suspension_Damper14 = 139
    Suspension_Damper15 = 140
    Suspension_Damper16 = 141
    Suspension_Damper17 = 142
    Suspension_Damper18 = 143
    Suspension_Damper19 = 144
    Suspension_DamperMax = 145
    Suspension_RideHeight0 = 146
    Suspension_RideHeight1 = 147
    Suspension_RideHeight2 = 148
    Suspension_RideHeight3 = 149
    Suspension_RideHeight4 = 150
    Suspension_RideHeight5 = 151
    Suspension_RideHeight6 = 152
    Suspension_RideHeight7 = 153
    Suspension_RideHeight8 = 154
    Suspension_RideHeight9 = 155
    Suspension_RideHeight10 = 156
    Suspension_RideHeight11 = 157
    Suspension_RideHeight12 = 158
    Suspension_RideHeight13 = 159
    Suspension_RideHeight14 = 160
    Suspension_RideHeight15 = 161
    Suspension_RideHeight16 = 162
    Suspension_RideHeight17 = 163
    Suspension_RideHeight18 = 164
    Suspension_RideHeight19 = 165
    Suspension_RideHeightMax = 166
    AntiRollBar0 = 167
    AntiRollBar1 = 168
    AntiRollBar2 = 169
    AntiRollBar3 = 170
    AntiRollBar4 = 171
    AntiRollBar5 = 172
    AntiRollBar6 = 173
    AntiRollBar7 = 174
    AntiRollBar8 = 175
    AntiRollBar9 = 176
    AntiRollBarMax = 177
    TaxiLicense = 178
    BusLicense = 179
    EscortLicense = 180
    FrontSpoiler = 181
    RearSpoiler = 182
    RearWing = 183
    Fender = 184
    SideSkirt = 185
    FrontWindowSticker = 186
    FrontWindowSunVisor = 187
    RearWindowLouvers = 188
    TrailerHitch = 189
    CargoBed0 = 190
    CargoBedAttachment0 = 191
    RoofRack0 = 192
    Roof = 193
    FrontBumper = 194
    RearBumper = 195
    Bonnet = 196
    Trunk = 197
    Winch0 = 198
    Crane0 = 199
    Crane1 = 200
    Crane2 = 201
    BrakePower = 202
    BrakeBalance = 203
    Headlight = 204
    Utility0 = 205
    Utility1 = 206
    Utility2 = 207
    Utility3 = 208
    Utility4 = 209
    Utility5 = 210
    Utility6 = 211
    Utility7 = 212
    Utility8 = 213
    Utility9 = 214
    UtilityMax = 215
    Bullbar = 216
    Attachment0 = 217
    Attachment1 = 218
    Attachment2 = 219
    Attachment3 = 220
    Attachment4 = 221
    Attachment5 = 222
    Attachment6 = 223
    Attachment7 = 224
    Attachment8 = 225
    Attachment9 = 226
    Attachment10 = 227
    Attachment11 = 228
    Attachment12 = 229
    Attachment13 = 230
    Attachment14 = 231
    Attachment15 = 232
    Attachment16 = 233
    Attachment17 = 234
    Attachment18 = 235
    Attachment19 = 236
    AttachmentMax = 237
    EMTVehiclePartSlot_MAX = 238
