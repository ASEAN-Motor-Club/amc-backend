jq 'map({
  model: "amc.deliverypoint",
  pk: .guid,
  fields: {
    type: .type,
    name: .name,
    coord: "Point (\(.coord.x) \(.coord.y) \(.coord.z))"
  }
})' $1
