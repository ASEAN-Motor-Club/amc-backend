jq '
map(
  # Store the parent guid in a variable for later use
  .guid as $guid |
  # Store the entire original object in a variable
  . as $original_data |
  
  # Start creating an array of fixture objects for each input item
  [
    # 1. Create the DeliveryPoint object
    {
      "model": "amc.deliverypoint",
      "pk": $guid,
      "fields": {
        "name": .name.en,
        "type": .type,
        "coord": "SRID=0;POINT Z (\(.coord.x) \(.coord.y) \(.coord.z))"
      }
    }
  ]
  # 2. Add the Input (demand) Storage objects
  + (.demandStorage // {} | to_entries | map({
      "model": "amc.deliverypointstorage",
      "fields": {
        "delivery_point": $guid,
        "kind": "IN",
        "cargo_key": .key,
        "cargo": .key,
        "amount": 0,
        "capacity": .value
      }
    }))
  # 3. Add the Output (supply) Storage objects
  + (.supplyStorage // {} | to_entries | map({
      "model": "amc.deliverypointstorage",
      "fields": {
        "delivery_point": $guid,
        "kind": "OU",
        "cargo_key": .key,
        "cargo": .key,
        "amount": 0,
        "capacity": .value
      }
    }))
) | flatten
' $1
