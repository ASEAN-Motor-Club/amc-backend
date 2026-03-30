# Generated from player_positions.proto
# To regenerate: protoc --python_out=. player_positions.proto

from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import symbol_database as _symbol_database

_sym_db = _symbol_database.Default()

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(
    b'\n\x16player_positions.proto\x12\x10player_positions\x1ad\n'
    b'\x0ePlayerPosition\x12R\n\tunique_id\x18\x01 \x01(\t\n'
    b'\x0bplayer_name\x18\x02 \x01(\t\n\x01z\x18\x03 \x01(\x01\n'
    b'\x01y\x18\x04 \x01(\x01\n\x01x\x18\x05 \x01(\x01\n'
    b'\x0bvehicle_key\x18\x06 \x01(\t\x1aD\n\x0fPlayerPositions\x121\n'
    b'\x07players\x18\x01 \x03(\x0b2 .player_positions.PlayerPosition'
    b'b\x06proto3'
)

PlayerPosition = _sym_db.GetPrototype(DESCRIPTOR.message_types_by_name['PlayerPosition'])
PlayerPositions = _sym_db.GetPrototype(DESCRIPTOR.message_types_by_name['PlayerPositions'])
