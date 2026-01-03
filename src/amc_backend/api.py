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
api.add_router('/tracks/', 'amc.api.routes.tracks_router')
api.add_router('/results/', 'amc.api.routes.results_router')
api.add_router('/championships/', 'amc.api.routes.championships_router')
api.add_router('/deliverypoints/', 'amc.api.routes.deliverypoints_router')
api.add_router('/deliveryjobs/', 'amc.api.routes.deliveryjobs_router')
api.add_router('/', 'amc.api.routes.app_router')

# Phase 1: Public API Routers
api.add_router('/cargos/', 'amc.api.routes.cargos_router')
api.add_router('/subsidies/rules/', 'amc.api.routes.subsidies_rules_router')
api.add_router('/ministry/', 'amc.api.routes.ministry_router')
api.add_router('/championships_list/', 'amc.api.routes.championships_list_router')
api.add_router('/stats/deliveries/', 'amc.api.routes.deliveries_stats_router')

# Phase 2: Community Features Routers
api.add_router('/companies/', 'amc.api.routes.companies_router')
api.add_router('/ministry/elections/', 'amc.api.routes.ministry_elections_router')
api.add_router('/race_setups_list/', 'amc.api.routes.race_setups_list_router')

# Phase 3: Extended Data Routers
api.add_router('/subsidies/areas/', 'amc.api.routes.subsidy_areas_router')
api.add_router('/stats/passengers/', 'amc.api.routes.passenger_stats_router')
api.add_router('/decals/', 'amc.api.routes.decals_router')
api.add_router('/dealerships/', 'amc.api.routes.dealerships_router')
# Server Commands
api.add_router('/commands/', 'amc.api.routes.commands_list_router')

# Bot Events (SSE for Discord bot)
api.add_router('/bot_events/', 'amc.api.bot_events.router')
