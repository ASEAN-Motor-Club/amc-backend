import logging
from datetime import timedelta

import discord
from discord import app_commands, ui
from discord.ext import commands
from django.conf import settings
from django.db.models import F, Sum
from django.utils import timezone
from typing import TYPE_CHECKING

from amc.game_server import is_player_online
from amc.models import Character, Delivery, House, HousingLicense, Player
from amc.mod_server import (
    extend_house_rent,
    get_houses,
    get_rent_info,
    rent_house,
    transfer_money,
)
from amc_cogs.housing_market import get_market_multiplier
from amc_finance.loans import get_player_bank_balance
from amc_finance.services import (
    record_treasury_rent_income,
    register_player_withdrawal,
)

if TYPE_CHECKING:
    from amc.discord_client import AMCDiscordBot

logger = logging.getLogger("amc.cogs.housing")

ZERO_GUID = "00000000000000000000000000000000"
SECONDS_PER_DAY = 86400


class CharacterSelect(ui.Select):
    def __init__(self, characters, action, house_data, rent_info, houses):
        options = []
        for c in characters:
            last = ""
            if c.last_online:
                delta = timezone.now() - c.last_online
                if delta < timedelta(minutes=5):
                    last = "Online now"
                elif delta < timedelta(hours=1):
                    last = f"{int(delta.total_seconds() / 60)}m ago"
                elif delta < timedelta(days=1):
                    last = f"{int(delta.total_seconds() / 3600)}h ago"
                else:
                    last = f"{int(delta.total_seconds() / 86400)}d ago"
            options.append(
                discord.SelectOption(
                    label=c.name,
                    description=last or "Unknown",
                    value=str(c.id),
                )
            )
        super().__init__(placeholder="Select a character...", options=options)
        self.action = action
        self.house_data = house_data
        self.rent_info = rent_info
        self.houses = houses

    async def callback(self, interaction: discord.Interaction):
        character_id = int(self.values[0])
        try:
            character = await Character.objects.select_related("player").aget(
                id=character_id
            )
        except Character.DoesNotExist:
            await interaction.response.send_message(
                "Character not found.", ephemeral=True
            )
            return

        if self.action == "buy":
            await self._handle_buy(interaction, character)
        elif self.action == "extend":
            await self._handle_extend(interaction, character)

    async def _handle_buy(self, interaction: discord.Interaction, character: Character):
        await interaction.response.defer(ephemeral=True)

        player = character.player
        house_data = self.house_data
        rent_info = self.rent_info
        house_guid = house_data["HouseGuid"]
        house_key = house_data.get("HousegKey", "")

        cost = rent_info.get("Cost")
        ratio = rent_info.get("HousingPlotRentalPriceRatio", 5.0)
        max_days = rent_info.get("MaxHousingPlotRentalDays", 15)

        if cost is None:
            # Fall back to House model (populated from PAK extraction)
            try:
                house_obj = await House.objects.aget(key=house_key)
                cost = house_obj.cost
            except House.DoesNotExist:
                pass

        if cost is None:
            await interaction.followup.send(
                f"Could not determine rent cost for **{house_key}**. "
                "The house data may not be loaded.",
                ephemeral=True,
            )
            return

        rent_cost = int(cost * ratio)

        market_multiplier, market_breakdown = await get_market_multiplier(
            self.houses, max_days
        )
        rent_cost = int(rent_cost * market_multiplier)

        lookback_days = settings.RENT_REBATE_LOOKBACK_DAYS
        cutoff = timezone.now() - timedelta(days=lookback_days)
        total_earnings = (
            await Delivery.objects.filter(
                character=character, timestamp__gte=cutoff
            ).aaggregate(total=Sum(F("payment") + F("subsidy")))
        )["total"] or 0

        licenses = HousingLicense.objects.filter(character=character)
        exact = licenses.filter(house_key=house_guid)
        general = licenses.filter(house_key__isnull=True)
        license = await exact.order_by("-rebate_pct").afirst()
        if license is None:
            license = await general.order_by("-rebate_pct").afirst()

        if license:
            effective_cost = int(rent_cost * license.rebate_pct / 100)
        else:
            effective_cost = rent_cost

        rebate = min(total_earnings, effective_cost)
        net_cost = max(0, rent_cost - rebate)

        mod_session = interaction.client.http_client_mod
        game_session = interaction.client.http_client_game

        if not await is_player_online(player.unique_id, game_session):
            await interaction.followup.send(
                f"**{character.name}** is not online.", ephemeral=True
            )
            return

        balance = await get_player_bank_balance(character)
        if balance < net_cost:
            await interaction.followup.send(
                f"Insufficient bank balance. Need **₱{net_cost:,}**, have **₱{balance:,}**.",
                ephemeral=True,
            )
            return

        try:
            await register_player_withdrawal(net_cost, character, player)
        except ValueError as e:
            await interaction.followup.send(f"Bank error: {e}", ephemeral=True)
            return

        try:
            await transfer_money(
                mod_session, net_cost, "House Rent", str(player.unique_id)
            )
        except Exception:
            logger.warning(
                "Failed to transfer wallet compensation for %s", character.guid,
                exc_info=True,
            )

        try:
            await rent_house(mod_session, house_guid, character.guid)
        except Exception as e:
            await interaction.followup.send(
                f"Failed to complete house rental: {e}", ephemeral=True
            )
            return

        await record_treasury_rent_income(net_cost, f"House Rent — {character.guid}")

        embed = discord.Embed(
            title="House Rented",
            description=(
                f"**{character.name}** has rented **{house_key}**.\n"
                f"Cost: **₱{rent_cost:,}**\n"
                + (f"Rebate: **-₱{rebate:,}**\n" if rebate > 0 else "")
                + f"Net Cost: **₱{net_cost:,}**\n"
                f"Duration: **{max_days} days**"
            ),
            color=discord.Color.green(),
        )
        embed.add_field(
            name="Market Rate",
            value=(
                f"**{market_breakdown['multiplier']}x**\n"
                f"Avg players (7d): {market_breakdown['avg_players']} "
                f"(factor {market_breakdown['player_factor']})\n"
                f"Avg rent remaining: {market_breakdown['avg_rent_pct']}% "
                f"(health {market_breakdown['rent_health']})"
            ),
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _handle_extend(
        self, interaction: discord.Interaction, character: Character
    ):
        await interaction.response.defer(ephemeral=True)

        player = character.player
        house_data = self.house_data
        rent_info = self.rent_info
        house_guid = house_data["HouseGuid"]
        house_key = house_data.get("HousegKey", "")

        cost = rent_info.get("Cost")
        ratio = rent_info.get("HousingPlotRentalPriceRatio", 5.0)
        max_days = rent_info.get("MaxHousingPlotRentalDays", 15)
        rent_left_seconds = house_data.get("Net_RentLeftTimeSeconds", 0)

        if cost is None:
            try:
                house_obj = await House.objects.aget(key=house_key)
                cost = house_obj.cost
            except House.DoesNotExist:
                pass

        if cost is None:
            await interaction.followup.send(
                f"Could not determine rent cost for **{house_key}**.",
                ephemeral=True,
            )
            return

        current_days = rent_left_seconds / SECONDS_PER_DAY
        extend_days = max(0, max_days - current_days)
        if extend_days <= 0:
            await interaction.followup.send(
                "This house is already at maximum rental duration.", ephemeral=True
            )
            return

        extend_seconds = int(extend_days * SECONDS_PER_DAY)
        cost_per_day = int(cost * ratio / max_days)
        rent_cost = cost_per_day * int(extend_days)

        market_multiplier, market_breakdown = await get_market_multiplier(
            self.houses, max_days
        )
        rent_cost = int(rent_cost * market_multiplier)

        lookback_days = settings.RENT_REBATE_LOOKBACK_DAYS
        cutoff = timezone.now() - timedelta(days=lookback_days)
        total_earnings = (
            await Delivery.objects.filter(
                character=character, timestamp__gte=cutoff
            ).aaggregate(total=Sum(F("payment") + F("subsidy")))
        )["total"] or 0

        licenses = HousingLicense.objects.filter(character=character)
        exact = licenses.filter(house_key=house_guid)
        general = licenses.filter(house_key__isnull=True)
        license = await exact.order_by("-rebate_pct").afirst()
        if license is None:
            license = await general.order_by("-rebate_pct").afirst()

        if license:
            effective_cost = int(rent_cost * license.rebate_pct / 100)
        else:
            effective_cost = rent_cost

        rebate = min(total_earnings, effective_cost)
        net_cost = max(0, rent_cost - rebate)

        mod_session = interaction.client.http_client_mod
        game_session = interaction.client.http_client_game

        if not await is_player_online(player.unique_id, game_session):
            await interaction.followup.send(
                f"**{character.name}** is not online.", ephemeral=True
            )
            return

        balance = await get_player_bank_balance(character)
        if balance < net_cost:
            await interaction.followup.send(
                f"Insufficient bank balance. Need **₱{net_cost:,}**, have **₱{balance:,}**.",
                ephemeral=True,
            )
            return

        view = ExtendConfirmView(
            character=character,
            player=player,
            house_guid=house_guid,
            house_key=house_key,
            extend_seconds=extend_seconds,
            extend_days=int(extend_days),
            rent_cost=rent_cost,
            rebate=rebate,
            net_cost=net_cost,
            mod_session=mod_session,
            market_breakdown=market_breakdown,
        )

        embed = discord.Embed(
            title="Extend House Rent",
            description=f"**{house_key}** — owned by **{character.name}**",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="Current Rent",
            value=f"{current_days:.1f} days remaining",
            inline=True,
        )
        embed.add_field(
            name="Extension",
            value=f"{int(extend_days)} days",
            inline=True,
        )
        embed.add_field(
            name="Market Rate",
            value=(
                f"**{market_breakdown['multiplier']}x** "
                f"(players {market_breakdown['player_factor']}, "
                f"rent health {market_breakdown['rent_health']})"
            ),
            inline=True,
        )
        embed.add_field(name="\u200b", value="\u200b", inline=False)
        embed.add_field(name="Rent", value=f"₱{rent_cost:,}", inline=True)
        if rebate > 0:
            embed.add_field(name="Rebate", value=f"-₱{rebate:,}", inline=True)
        embed.add_field(name="Net Cost", value=f"**₱{net_cost:,}**", inline=True)

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class CharacterSelectView(ui.View):
    def __init__(self, characters, action, house_data, rent_info, houses):
        super().__init__(timeout=120)
        self.add_item(CharacterSelect(characters, action, house_data, rent_info, houses))


class ExtendConfirmView(ui.View):
    def __init__(
        self,
        character,
        player,
        house_guid,
        house_key,
        extend_seconds,
        extend_days,
        rent_cost,
        rebate,
        net_cost,
        mod_session,
        market_breakdown=None,
    ):
        super().__init__(timeout=120)
        self.character = character
        self.player = player
        self.house_guid = house_guid
        self.house_key = house_key
        self.extend_seconds = extend_seconds
        self.extend_days = extend_days
        self.rent_cost = rent_cost
        self.rebate = rebate
        self.net_cost = net_cost
        self.mod_session = mod_session
        self.market_breakdown = market_breakdown

    @ui.button(label="Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)

        balance = await get_player_bank_balance(self.character)
        if balance < self.net_cost:
            await interaction.followup.send(
                f"Insufficient bank balance. Need **₱{self.net_cost:,}**, have **₱{balance:,}**.",
                ephemeral=True,
            )
            self.stop()
            return

        if self.net_cost > 0:
            try:
                await register_player_withdrawal(
                    self.net_cost, self.character, self.player
                )
            except ValueError as e:
                await interaction.followup.send(f"Bank error: {e}", ephemeral=True)
                self.stop()
                return

        try:
            await extend_house_rent(
                self.mod_session,
                self.house_guid,
                self.character.guid,
                self.extend_seconds,
            )
        except Exception as e:
            await interaction.followup.send(
                f"Failed to extend rent: {e}", ephemeral=True
            )
            self.stop()
            return

        if self.net_cost > 0:
            await record_treasury_rent_income(
                self.net_cost, f"House Rent Extend — {self.character.guid}"
            )

        embed = discord.Embed(
            title="House Rent Extended",
            description=(
                f"**{self.house_key}** rent extended by **{self.extend_days} days**.\n"
                f"Cost: **₱{self.net_cost:,}**"
                + (f"\nRebate: **₱{self.rebate:,}**" if self.rebate > 0 else "")
            ),
            color=discord.Color.green(),
        )
        if self.market_breakdown:
            mb = self.market_breakdown
            embed.add_field(
                name="Market Rate",
                value=(
                    f"**{mb['multiplier']}x** "
                    f"(players {mb['player_factor']}, "
                    f"rent health {mb['rent_health']})"
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)
        self.stop()

    @ui.button(label="Cancel", style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("Cancelled.", ephemeral=True)
        self.stop()


class HousingCog(commands.Cog):
    def __init__(self, bot: "AMCDiscordBot"):
        self.bot = bot

    house = app_commands.Group(name="house", description="Housing management")

    async def _fetch_houses(self):
        try:
            return await get_houses(self.bot.http_client_mod)
        except Exception:
            logger.warning("Failed to fetch houses from mod", exc_info=True)
            return []

    async def _get_characters_for_user(self, discord_user_id: int):
        try:
            player = await Player.objects.aget(discord_user_id=discord_user_id)
        except Player.DoesNotExist:
            return None, []
        characters = [
            c
            async for c in Character.objects.filter(
                player=player, guid__isnull=False
            )
            .exclude(guid=ZERO_GUID)
            .order_by("-last_online")
        ]
        return player, characters

    @house.command(name="buy", description="Rent a house using bank funds")
    @app_commands.describe(plot_key="The house plot to rent")
    async def house_buy(self, interaction: discord.Interaction, plot_key: str):
        await interaction.response.defer(ephemeral=True)

        houses = await self._fetch_houses()
        house_data = None
        for h in houses:
            if h.get("HouseGuid", "").upper() == plot_key.upper():
                house_data = h
                break
            if h.get("HousegKey", "").upper() == plot_key.upper():
                house_data = h
                break

        if not house_data:
            await interaction.followup.send("House not found.", ephemeral=True)
            return

        if house_data.get("Net_OwnerCharacterGuid", ZERO_GUID) not in (
            "",
            ZERO_GUID,
        ):
            await interaction.followup.send(
                "This house is already owned.", ephemeral=True
            )
            return

        try:
            rent_info = await get_rent_info(
                self.bot.http_client_mod, house_data["HouseGuid"]
            )
        except Exception:
            await interaction.followup.send(
                "Failed to fetch rent info from server.", ephemeral=True
            )
            return

        player, characters = await self._get_characters_for_user(interaction.user.id)
        if not player:
            await interaction.followup.send(
                "You must be verified to rent a house.", ephemeral=True
            )
            return
        if not characters:
            await interaction.followup.send(
                "You have no characters.", ephemeral=True
            )
            return

        view = CharacterSelectView(characters, "buy", house_data, rent_info, houses)
        cost = rent_info.get("Cost")
        ratio = rent_info.get("HousingPlotRentalPriceRatio", 5.0)
        max_days = rent_info.get("MaxHousingPlotRentalDays", 15)

        if cost is None:
            try:
                house_obj = await House.objects.aget(key=house_data.get("HousegKey", ""))
                cost = house_obj.cost
            except House.DoesNotExist:
                pass

        rent_cost = int(cost * ratio) if cost else None

        market_multiplier, market_breakdown = await get_market_multiplier(
            houses, max_days
        )

        embed = discord.Embed(
            title="Rent House",
            description=f"**{house_data.get('HousegKey', plot_key)}**",
            color=discord.Color.blue(),
        )
        if rent_cost is not None:
            market_rent_cost = int(rent_cost * market_multiplier)
            embed.add_field(name="Cost", value=f"₱{market_rent_cost:,}", inline=True)
            embed.add_field(name="Duration", value=f"{max_days} days", inline=True)
        embed.add_field(
            name="Market Rate",
            value=(
                f"**{market_breakdown['multiplier']}x** "
                f"(players {market_breakdown['player_factor']}, "
                f"rent health {market_breakdown['rent_health']})"
            ),
            inline=False,
        )

        await interaction.followup.send(
            "Select the character to rent with:", embed=embed, view=view, ephemeral=True
        )

    @house.command(name="extend", description="Extend house rent using bank funds")
    @app_commands.describe(plot_key="The house to extend")
    async def house_extend(self, interaction: discord.Interaction, plot_key: str):
        await interaction.response.defer(ephemeral=True)

        player, characters = await self._get_characters_for_user(interaction.user.id)
        if not player:
            await interaction.followup.send(
                "You must be verified.", ephemeral=True
            )
            return
        if not characters:
            await interaction.followup.send(
                "You have no characters.", ephemeral=True
            )
            return

        character_guids = {c.guid.upper() for c in characters if c.guid}

        houses = await self._fetch_houses()
        house_data = None
        for h in houses:
            owner_guid = h.get("Net_OwnerCharacterGuid", "").upper()
            if owner_guid not in character_guids:
                continue
            if h.get("HouseGuid", "").upper() == plot_key.upper():
                house_data = h
                break
            if h.get("HousegKey", "").upper() == plot_key.upper():
                house_data = h
                break

        if not house_data:
            await interaction.followup.send(
                "House not found or not owned by your characters.", ephemeral=True
            )
            return

        owner_guid = house_data.get("Net_OwnerCharacterGuid", "").upper()
        owning_characters = [c for c in characters if c.guid and c.guid.upper() == owner_guid]

        try:
            rent_info = await get_rent_info(
                self.bot.http_client_mod, house_data["HouseGuid"]
            )
        except Exception:
            await interaction.followup.send(
                "Failed to fetch rent info from server.", ephemeral=True
            )
            return

        view = CharacterSelectView(owning_characters, "extend", house_data, rent_info, houses)

        rent_info_max_days = rent_info.get("MaxHousingPlotRentalDays", 15)
        _, market_breakdown = await get_market_multiplier(houses, rent_info_max_days)

        embed = discord.Embed(
            title="Extend House Rent",
            description=f"**{house_data.get('HousegKey', plot_key)}**",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="Market Rate",
            value=(
                f"**{market_breakdown['multiplier']}x** "
                f"(players {market_breakdown['player_factor']}, "
                f"rent health {market_breakdown['rent_health']})"
            ),
            inline=False,
        )

        await interaction.followup.send(
            "Select the character that owns this house:",
            embed=embed,
            view=view,
            ephemeral=True,
        )

    async def _buy_autocomplete(
        self, interaction: discord.Interaction, current: str
    ):
        houses = await self._fetch_houses()
        choices = []
        for h in houses:
            owner = h.get("Net_OwnerCharacterGuid", "")
            if owner and owner != ZERO_GUID:
                continue
            key = h.get("HousegKey", h.get("HouseGuid", ""))
            if current and current.upper() not in key.upper():
                continue
            choices.append(
                app_commands.Choice(name=key[:100], value=h["HouseGuid"])
            )
            if len(choices) >= 25:
                break
        return choices

    async def _extend_autocomplete(
        self, interaction: discord.Interaction, current: str
    ):
        _, characters = await self._get_characters_for_user(interaction.user.id)
        if not characters:
            return []
        character_guids = {c.guid.upper() for c in characters if c.guid}

        houses = await self._fetch_houses()
        choices = []
        for h in houses:
            owner = h.get("Net_OwnerCharacterGuid", "").upper()
            if owner not in character_guids:
                continue
            key = h.get("HousegKey", h.get("HouseGuid", ""))
            if current and current.upper() not in key.upper():
                continue
            choices.append(
                app_commands.Choice(name=key[:100], value=h["HouseGuid"])
            )
            if len(choices) >= 25:
                break
        return choices

    @house_buy.autocomplete("plot_key")
    async def house_buy_plot_key_autocomplete(
        self, interaction: discord.Interaction, current: str
    ):
        return await self._buy_autocomplete(interaction, current)

    @house_extend.autocomplete("plot_key")
    async def house_extend_plot_key_autocomplete(
        self, interaction: discord.Interaction, current: str
    ):
        return await self._extend_autocomplete(interaction, current)


async def setup(bot):
    await bot.add_cog(HousingCog(bot))
