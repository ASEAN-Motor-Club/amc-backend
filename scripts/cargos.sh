jq '
to_entries |
map(
    {
      "model": "amc.cargo",
      "pk": .key,
      "fields": {
        "key": .key,
        "label": .value.en
      }
  }
)
' ../cargo_name.json
