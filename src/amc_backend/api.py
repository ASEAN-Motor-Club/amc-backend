from ninja import NinjaAPI, Schema
from amc_backend.auth import OAuth2Bearer

api = NinjaAPI(urls_namespace="internal")


class UserMeSchema(Schema):
    user: str
    name: str
    mail: str
    grps: list[str]


@api.get("/users/me/", response=UserMeSchema, auth=OAuth2Bearer())
async def get_user_me(request):
    user = request.auth
    groups = [group.name async for group in user.groups.all()]
    return {
        "user": user.username,
        "name": user.discord_name if hasattr(user, "discord_name") else user.username,
        "mail": user.email or "",
        "grps": groups,
    }


api.add_router("/stats/", "amc.api.routes.stats_router")
api.add_router("/players/", "amc.api.routes.players_router")
api.add_router("/characters/", "amc.api.routes.characters_router")
api.add_router("/player_positions/", "amc.api.routes.player_positions_router")
api.add_router("/player_count/", "amc.api.routes.player_count_router")
api.add_router("/character_locations/", "amc.api.routes.player_locations_router")
api.add_router("/race_setups/", "amc.api.routes.race_setups_router")
api.add_router("/teams/", "amc.api.routes.teams_router")
api.add_router("/scheduled_events/", "amc.api.routes.scheduled_events_router")
api.add_router("/tracks/", "amc.api.routes.tracks_router")
api.add_router("/results/", "amc.api.routes.results_router")
api.add_router("/championships/", "amc.api.routes.championships_router")
api.add_router("/deliverypoints/", "amc.api.routes.deliverypoints_router")
api.add_router("/deliveryjobs/", "amc.api.routes.deliveryjobs_router")
api.add_router("/webui/deliveryjobs/", "amc.api.routes.webui_deliveryjobs_router")
api.add_router("/", "amc.api.routes.app_router")
api.add_router("/houses/", "amc.api.house_routes.house_router")

# Phase 1: Public API Routers
api.add_router("/cargos/", "amc.api.routes.cargos_router")
api.add_router("/subsidies/rules/", "amc.api.routes.subsidies_rules_router")
api.add_router("/ministry/", "amc.api.routes.ministry_router")
api.add_router("/championships_list/", "amc.api.routes.championships_list_router")
api.add_router("/stats/deliveries/", "amc.api.routes.deliveries_stats_router")

# Phase 2: Community Features Routers
api.add_router("/companies/", "amc.api.routes.companies_router")
api.add_router("/ministry/elections/", "amc.api.routes.ministry_elections_router")
api.add_router("/race_setups_list/", "amc.api.routes.race_setups_list_router")

# Phase 3: Extended Data Routers
api.add_router("/subsidies/areas/", "amc.api.routes.subsidy_areas_router")
api.add_router("/stats/passengers/", "amc.api.routes.passenger_stats_router")
api.add_router("/decals/", "amc.api.routes.decals_router")
api.add_router("/dealerships/", "amc.api.routes.dealerships_router")
# Server Commands
api.add_router("/commands/", "amc.api.routes.commands_list_router")

# Bot Events (SSE for Discord bot)
api.add_router("/bot_events/", "amc.api.bot_events.router")

# Dashboard auth (Discord Activity + Steam Login)
api.add_router("/auth/", "amc.api.auth_routes.auth_router")

# Auction System — private balance endpoint
api.add_router("/auction/", "amc.api.auction_routes.router")

# Anti-cheat physics integrity reporting (PoC — observe-only)
api.add_router("/ac/", "amc.api.anticheat_routes.router")

# Shortcut Zones
api.add_router("/shortcut_zones/", "amc.api.routes.shortcut_zones_router")

# Economy management — subsidy rules and job posting config (internal, Tailscale-only)
api.add_router("/economy/", "amc.api.economy_routes.router")
