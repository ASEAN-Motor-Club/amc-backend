from ninja import NinjaAPI

api = NinjaAPI()
api.add_router('/stats/', 'amc.api.routes.stats_router')
api.add_router('/players/', 'amc.api.routes.players_router')
api.add_router('/characters/', 'amc.api.routes.characters_router')
api.add_router('/player_positions/', 'amc.api.routes.player_positions_router')

