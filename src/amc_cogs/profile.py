import logging
import discord
from discord import app_commands
from discord.ext import commands
from django.utils import timezone
from django.db.models import Sum, Count, F
from datetime import timedelta
from typing import Optional, TYPE_CHECKING

from amc.game_server import get_players
from amc.models import (
    Character,
    CharacterLocationStats,
    Player,
    PlayerStatusLog,
    PlayerVehicleLog,
    PlayerRestockDepotLog,
    ServerCargoArrivedLog,
    ServerSignContractLog,
    ServerPassengerArrivedLog,
    ServerTowRequestArrivedLog,
    Delivery,
    Thank,
    RescueRequest,
)
from amc.enums import CargoKey, VehicleKey
from amc.sessions import MAX_SESSION
from amc_finance.models import LedgerEntry

if TYPE_CHECKING:
    from amc.discord_client import AMCDiscordBot

logger = logging.getLogger(__name__)


def _format_relative_time(dt) -> str:
    """Format a datetime as a human-readable relative string (e.g. '3d ago')."""
    if dt is None:
        return "Never"
    delta = timezone.now() - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "Just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    return f"{months}mo ago"


class PlayerProfileCog(commands.Cog):
    def __init__(self, bot: "AMCDiscordBot"):
        self.bot = bot

    async def character_autocomplete(
        self, interaction: discord.Interaction, current: str
    ):
        """
        Custom autocomplete that shows character name, driver level, and
        last online status. Does NOT expose Steam IDs.
        """
        if not current:
            # Show recently active characters when no input
            characters = (
                Character.objects.filter(last_online__isnull=False)
                .select_related("player")
                .order_by("player_id", "-last_online")
                .distinct("player_id")
            )
            # Re-sort by last_online after dedup (distinct requires matching order_by prefix)
            characters = (
                Character.objects.filter(id__in=characters.values("id"))
                .select_related("player")
                .order_by("-last_online")[:25]
            )
        else:
            characters = (
                Character.objects.filter(name__icontains=current)
                .select_related("player")
                .order_by("player_id", F("last_online").desc(nulls_last=True))
                .distinct("player_id")
            )
            characters = (
                Character.objects.filter(id__in=characters.values("id"))
                .select_related("player")
                .order_by(F("last_online").desc(nulls_last=True))[:25]
            )

        # Check who is currently online
        try:
            online_players = await get_players(self.bot.http_client_game)
            online_ids = {int(pid) for pid, _ in online_players}
        except Exception:
            online_ids = set()

        online_choices = []
        offline_choices = []

        async for char in characters:
            level_str = f"Lv.{char.driver_level}" if char.driver_level else "Lv.?"
            is_online = char.player.unique_id in online_ids

            if is_online:
                status = "🟢 Online"
            else:
                status = _format_relative_time(char.last_online)

            label = f"{char.name} - {level_str} Driver - {status}"
            # Discord limits choice name to 100 chars
            label = label[:100]
            choice = app_commands.Choice(name=label, value=str(char.id))

            if is_online:
                online_choices.append(choice)
            else:
                offline_choices.append(choice)

        return [*online_choices, *offline_choices][:25]

    @app_commands.command(
        name="player_profile",
        description="Show the full profile for a character",
    )
    @app_commands.describe(
        character="Character to show profile for (defaults to yourself)"
    )
    @app_commands.autocomplete(character=character_autocomplete)
    async def player_profile(
        self,
        interaction: discord.Interaction,
        character: Optional[str] = None,
    ):
        await interaction.response.defer()

        target_character: Character | None = None
        target_player: Player | None = None

        if character:
            try:
                target_character = await Character.objects.select_related(
                    "player"
                ).aget(id=int(character))
                target_player = target_character.player
            except (ValueError, Character.DoesNotExist):
                await interaction.followup.send("Character not found.", ephemeral=True)
                return
        else:
            # Default to the calling user
            try:
                target_player = await Player.objects.aget(
                    discord_user_id=interaction.user.id
                )
                target_character = await (
                    target_player.characters.with_last_login()
                    .filter(last_login__isnull=False)
                    .alatest("last_login")
                )
            except Player.DoesNotExist:
                await interaction.followup.send(
                    "You need to be verified to use this command. Use `/verify` to link your account.",
                    ephemeral=True,
                )
                return
            except Character.DoesNotExist:
                await interaction.followup.send(
                    "No characters found for your account.",
                    ephemeral=True,
                )
                return

        embed = await self._build_profile_embed(target_character, target_player)
        embed.set_footer(text=f"Requested by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

    async def _build_profile_embed(
        self, character: Character, player: Player
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"🪪 Player Profile: {character.name}",
            color=discord.Color.blue(),
            timestamp=timezone.now(),
        )

        # --- Levels ---
        levels = []
        level_fields = [
            ("Driver", character.driver_level),
            ("Bus", character.bus_level),
            ("Taxi", character.taxi_level),
            ("Police", character.police_level),
            ("Truck", character.truck_level),
            ("Wrecker", character.wrecker_level),
            ("Racer", character.racer_level),
            ("Government", character.gov_employee_level),
        ]
        for name, level in level_fields:
            # pyrefly: ignore [bad-argument-type]
            if name == "Government" and not character.is_gov_employee:
                continue
            levels.append(f"**{name}:** {level or 0}")

        embed.add_field(
            name="📊 Levels",
            value="\n".join(levels),
            inline=True,
        )

        # --- Economy ---
        economy_lines = [
            f"**Total Donations:** ${character.total_donations:,}",
        ]
        if character.money is not None:
            economy_lines.insert(0, f"**Cash:** ${character.money:,}")

        embed.add_field(
            name="💰 Economy",
            value="\n".join(economy_lines),
            inline=True,
        )

        # --- Activity ---
        # Total play time. Ranges longer than MAX_SESSION are log-corruption
        # artefacts (a lost logout used to leave a session running for weeks);
        # counting them is what inflated profiles to thousands of hours.
        session_agg = await PlayerStatusLog.objects.filter(
            character=character, duration__lte=MAX_SESSION
        ).aaggregate(total=Sum("duration", default=timedelta(0)))
        total_hours = session_agg["total"].total_seconds() / 3600

        # Last online
        if character.last_online:
            last_online_str = discord.utils.format_dt(character.last_online, "R")
        else:
            last_online_str = "Never"

        embed.add_field(
            name="🕒 Activity",
            value=(
                f"**Last Online:** {last_online_str}\n"
                f"**Total Play Time:** {total_hours:,.1f}h"
            ),
            inline=True,
        )

        # --- Favourite Vehicles ---
        stats = await CharacterLocationStats.objects.filter(
            character=character
        ).afirst()
        if stats and stats.vehicle_stats:
            all_stats = dict(stats.vehicle_stats)
            total_samples = stats.total_location_records or 1

            # Separate on-foot ("None" key) from actual vehicles
            on_foot_count = all_stats.pop("None", 0)
            vehicle_total = sum(all_stats.values())

            vehicle_lines = []

            # Show on-foot % against total samples
            if on_foot_count:
                on_foot_pct = on_foot_count / total_samples * 100
                vehicle_lines.append(f"🚶 **On Foot:** {on_foot_pct:.0f}%")

            # Show vehicles as % of vehicle-only time
            if all_stats and vehicle_total > 0:
                top_vehicles = sorted(
                    all_stats.items(), key=lambda x: x[1], reverse=True
                )[:5]
                for key, count in top_vehicles:
                    try:
                        label = VehicleKey(key).label
                    except ValueError:
                        label = key
                    pct = count / vehicle_total * 100
                    vehicle_lines.append(f"**{label}:** {pct:.0f}%")

            if vehicle_lines:
                embed.add_field(
                    name="🚗 Favourite Vehicles",
                    value="\n".join(vehicle_lines),
                    inline=True,
                )

        # --- Deliveries by Cargo ---
        delivery_stats = (
            ServerCargoArrivedLog.objects.filter(player=player)
            .values("cargo_key")
            .annotate(count=Count("id"), total_payment=Sum("payment"))
            .order_by("-total_payment")
        )

        cargo_labels = dict(CargoKey.choices)
        delivery_lines = []
        grand_total_count = 0
        grand_total_payment = 0

        async for stat in delivery_stats[:8]:
            cargo_name = cargo_labels.get(stat["cargo_key"], stat["cargo_key"])
            count = stat["count"]
            payment = stat["total_payment"] or 0
            grand_total_count += count
            grand_total_payment += payment
            delivery_lines.append(f"**{cargo_name}:** {count} (${payment:,})")

        # Count remaining if more than 8
        remaining_stats = (
            ServerCargoArrivedLog.objects.filter(player=player)
            .values("cargo_key")
            .annotate(count=Count("id"), total_payment=Sum("payment"))
            .order_by("-count")
        )
        all_totals = await remaining_stats.aaggregate(
            total_count=Count("id"),
            total_payment=Sum("payment", default=0),
        )

        total_deliveries = all_totals["total_count"]
        total_payment = all_totals["total_payment"]

        if total_deliveries > 0:
            header = f"📦 Deliveries ({total_deliveries} total, ${total_payment:,})"
            if not delivery_lines:
                delivery_lines.append("No deliveries recorded.")
            embed.add_field(
                name=header,
                value="\n".join(delivery_lines),
                inline=False,
            )

        # --- Jobs & Contracts ---
        # Jobs contributed to (distinct fulfilled jobs linked via Delivery)
        jobs_contributed = await (
            Delivery.objects.filter(
                character=character,
                job__isnull=False,
                job__fulfilled=True,
            )
            .values("job")
            .distinct()
            .acount()
        )

        # Total job completion bonuses from ledger
        job_bonus_agg = await LedgerEntry.objects.filter(
            account__character=character,
            journal_entry__description="Job Completion",
        ).aaggregate(total=Sum("credit", default=0))
        total_job_bonuses = job_bonus_agg["total"]

        # Contracts
        contracts_agg = await ServerSignContractLog.objects.filter(
            player=player
        ).aaggregate(
            count=Count("id"),
            total_payment=Sum("payment", default=0),
        )

        jobs_lines = [
            f"**Jobs Contributed To:** {jobs_contributed}",
            f"**Total Job Bonuses:** ${total_job_bonuses:,}",
            f"**Contracts Signed:** {contracts_agg['count']} (${contracts_agg['total_payment']:,})",
        ]
        embed.add_field(
            name="🏗️ Jobs & Contracts",
            value="\n".join(jobs_lines),
            inline=True,
        )

        # --- Services ---
        passengers_agg = await ServerPassengerArrivedLog.objects.filter(
            player=player
        ).aaggregate(
            count=Count("id"),
            total_payment=Sum("payment", default=0),
        )
        tow_agg = await ServerTowRequestArrivedLog.objects.filter(
            player=player
        ).aaggregate(
            count=Count("id"),
            total_payment=Sum("payment", default=0),
        )

        services_lines = [
            f"**Passengers:** {passengers_agg['count']} (${passengers_agg['total_payment']:,})",
            f"**Tow Requests:** {tow_agg['count']} (${tow_agg['total_payment']:,})",
        ]
        embed.add_field(
            name="🚌 Services",
            value="\n".join(services_lines),
            inline=True,
        )

        # --- Other ---
        vehicles_bought = await PlayerVehicleLog.objects.filter(
            character=character, action=PlayerVehicleLog.Action.BOUGHT
        ).acount()

        restocks = await PlayerRestockDepotLog.objects.filter(
            character=character
        ).acount()

        thanks_received = await Thank.objects.filter(
            recipient_character=character
        ).acount()

        rescues_requested = await RescueRequest.objects.filter(
            character=character
        ).acount()

        rescue_responses = await player.rescue_responses.acount()

        other_lines = [
            f"**Vehicles Bought:** {vehicles_bought}",
            f"**Depot Restocks:** {restocks}",
            f"**Thanks Received:** {thanks_received}",
            f"**Rescues Requested:** {rescues_requested}",
            f"**Rescue Responses:** {rescue_responses}",
        ]
        embed.add_field(
            name="🔧 Other",
            value="\n".join(other_lines),
            inline=True,
        )

        return embed


async def setup(bot):
    await bot.add_cog(PlayerProfileCog(bot))
