from ninja import NinjaAPI

api = NinjaAPI()
api.add_router('/stats/', 'amc.api.routes.stats_router')
api.add_router('/players/', 'amc.api.routes.players_router')
api.add_router('/characters/', 'amc.api.routes.characters_router')
api.add_router('/player_positions/', 'amc.api.routes.player_positions_router')
api.add_router('/character_locations/', 'amc.api.routes.player_locations_router')
api.add_router('/race_setups/', 'amc.api.routes.race_setups_router')
api.add_router('/teams/', 'amc.api.routes.teams_router')
api.add_router('/scheduled_events/', 'amc.api.routes.scheduled_events_router')
api.add_router('/championships/', 'amc.api.routes.championships_router')
api.add_router('/deliverypoints/', 'amc.api.routes.deliverypoints_router')

