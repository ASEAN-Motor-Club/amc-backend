# MotorTown World Save Data (`get_world()`)

## Source

`amc.save_file.get_world()` decrypts `/var/lib/motortown-server/MotorTown/Saved/SaveGames/Worlds/0/Island.world` and returns `json.loads(decrypted)["world"]`.

There is also `amc.game_server.get_world(session)` which fetches the same structure from the remote game server API at `https://server.aseanmotorclub.com/api/world/`.

## Complete Structure

```json
{
  "worldGuid": "GUID",
  "worldKey": "string",
  "day": 0,
  "timeOfDay": 0,
  "townState": {
    "zoneStates": {
      "__key": {
        "taxiTransportRate": 0,
        "policePatrolRate": 0,
        "garbageCollectRate": 0
      }
    },
    "policy": {
      "activePolicies": ["string"],
      "policyReadinessHours": [0]
    }
  },
  "character": {
    "dateTimeTicks": 0,
    "player": {
      "slot": 0,
      "guid": "GUID",
      "nickname": "string",
      "playtimeSeconds": 0,
      "bIsCheater": true,
      "locations": {},
      "level": {
        "levels": [],
        "experiences": []
      },
      "customization": {
        "bodyKey": "string",
        "bodyParts": [],
        "costumeBodyKey": "string",
        "costumeItemKey": "string"
      },
      "counter": {
        "counters": {},
        "discoveredPOIGuids": []
      },
      "helpMessageCounter": {
        "counters": {}
      },
      "money": 0,
      "lastVehicleId": 0,
      "vehicles": [
        {
          "iD": 0,
          "key": "string",
          "vehicleName": "string",
          "fuel": 0,
          "condition": 0,
          "settings": {
            "bLocked": true,
            "driveAllowedPlayers": "string",
            "levelRequirementsToDrive": [],
            "vehicleOwnerProfitShare": 0,
            "vehicleSettings": []
          },
          "seatPosition": {
            "forwardPosition": 0,
            "height": 0,
            "steeringWheelDistance": 0,
            "steeringWheelHeight": 0
          },
          "mirrorPositions": {
            "mirrorPositions": []
          },
          "customization": {
            "bodyMaterialIndex": 0,
            "bodyColors": []
          },
          "decal": {
            "decalLayers": []
          },
          "doors": [],
          "traveledDistanceKm": 0,
          "lastUsedPlayTimeSeconds": 0,
          "bIsModded": true,
          "vehicleTags": []
        }
      ],
      "vehicleParts": [
        {
          "iD": 0,
          "key": "string",
          "installedVehicleId": 0,
          "damage": 0,
          "floatValues": [],
          "int64Values": [],
          "stringValues": [],
          "vectorValues": [],
          "slot": "string",
          "itemInventory": {
            "items": []
          }
        }
      ],
      "houses": [],
      "items": [],
      "itemInventory": {
        "items": [
          {
            "key": "string",
            "numStack": 0
          }
        ]
      },
      "couponInventory": {
        "items": [
          {
            "key": "string",
            "numStack": 0
          }
        ]
      },
      "equipmentInventory": {
        "equipmentSlots": [
          {
            "slot": "string",
            "itemKey": "string"
          }
        ]
      },
      "characterCustomizationParts": {
        "slots": []
      },
      "quickbarSlots": [],
      "vehicleTopSpeedsKPH": {},
      "quests": [],
      "finishedQuestKeys": [],
      "companies": [
        {
          "guid": "GUID",
          "bIsCorporation": true,
          "name": "string",
          "shortDesc": "string",
          "money": 0,
          "ownerCharacterId": {
            "uniqueNetId": "string",
            "characterGuid": "GUID"
          },
          "ownerCharacterName": "string",
          "addedVehicleSlots": 0,
          "roles": [
            {
              "roleGuid": "GUID",
              "name": "string",
              "bIsOwner": true,
              "bIsDefaultRole": true,
              "bIsManager": true
            }
          ],
          "players": [
            {
              "characterId": {
                "uniqueNetId": "string",
                "characterGuid": "GUID"
              },
              "characterName": "string",
              "roleGuid": "GUID"
            }
          ],
          "joinRequests": [],
          "vehicles": [
            {
              "vehicleId": 0,
              "donatorVehicleId": 0,
              "vehicleKey": "string",
              "vehicleName": "string",
              "busRouteGuid": "GUID",
              "taxiDepotHouseGuid": "GUID",
              "routePointIndex": 0,
              "vehicleFlags": 0,
              "dailyStats": [
                {
                  "totalCost": 0,
                  "totalIncome": 0,
                  "passengerStat": {
                    "numPassengers": 0,
                    "payments": 0
                  },
                  "cargoStats": []
                }
              ]
            }
          ],
          "ownVehicles": [
            {
              "iD": 0,
              "key": "string",
              "vehicleName": "string",
              "fuel": 0,
              "condition": 0,
              "settings": {
                "bLocked": true,
                "driveAllowedPlayers": "string",
                "levelRequirementsToDrive": [],
                "vehicleOwnerProfitShare": 0,
                "vehicleSettings": []
              },
              "seatPosition": {
                "forwardPosition": 0,
                "height": 0,
                "steeringWheelDistance": 0,
                "steeringWheelHeight": 0
              },
              "mirrorPositions": {
                "mirrorPositions": []
              },
              "customization": {
                "bodyMaterialIndex": 0,
                "bodyColors": [
                  {
                    "materialSlotName": "string",
                    "color": {
                      "b": 0,
                      "g": 0,
                      "r": 0,
                      "a": 0
                    },
                    "metallic": 0,
                    "roughness": 0
                  }
                ]
              },
              "decal": {
                "decalLayers": [
                  {
                    "decalKey": "string",
                    "color": {
                      "b": 0,
                      "g": 0,
                      "r": 0,
                      "a": 0
                    },
                    "position": {
                      "x": 0,
                      "y": 0
                    },
                    "rotation": {
                      "pitch": 0,
                      "yaw": 0,
                      "roll": 0
                    },
                    "decalScale": 0,
                    "stretch": 0,
                    "coverage": 0,
                    "flags": 0
                  }
                ]
              },
              "doors": [],
              "traveledDistanceKm": 0,
              "lastUsedPlayTimeSeconds": 0,
              "bIsModded": true,
              "vehicleTags": []
            }
          ],
          "ownVehicleWorldData": [
            {
              "vehicleId": 0,
              "cargoSpaces": [
                {
                  "cargoSpaceIndex": 0,
                  "loadedItemType": 0,
                  "loadedItemVolume": 0
                }
              ]
            }
          ],
          "ownVehicleParts": [
            {
              "iD": 0,
              "key": "string",
              "installedVehicleId": 0,
              "damage": 0,
              "floatValues": [],
              "int64Values": [],
              "stringValues": [],
              "vectorValues": [],
              "slot": "string",
              "itemInventory": {
                "items": []
              }
            }
          ],
          "busRoutes": [
            {
              "guid": "GUID",
              "routeName": "string",
              "busStops": ["GUID"],
              "points": [
                {
                  "pointGuid": "GUID",
                  "flags": 0
                }
              ]
            }
          ],
          "truckRoutes": [
            {
              "guid": "GUID",
              "routeName": "string",
              "deliveryPoints": [
                {
                  "deliveryPointGuid": "GUID",
                  "flags": 0
                }
              ]
            }
          ],
          "dailyStats": [
            {
              "totalCost": 0,
              "totalIncome": 0
            }
          ],
          "contractsInProgress": [],
          "idleDurationSeconds": 0
        }
      ],
      "loans": []
    },
    "version": 0
  },
  "aICharacters2": {
    "residents": [
      {
        "residentId": 0,
        "residentKey": "string",
        "zoneKey": "string",
        "homePOIGuid": "GUID",
        "workPOIGuid": "GUID",
        "state": 0,
        "currentPOIGuid": "GUID",
        "currentBusStopGuid": "GUID",
        "destinationPOIGuid": "GUID",
        "destinationBusStopGuid": "GUID",
        "stayUntilSeconds": 0,
        "bTransportNotAvailable": true
      }
    ],
    "characters": [
      {
        "characterType": "string",
        "actorName": "string",
        "residentKey": "string",
        "residentId": 0,
        "absoluteLocation": {
          "x": 0,
          "y": 0,
          "z": 0
        },
        "rotation": {
          "pitch": 0,
          "yaw": 0,
          "roll": 0
        },
        "destinationLocation": {
          "x": 0,
          "y": 0,
          "z": 0
        },
        "passengerRequirementFlags": 0,
        "numGroupCharacters": 0,
        "busPassenger": {
          "type": "string",
          "exitAfterStops": 0,
          "busStopName": "string",
          "destinationPOIGuid": "GUID",
          "destinationBusStopGuid": "GUID"
        }
      }
    ],
    "searchAndRescueLastLocations": [
      {
        "x": 0,
        "y": 0,
        "z": 0
      }
    ]
  },
  "aIVehicles": {
    "vehicles": [
      {
        "index": 0,
        "tractorIndex": 0,
        "settingKey": "string",
        "absoluteLocation": {
          "x": 0,
          "y": 0,
          "z": 0
        },
        "rotation": {
          "pitch": 0,
          "yaw": 0,
          "roll": 0
        },
        "customization": {
          "bodyMaterialIndex": 0,
          "bodyColors": [
            {
              "materialSlotName": "string",
              "color": {
                "b": 0,
                "g": 0,
                "r": 0,
                "a": 0
              },
              "metallic": 0,
              "roughness": 0
            }
          ]
        },
        "towRequest": {
          "bIsValid": true,
          "startLocation": {
            "x": 0,
            "y": 0,
            "z": 0
          },
          "destinationLocation": {
            "x": 0,
            "y": 0,
            "z": 0
          },
          "payment": 0,
          "towRequestFlags": 0,
          "bArrived": true,
          "punctureWheelSlotIndex": 0,
          "missingWheelSlotIndex": 0
        },
        "vehicleKey": "string",
        "ageSeconds": 0,
        "fireData": {
          "location": {
            "x": 0,
            "y": 0,
            "z": 0
          },
          "initialThermalMass": 0,
          "initialFuel": 0,
          "initialTemperature": 0,
          "bEnableSpreading": true,
          "cellRadius": 0,
          "fireFlags": 0,
          "payment": 0,
          "fireCells": [
            {
              "cellCoord": {
                "x": 0,
                "y": 0,
                "z": 0
              },
              "relativeLocation": {
                "x": 0,
                "y": 0,
                "z": 0
              },
              "thermalMass": 0,
              "temperature": 0,
              "fuel": 0
            }
          ]
        }
      }
    ]
  },
  "cargoSpawners": [
    {
      "spawnerGuid": "GUID",
      "bHasCargo": true,
      "timeToSpawnSeconds": 0
    }
  ],
  "deliveryPoints": [
    {
      "deliveryPointGuid": "GUID",
      "inputInventory": {
        "entries": [
          {
            "cargoKey": "string",
            "amount": 0
          }
        ]
      },
      "outputInventory": {
        "entries": [
          {
            "cargoKey": "string",
            "amount": 0
          }
        ]
      },
      "productionProgresses": [0]
    }
  ],
  "deliveries": [
    {
      "iD": 0,
      "cargoType": "string",
      "cargoKey": "string",
      "numCargos": 0,
      "colorIndex": 0,
      "weight": 0,
      "senderPointGuid": "GUID",
      "receiverPointGuid": "GUID",
      "timeUntilExpiresSeconds": 0,
      "payment": 0,
      "deliveryFlags": 0
    }
  ],
  "parkingSpaces": {
    "__key": {
      "parkedVehicleId": 0,
      "buildingGuid": "GUID"
    }
  },
  "vehicles": {},
  "notInWorldVehicleIds": [],
  "housings": {
    "__key": {
      "housingKey": "string",
      "ownerUniqueNetId": "string",
      "ownerCharacterGuid": "GUID",
      "ownerName": "string",
      "rentLeftTimeSeconds": 0
    }
  },
  "buildings": [
    {
      "guid": "GUID",
      "buildingKey": "string",
      "housingKey": "string",
      "ownerUniqueNetId": "string",
      "ownerCharacterGuid": "GUID",
      "buildingStep": 0,
      "location": {
        "x": 0,
        "y": 0,
        "z": 0
      },
      "rotation": {
        "pitch": 0,
        "yaw": 0,
        "roll": 0
      },
      "materials": {},
      "itemInventory": {
        "items": []
      },
      "accessSettings": {
        "inventoryAccess": "string"
      }
    }
  ],
  "depots": [
    {
      "buildingGuid": "GUID",
      "name": "string",
      "storage": 0,
      "taxiDispatchLevel": 0,
      "dailyStats": []
    }
  ],
  "items": [
    {
      "itemGuid": "GUID",
      "itemKey": "string",
      "ownerCharacterGuid": "GUID",
      "ownerName": "string",
      "location": {
        "x": 0,
        "y": 0,
        "z": 0
      },
      "rotation": {
        "pitch": 0,
        "yaw": 0,
        "roll": 0
      },
      "bHoldingByServerCharacter": true,
      "bStrapped": true
    }
  ],
  "police": {
    "patrolAreas": [
      {
        "areaCenter": {
          "x": 0,
          "y": 0,
          "z": 0
        },
        "areaRadius": 0,
        "numTotalPoints": 0,
        "pointsToPatrol": [
          {
            "x": 0,
            "y": 0,
            "z": 0
          }
        ],
        "contributionCountMap": {
          "(UniqueNetId=\"76561198782529165\",CharacterGuid=0C17066D4E9EEFEF3597B287BDA80CF6)": 0
        }
      }
    ]
  },
  "fire": {
    "fires": [
      {
        "location": {
          "x": 0,
          "y": 0,
          "z": 0
        },
        "initialThermalMass": 0,
        "initialFuel": 0,
        "initialTemperature": 0,
        "bEnableSpreading": true,
        "cellRadius": 0,
        "fireFlags": 0,
        "payment": 0,
        "fireCells": [
          {
            "cellCoord": {
              "x": 0,
              "y": 0,
              "z": 0
            },
            "relativeLocation": {
              "x": 0,
              "y": 0,
              "z": 0
            },
            "thermalMass": 0,
            "temperature": 0,
            "fuel": 0
          }
        ]
      }
    ],
    "untilNextRandomFireStartTimeSeconds": 0
  },
  "navigation": {
    "customDestinations": []
  },
  "trade": {
    "balance": 0,
    "last7DaysStats": [
      {
        "exportCount": 0,
        "exportAmount": 0,
        "exportCargoCounts": [],
        "importCount": 0,
        "importAmount": 0,
        "importCargoCounts": [
          {
            "cargoKey": "string",
            "count": 0
          }
        ],
        "balanceDelta_Policy": 0,
        "balanceDelta_Etc": 0,
        "balanceDeltaTotal": 0
      }
    ]
  }
}
```

## Guidelines

- When cross-referencing `depots.buildingGuid` with `buildings.guid`, filter the `buildings` array into a single dict/map once rather than scanning it per depot.
- `housings` and `parkingSpaces` are dicts keyed by string identifiers.
- `character` contains the dedicated server player's save data including `companies`.
- `aICharacters2` contains NPC residents and active AI characters in the world.
- `aIVehicles` contains AI-driven vehicles (delivery trucks, tow trucks, etc.).
- The world structure is large; only access the keys you need.
- The save file is encrypted with AES ECB using a fixed key.
