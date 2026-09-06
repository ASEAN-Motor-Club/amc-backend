from django.test import SimpleTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import timedelta
from decimal import Decimal
from django.utils import timezone
from amc.command_framework import registry, CommandContext, CommandRegistry
from amc.vehicles import format_driveline_game
from amc.commands.admin import (
    cmd_bill,
    cmd_exit,
    cmd_save_vehicle,
    cmd_spawn,
    cmd_spawn_assets,
    cmd_spawn_dealerships,
    cmd_spawn_displays,
    cmd_spawn_garage_single,
    cmd_spawn_garages,
    cmd_spawn_vehicle,
    cmd_tp_player,
)
from amc.commands.vehicles import cmd_check_mods, cmd_check_parts, cmd_rent
from amc.commands.decals import cmd_apply_decal, cmd_decals, cmd_save_decal
from amc.commands.events import (
    cmd_auto_grid,
    cmd_countdown,
    cmd_events_list,
    cmd_results,
    cmd_setup_event,
    cmd_staggered_start,
)
from amc.commands.finance import (
    cmd_bank,
    cmd_burn,
    cmd_donate,
    cmd_loan,
    cmd_repay_loan,
    cmd_set_repayment_rate,
    cmd_set_saving_rate,
    cmd_toggle_ubi,
    cmd_withdraw,
)
from amc.commands.general import (
    cmd_bot,
    cmd_coords,
    cmd_credits,
    cmd_help,
    cmd_rename,
    cmd_song_request,
    cmd_verify,
)
from amc.commands.jobs import cmd_jobs, cmd_subsidies
from amc.commands.language import cmd_language
from amc.commands.rp_rescue import cmd_rescue, cmd_respond
from amc.commands.social import cmd_thank
from amc.commands.teleport import cmd_tp_coords, cmd_tp_name, cmd_tp_vehicle
from amc.commands.faction import cmd_arrest, parse_location_string
from amc.commands.police import cmd_police
from amc.commands.wanted import cmd_wanted


from amc.models import Character, CriminalRecord, Player, PoliceSession
# Import other models as needed for mocking or actual DB tests if we go that route


class CommandRegistryTestCase(SimpleTestCase):
    def setUp(self):
        self.registry = CommandRegistry()

    def test_build_regex_simple(self):
        async def func(ctx, arg1: str):
            pass

        # Test registering
        decorator = self.registry.register("/test")
        decorator(func)

        cmd = self.registry.commands[0]
        pattern = cmd["pattern"]

        self.assertTrue(pattern.match("/test hello"))
        match = pattern.match("/test hello")
        self.assertEqual(match.group("arg1"), "hello")

    def test_build_regex_int(self):
        async def func(ctx, number: int):
            pass

        decorator = self.registry.register("/num")
        decorator(func)

        cmd = self.registry.commands[0]
        match = cmd["pattern"].match("/num 123")
        self.assertTrue(match)
        self.assertEqual(match.group("number"), "123")

        match = cmd["pattern"].match("/num -50")
        self.assertTrue(match)
        self.assertEqual(match.group("number"), "-50")

    def test_build_regex_multiple_args(self):
        async def func(ctx, name: str, age: int):
            pass

        decorator = self.registry.register("/person")
        decorator(func)

        match = self.registry.commands[0]["pattern"].match("/person John 30")
        self.assertTrue(match)
        self.assertEqual(match.group("name"), "John")
        self.assertEqual(match.group("age"), "30")

    def test_execute_flow(self):
        ctx = MagicMock(spec=CommandContext)
        mock_func = AsyncMock()

        # Manually inject a command into a fresh registry
        reg = CommandRegistry()

        @reg.register("/mock")
        async def mock_cmd_func(ctx, arg: str):
            await mock_func(ctx, arg)

        async def run():
            await reg.execute("/mock check", ctx)

        # We need to run this in an async loop or use async_to_sync wrapper if using Django's TestCase capability for async
        # But since this is SimpleTestCase and we are calling logic, we might need a workaround for execution if not running via a runner that supports it purely.
        # Ideally we test execute logic.
        pass

    async def test_deprecated_command_returns_true(self):
        """Test that deprecated commands return True to prevent forwarding to Discord"""
        ctx = MagicMock(spec=CommandContext)
        ctx.reply = AsyncMock()
        ctx.is_current_event = True

        reg = CommandRegistry()

        @reg.register("/deprecated_cmd", deprecated=True)
        async def deprecated_func(ctx):
            pass

        result = await reg.execute("/deprecated_cmd", ctx)
        self.assertTrue(result)

    async def test_deprecated_command_sends_message(self):
        """Test that deprecated commands send a deprecation message to the user"""
        ctx = MagicMock(spec=CommandContext)
        ctx.reply = AsyncMock()
        ctx.is_current_event = True

        reg = CommandRegistry()

        @reg.register("/old_cmd", deprecated=True)
        async def old_func(ctx):
            pass

        await reg.execute("/old_cmd", ctx)
        ctx.reply.assert_called_once()
        args, _ = ctx.reply.call_args
        self.assertIn("Command Deprecated", args[0])

    async def test_deprecated_command_custom_message(self):
        """Test that deprecated commands can use a custom deprecation message"""
        ctx = MagicMock(spec=CommandContext)
        ctx.reply = AsyncMock()
        ctx.is_current_event = True

        custom_msg = "This command has been replaced by /newcmd"
        reg = CommandRegistry()

        @reg.register("/legacy", deprecated=True, deprecated_message=custom_msg)
        async def legacy_func(ctx):
            pass

        await reg.execute("/legacy", ctx)
        ctx.reply.assert_called_once_with(custom_msg)

    async def test_deprecated_command_does_not_execute_handler(self):
        """Test that deprecated commands do not execute the handler function"""
        ctx = MagicMock(spec=CommandContext)
        ctx.reply = AsyncMock()
        ctx.is_current_event = True

        handler_called = False
        reg = CommandRegistry()

        @reg.register("/obsolete", deprecated=True)
        async def obsolete_func(ctx):
            nonlocal handler_called
            handler_called = True

        await reg.execute("/obsolete", ctx)
        self.assertFalse(handler_called)

    async def test_deprecated_command_no_message_when_not_current_event(self):
        """Test that deprecated commands don't send messages for historical events"""
        ctx = MagicMock(spec=CommandContext)
        ctx.reply = AsyncMock()
        ctx.is_current_event = False  # Historical event

        reg = CommandRegistry()

        @reg.register("/old", deprecated=True)
        async def old_func(ctx):
            pass

        result = await reg.execute("/old", ctx)
        self.assertTrue(result)  # Still returns True to prevent forwarding
        ctx.reply.assert_not_called()  # But doesn't send message


class MockResponse:
    def __init__(self, data=None):
        self.status = 200
        self.data = data or {}
        self.json = AsyncMock(return_value=self.data)

    def __await__(self):
        return self._await().__await__()

    async def _await(self) -> "MockResponse":
        return self

    async def __aenter__(self) -> "MockResponse":
        return self

    async def __aexit__(self, *args):
        pass


class CommandsTestCase(TestCase):
    def setUp(self):
        self.ctx = MagicMock(spec=CommandContext)
        self.ctx.reply = AsyncMock()
        self.ctx.announce = AsyncMock()
        self.ctx.http_client_mod = MagicMock()
        self.ctx.http_client = MagicMock()
        self.ctx.discord_client = None
        self.ctx.player_info = {}

        # Configure http clients to return MockResponse
        self.ctx.http_client_mod.get.return_value = MockResponse()
        self.ctx.http_client_mod.post.return_value = MockResponse()
        self.ctx.http_client.get.return_value = MockResponse()
        self.ctx.http_client.post.return_value = MockResponse()

        self.player = Player.objects.create(unique_id="76561198000000000")
        self.character = Character.objects.create(
            name="TestChar", player=self.player, guid="guid-123"
        )
        self.ctx.character = self.character
        self.ctx.player = self.player
        self.ctx.timestamp = timezone.now()

    async def test_cmd_help(self):
        # Test that we get a reply
        await cmd_help(self.ctx)
        self.ctx.reply.assert_called()

        # Check content contains some expected commands
        args, _ = self.ctx.reply.call_args
        output = args[0]
        self.assertIn("Available Commands", output)
        # self.assertIn("General", output) # General category title is hidden
        self.assertIn("/help", output)
        self.assertIn("/credits", output)

        # Check specific metadata presence (description) if possible
        self.assertIn("Show this help message", output)

    async def test_cmd_credits(self):
        await cmd_credits(self.ctx)
        self.ctx.reply.assert_called()

    async def test_cmd_bank(self):
        mock_bal = 1000
        mock_loan = 500
        mock_max_loan = 5000
        mock_npl = {
            "is_npl": True,
            "loan_balance": 1_000_000,
            "period_days": 7,
            "repayment_rate": Decimal("0.10"),
            "total_repaid_in_period": Decimal(5_000),
            "min_required_repayment": Decimal(100_000),
        }

        # Create mock transaction ledger entry
        mock_ledger = MagicMock()
        mock_ledger.journal_entry.date = "2023-01-01"
        mock_ledger.journal_entry.description = "Test Tx"
        mock_ledger.credit = 100
        mock_ledger.debit = 0

        async def async_iter(items):
            for i in items:
                yield i

        mock_slice = MagicMock()
        mock_slice.__aiter__.side_effect = lambda: async_iter([mock_ledger])

        with (
            patch(
                "amc.commands.finance.get_player_bank_balance",
                new=AsyncMock(return_value=mock_bal),
            ),
            patch(
                "amc.commands.finance.get_player_loan_balance",
                new=AsyncMock(return_value=mock_loan),
            ),
            patch(
                "amc.commands.finance.get_character_max_loan",
                new=AsyncMock(return_value=(mock_max_loan, "Ok")),
            ),
            patch(
                "amc.commands.finance.get_character_npl_status",
                new=AsyncMock(return_value=mock_npl),
            ),
            patch("amc_finance.models.LedgerEntry.objects.filter") as mock_filter,
        ):
            mock_filter.return_value.select_related.return_value.order_by.return_value.__getitem__.return_value = mock_slice

            await cmd_bank(self.ctx)

            self.ctx.reply.assert_called()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("<Title>Your Bank ASEAN Account</>", output)
            self.assertIn("<Bold>Balance:</> <Money>1,000</>", output)
            self.assertIn("Test Tx", output)
            self.assertIn("Daily (IRL) Interest Rate", output)
            self.assertIn("Payment Plan", output)
            self.assertIn("behind on its payment plan", output)

    async def test_cmd_tp_admin(self):
        self.ctx.player_info["bIsAdmin"] = True
        with patch("amc.commands.teleport.teleport_player", new=AsyncMock()) as mock_tp:
            await cmd_tp_coords(self.ctx, 100, 200, 300)
            mock_tp.assert_called_with(
                self.ctx.http_client_mod,
                "76561198000000000",
                {"X": 100, "Y": 200, "Z": 300},
                no_vehicles=False,
            )

    async def test_cmd_tp_non_admin(self):
        self.ctx.player_info["bIsAdmin"] = False
        with patch("amc.commands.teleport.teleport_player", new=AsyncMock()) as mock_tp:
            await cmd_tp_coords(self.ctx, 100, 200, 300)
            mock_tp.assert_not_called()
            self.ctx.reply.assert_called_with("Admin Only")

    async def test_cmd_tp_name(self):
        from amc.models import TeleportPoint

        mock_tp = MagicMock()
        mock_tp.location.x = 100
        mock_tp.location.y = 200
        mock_tp.location.z = 300

        # Test valid name
        with (
            patch(
                "amc.models.TeleportPoint.objects.aget",
                new=AsyncMock(return_value=mock_tp),
            ),
            patch(
                "amc.commands.teleport.teleport_player", new=AsyncMock()
            ) as mock_teleport,
        ):
            await cmd_tp_name(self.ctx, "Home")
            mock_teleport.assert_called_with(
                self.ctx.http_client_mod,
                self.ctx.player.unique_id,
                {"X": 100, "Y": 200, "Z": 300},
                no_vehicles=True,  # Non-admin defaults to True
                reset_trailers=True,
                reset_carried_vehicles=True,
            )

        # Test invalid name (popup)
        with (
            patch(
                "amc.models.TeleportPoint.objects.aget",
                side_effect=TeleportPoint.DoesNotExist,
            ),
            patch("amc.commands.teleport.show_popup", new=AsyncMock()) as mock_popup,
        ):
            await cmd_tp_name(self.ctx, "Invalid")
            mock_popup.assert_called()

        # Test empty name (Usage popup)
        with patch("amc.commands.teleport.show_popup", new=AsyncMock()) as mock_popup:
            await cmd_tp_name(self.ctx, "")
            mock_popup.assert_called()
            args = mock_popup.call_args[0]
            self.assertIn("Choose from one of the following locations", args[1])

    async def test_cmd_tp_vehicle_police(self):
        with (
            patch("amc.commands.teleport.PoliceSession.objects.filter") as mock_filter,
            patch(
                "amc.commands.teleport.enter_last_vehicle",
                new=AsyncMock(return_value={"status": "success"}),
            ) as mock_enter,
        ):
            mock_filter.return_value.aexists = AsyncMock(return_value=True)

            await cmd_tp_vehicle(self.ctx)

            mock_enter.assert_called_with(self.ctx.http_client_mod, "guid-123")

    async def test_cmd_tp_vehicle_not_police(self):
        with (
            patch("amc.commands.teleport.PoliceSession.objects.filter") as mock_filter,
            patch(
                "amc.commands.teleport.enter_last_vehicle", new=AsyncMock()
            ) as mock_enter,
        ):
            mock_filter.return_value.aexists = AsyncMock(return_value=False)

            await cmd_tp_vehicle(self.ctx)

            mock_enter.assert_not_called()
            self.ctx.reply.assert_called()
            self.assertIn("Police Only", self.ctx.reply.call_args[0][0])

    async def test_cmd_donate_flow(self):
        from amc.utils import generate_verification_code

        self.ctx.character.id = 1
        amount = 500
        code = generate_verification_code((amount, self.ctx.character.id))

        # 1. First call without code
        await cmd_donate(self.ctx, "500", "")
        self.ctx.reply.assert_called()
        args, _ = self.ctx.reply.call_args
        self.assertIn("To confirm, type:", args[0])

        # Mock arefresh_from_db
        self.ctx.character.arefresh_from_db = AsyncMock()

        # 2. Second call with code
        with (
            patch(
                "amc.commands.finance.register_player_withdrawal", new=AsyncMock()
            ) as mock_withdraw,
            patch(
                "amc.commands.finance.player_donation", new=AsyncMock()
            ) as mock_donate,
        ):
            await cmd_donate(self.ctx, "500", code)

            mock_withdraw.assert_called_with(500, self.ctx.character, self.ctx.player)
            mock_donate.assert_called_with(500, self.ctx.character)
            self.assertTrue(self.ctx.reply.call_count >= 2)  # Confirm reply sent

    async def test_integration_registry_execute(self):
        """
        Test that the registry actually routes a string to a command function.
        """
        with patch("amc.commands.general.cmd_help", new=AsyncMock()):
            self.ctx.reply.reset_mock()
            result = await registry.execute("/help", self.ctx)
            self.assertTrue(result)
            # Side effect depends on what we mocked.
            # If we mocked cmd_help, stripped of side effects, we just check return True.

    # --- General Info Tests ---

    async def test_cmd_coords(self):
        with patch(
            "amc.commands.general.get_player_info",
            new=AsyncMock(
                return_value={"Location": {"X": 100.5, "Y": 200.5, "Z": 300.5}}
            ),
        ):
            await cmd_coords(self.ctx)
            self.ctx.announce.assert_called_with("100, 200, 300")

    # --- Decal Tests ---

    async def test_cmd_decals(self):
        # Mock VehicleDecal queryset iteration
        mock_decal = MagicMock()
        mock_decal.hash = "1234567890"
        mock_decal.name = "Test Decal"
        mock_decal.vehicle_key = "Jemusi"

        # Async iterator mock is tricky, let's patch objects.filter
        with patch("amc.models.VehicleDecal.objects.filter") as mock_filter:
            mock_qs = MagicMock()
            mock_qs.__aiter__.return_value = [mock_decal]
            mock_filter.return_value = mock_qs

            await cmd_decals(self.ctx)
            self.ctx.reply.assert_called()
            args, _ = self.ctx.reply.call_args
            self.assertIn("Test Decal", args[0])

    async def test_cmd_save_decal(self):
        decal_config = {"some": "config"}
        self.ctx.player_info = {"VehicleKey": "Truck"}

        with (
            patch(
                "amc.commands.decals.get_decal",
                new=AsyncMock(return_value=decal_config),
            ),
            patch("amc.models.VehicleDecal.calculate_hash", return_value="hash123"),
            patch(
                "amc.models.VehicleDecal.objects.acreate", new=AsyncMock()
            ) as mock_create,
        ):
            mock_create.return_value = MagicMock(name="NewDecal", hash="hash123")

            await cmd_save_decal(self.ctx, "MyDecal")

            mock_create.assert_called()
            self.ctx.reply.assert_called()

    async def test_cmd_apply_decal(self):
        mock_decal = MagicMock()
        mock_decal.config = {"color": "red"}

        with (
            patch(
                "amc.models.VehicleDecal.objects.aget",
                new=AsyncMock(return_value=mock_decal),
            ),
            patch("amc.commands.decals.set_decal", new=AsyncMock()) as mock_set,
        ):
            await cmd_apply_decal(self.ctx, "DecalName")
            mock_set.assert_called_with(
                self.ctx.http_client_mod, "76561198000000000", {"color": "red"}
            )

    async def test_cmd_apply_decal_not_found(self):
        from amc.models import VehicleDecal

        async def async_iter(items):
            for i in items:
                yield i

        mock_qs = MagicMock()
        mock_qs.__aiter__.side_effect = lambda: async_iter([])

        with (
            patch(
                "amc.models.VehicleDecal.objects.aget",
                new=AsyncMock(side_effect=VehicleDecal.DoesNotExist),
            ),
            patch("amc.models.VehicleDecal.objects.filter", return_value=mock_qs),
        ):
            await cmd_apply_decal(self.ctx, "Missing")
            self.ctx.reply.assert_called()
            args, _ = self.ctx.reply.call_args
            self.assertIn("Decal not found", args[0])

    # --- Jobs & Economy Tests ---

    async def test_cmd_jobs(self):
        mock_job = MagicMock()
        mock_job.quantity_fulfilled = 0
        mock_job.quantity_requested = 10
        mock_job.name = "Test Job"
        mock_job.bonus_multiplier = 0
        mock_job.completion_bonus = 1000
        mock_job.rp_mode = False
        mock_job.expired_at = self.ctx.timestamp + timedelta(hours=1)
        mock_job.get_cargo_key_display.return_value = "Boxes"

        with patch("amc.models.DeliveryJob.objects.filter") as mock_filter:
            mock_qs = MagicMock()
            mock_qs.prefetch_related.return_value.__aiter__.return_value = [mock_job]
            mock_filter.return_value = mock_qs

            sp_mock = MagicMock(spec=["name"])
            sp_mock.name = "Source A"
            dp_mock = MagicMock(spec=["name"])
            dp_mock.name = "Dest B"
            mock_job.source_points.all.return_value = [sp_mock]
            mock_job.destination_points.all.return_value = [dp_mock]

            with patch(
                "amc.commands.jobs.calculate_treasury_multiplier",
                new=MagicMock(return_value=1.5),
            ):
                await cmd_jobs(self.ctx)
                self.ctx.reply.assert_called()
                args, _ = self.ctx.reply.call_args
                self.assertIn("Test Job", args[0])
                self.assertIn("Source A", args[0])
                self.assertIn("Dest B", args[0])

    async def test_cmd_subsidies(self):
        await cmd_subsidies(self.ctx)
        self.ctx.reply.assert_called()

    # --- Events & Racing Tests ---

    async def test_cmd_staggered_start(self):
        mock_event = MagicMock()

        with patch("amc.models.GameEvent.objects.filter") as mock_filter:
            mock_qs = MagicMock()
            mock_qs.select_related.return_value.alatest = AsyncMock(
                return_value=mock_event
            )
            mock_filter.return_value = mock_qs

            with patch(
                "amc.commands.events.staggered_start", new=AsyncMock()
            ) as mock_start:
                await cmd_staggered_start(self.ctx, 5)
                mock_start.assert_called()

    async def test_cmd_auto_grid(self):
        mock_event = MagicMock()

        with patch("amc.models.GameEvent.objects.filter") as mock_filter:
            mock_qs = MagicMock()
            mock_qs.select_related.return_value.alatest = AsyncMock(
                return_value=mock_event
            )
            mock_filter.return_value = mock_qs

            with patch(
                "amc.commands.events.auto_starting_grid", new=AsyncMock()
            ) as mock_grid:
                await cmd_auto_grid(self.ctx)
                mock_grid.assert_called()

    async def test_cmd_results(self):
        mock_event = MagicMock()

        with patch("amc.models.ScheduledEvent.objects.filter_active_at") as mock_filter:
            mock_qs = MagicMock()
            mock_qs.select_related.return_value.afirst = AsyncMock(
                return_value=mock_event
            )
            mock_filter.return_value = mock_qs

            with patch(
                "amc.commands.events.show_scheduled_event_results_popup",
                new=AsyncMock(),
            ) as mock_popup:
                await cmd_results(self.ctx)
                mock_popup.assert_called()

    async def test_cmd_setup_event(self):
        """When called without event_id, lists all events with race setups."""
        mock_event = MagicMock()
        mock_event.id = 42
        mock_event.name = "Sunday Race"
        mock_event.description = "A fun race"
        mock_event.description_in_game = ""

        with patch("amc.models.ScheduledEvent.objects.filter") as mock_filter:
            mock_qs = MagicMock()
            mock_qs.select_related.return_value.order_by.return_value.__aiter__.return_value = [
                mock_event
            ]
            mock_filter.return_value = mock_qs

            await cmd_setup_event(self.ctx)
            self.ctx.reply.assert_called()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("#42 Sunday Race", output)
            self.assertIn("/setup_event 42", output)
            self.assertIn("<Small>A fun race</>", output)

    async def test_cmd_setup_event_with_id(self):
        """When called with event_id, sets up that event."""
        mock_event = MagicMock()

        with patch("amc.models.ScheduledEvent.objects.select_related") as mock_sr:
            mock_sr.return_value.filter.return_value.aget = AsyncMock(
                return_value=mock_event
            )

            with patch(
                "amc.commands.events.setup_event", new=AsyncMock(return_value=True)
            ) as mock_setup:
                await cmd_setup_event(self.ctx, event_id=42)
                mock_setup.assert_called()

    async def test_cmd_events_list(self):
        mock_event = MagicMock()
        mock_event.name = "Race"
        mock_event.start_time = self.ctx.timestamp + timedelta(hours=1)
        mock_event.end_time = self.ctx.timestamp + timedelta(hours=2)
        mock_event.description = "Test Description"

        with patch("amc.models.ScheduledEvent.objects.filter") as mock_filter:
            mock_qs = MagicMock()
            mock_qs.order_by.return_value.__aiter__.return_value = [mock_event]
            mock_filter.return_value = mock_qs

            await cmd_events_list(self.ctx)
            self.ctx.reply.assert_called()
            self.assertIn("Race", self.ctx.reply.call_args[0][0])

    async def test_cmd_countdown(self):
        with patch("amc.commands.events.countdown", new=AsyncMock()) as mock_cd:
            await cmd_countdown(self.ctx)
            mock_cd.assert_called()

    # --- RP Mode & Rescue Tests ---

    async def test_cmd_rescue_cooldown(self):
        with patch("amc.models.RescueRequest.objects.filter") as mock_filter:
            mock_filter.return_value.aexists = AsyncMock(
                return_value=True
            )  # Recently requested

            await cmd_rescue(self.ctx, "Help!")
            self.ctx.reply.assert_called_with(
                "You have requested a rescue less than 5 minutes ago"
            )

    async def test_cmd_rescue_success(self):
        mock_req = MagicMock()
        mock_req.id = 123
        self.ctx.is_current_event = True

        with (
            patch("amc.models.RescueRequest.objects.filter") as mock_filter,
            patch(
                "amc.commands.rp_rescue.get_players_mod", new=AsyncMock(return_value=[])
            ),
            patch(
                "amc.commands.rp_rescue.get_player_last_vehicle",
                new=AsyncMock(return_value={"vehicle": None}),
            ),
            patch(
                "amc.models.RescueRequest.objects.acreate",
                new=AsyncMock(return_value=mock_req),
            ),
        ):
            mock_filter.return_value.aexists = AsyncMock(return_value=False)

            await cmd_rescue(self.ctx, "Help!")
            self.ctx.announce.assert_called()
            self.ctx.reply.assert_called()

    async def test_cmd_respond(self):
        mock_req = MagicMock()
        mock_req.discord_message_id = 123

        # Setup discord mocks
        mock_cog = MagicMock()
        mock_cog.add_reaction_to_rescue_message = (
            AsyncMock()
        )  # ERR FIX: needs to return coroutine
        self.ctx.discord_client = MagicMock()
        self.ctx.discord_client.get_cog.return_value = mock_cog
        self.ctx.discord_client.loop = (
            MagicMock()
        )  # Needs loop for run_coroutine_threadsafe

        with (
            patch("amc.models.RescueRequest.objects.select_related") as mock_sr,
            patch("asyncio.run_coroutine_threadsafe") as mock_run,
        ):
            mock_sr.return_value.aget = AsyncMock(return_value=mock_req)
            mock_req.responders.aadd = AsyncMock()  # ERR FIX

            await cmd_respond(self.ctx, 123)
            mock_req.responders.aadd.assert_called_with(self.ctx.player)

            # Handle unawaited coroutine from run_coroutine_threadsafe
            if mock_run.called:
                coro = mock_run.call_args[0][0]
                coro.close()
            self.ctx.announce.assert_called()

    async def test_rescue_reminder_sends_reminder(self):
        from amc.rescue_reminder import send_rescue_reminders

        mock_req = MagicMock()
        mock_req.timestamp = timezone.now() - timedelta(minutes=2)
        mock_req.last_reminded_at = None
        mock_req.character.name = "TestChar"
        mock_req.id = 42
        mock_req.message = "Help!"
        mock_req.asave = AsyncMock()

        with (
            patch("amc.rescue_reminder.RescueRequest.objects.filter") as mock_filter,
            patch("amc.rescue_reminder.announce", new=AsyncMock()) as mock_announce,
            patch("amc.rescue_reminder.forward_to_discord", new=AsyncMock()),
        ):
            mock_filter.return_value.select_related.return_value.order_by.return_value = [
                mock_req
            ]

            ctx = {"http_client": MagicMock(), "discord_client": None}
            await send_rescue_reminders(ctx)
            mock_announce.assert_called()
            mock_req.asave.assert_called()

    async def test_rescue_reminder_no_open_requests(self):
        from amc.rescue_reminder import send_rescue_reminders

        with (
            patch("amc.rescue_reminder.RescueRequest.objects.filter") as mock_filter,
            patch("amc.rescue_reminder.announce", new=AsyncMock()) as mock_announce,
        ):
            mock_filter.return_value.select_related.return_value.order_by.return_value = []

            ctx = {"http_client": MagicMock(), "discord_client": None}
            await send_rescue_reminders(ctx)
            mock_announce.assert_not_called()

    # --- Admin & Spawning Tests ---

    async def test_cmd_tp_player(self):
        from amc.models import TeleportPoint

        self.ctx.player_info["bIsAdmin"] = True

        # Mock Data with Multiple Players to test fuzzy selection
        mock_p1 = {
            "name": "TargetPlayer",
            "character_guid": "guid-1",
            "player_id": "pid-1",
        }
        mock_p2 = {
            "name": "TargetDummy",
            "character_guid": "guid-2",
            "player_id": "pid-2",
        }
        mock_p3 = {
            "name": "OtherPerson",
            "character_guid": "guid-3",
            "player_id": "pid-3",
        }

        # get_players returns a list of tuples (unique_id, player_dict)
        mock_players = [("pid-1", mock_p1), ("pid-2", mock_p2), ("pid-3", mock_p3)]

        mock_tp = MagicMock()
        mock_tp.location.x = 100
        mock_tp.location.y = 200
        mock_tp.location.z = 300

        # Test Successful Teleport (Exact)
        with (
            patch(
                "amc.commands.admin.get_players",
                new=AsyncMock(return_value=mock_players),
            ),
            patch(
                "amc.models.TeleportPoint.objects.aget",
                new=AsyncMock(return_value=mock_tp),
            ),
            patch(
                "amc.commands.admin.teleport_player", new=AsyncMock()
            ) as mock_teleport,
        ):
            await cmd_tp_player(self.ctx, "TargetPlayer", "Home")

            mock_teleport.assert_called_with(
                self.ctx.http_client_mod,
                "pid-1",
                {"X": 100, "Y": 200, "Z": 300},
                no_vehicles=False,
                reset_trailers=False,
                reset_carried_vehicles=False,
            )

        # Test Successful Teleport (Fuzzy - "TargetP" should match "TargetPlayer" better than "TargetDummy")
        with (
            patch(
                "amc.commands.admin.get_players",
                new=AsyncMock(return_value=mock_players),
            ),
            patch(
                "amc.models.TeleportPoint.objects.aget",
                new=AsyncMock(return_value=mock_tp),
            ),
            patch(
                "amc.commands.admin.teleport_player", new=AsyncMock()
            ) as mock_teleport,
        ):
            await cmd_tp_player(self.ctx, "TargetP", "Home")

            mock_teleport.assert_called_with(
                self.ctx.http_client_mod,
                "pid-1",
                {"X": 100, "Y": 200, "Z": 300},
                no_vehicles=False,
                reset_trailers=False,
                reset_carried_vehicles=False,
            )

        # Test Successful Teleport (Fuzzy - "Dummy" should match "TargetDummy")
        with (
            patch(
                "amc.commands.admin.get_players",
                new=AsyncMock(return_value=mock_players),
            ),
            patch(
                "amc.models.TeleportPoint.objects.aget",
                new=AsyncMock(return_value=mock_tp),
            ),
            patch(
                "amc.commands.admin.teleport_player", new=AsyncMock()
            ) as mock_teleport,
        ):
            await cmd_tp_player(self.ctx, "Dummy", "Home")

            mock_teleport.assert_called_with(
                self.ctx.http_client_mod,
                "pid-2",
                {"X": 100, "Y": 200, "Z": 300},
                no_vehicles=False,
                reset_trailers=False,
                reset_carried_vehicles=False,
            )

        # Test Player Not Found
        with (
            patch("amc.commands.admin.get_players", new=AsyncMock(return_value={})),
            patch("amc.commands.admin.show_popup", new=AsyncMock()) as mock_popup,
        ):
            await cmd_tp_player(self.ctx, "Ghost", "Home")
            mock_popup.assert_called()
            self.assertIn("Player not found", mock_popup.call_args[0][1])

        # Test Location Not Found
        with (
            patch(
                "amc.commands.admin.get_players",
                new=AsyncMock(return_value=mock_players),
            ),
            patch(
                "amc.models.TeleportPoint.objects.aget",
                side_effect=TeleportPoint.DoesNotExist,
            ),
            patch("amc.commands.admin.show_popup", new=AsyncMock()) as mock_popup,
        ):
            await cmd_tp_player(self.ctx, "Target", "InvalidLoc")
            mock_popup.assert_called()
            self.assertIn("Teleport point not found", mock_popup.call_args[0][1])

    # --- Bill Command Tests ---

    async def test_cmd_bill_non_admin(self):
        self.ctx.player_info["bIsAdmin"] = False
        with patch(
            "amc.commands.admin.transfer_money", new=AsyncMock()
        ) as mock_transfer:
            await cmd_bill(self.ctx, "SomePlayer")
            mock_transfer.assert_not_called()

    async def test_cmd_bill_player_not_found(self):
        self.ctx.player_info["bIsAdmin"] = True
        with (
            patch(
                "amc.commands.admin.get_players",
                new=AsyncMock(return_value=[]),
            ),
            patch("amc.commands.admin.show_popup", new=AsyncMock()) as mock_popup,
        ):
            await cmd_bill(self.ctx, "Ghost")
            mock_popup.assert_called()
            self.assertIn("Player not found", mock_popup.call_args[0][1])

    async def test_cmd_bill_success(self):
        self.ctx.player_info["bIsAdmin"] = True
        target_char = MagicMock()
        target_char.name = "BillTarget"
        target_char.driver_level = 400
        target_char.gov_employee_contributions = 0
        target_char.asave = AsyncMock()

        mock_players = [
            ("pid-bill", {"name": "BillTarget", "character_guid": "guid-bill-target"}),
        ]

        with (
            patch(
                "amc.commands.admin.get_players",
                new=AsyncMock(return_value=mock_players),
            ),
            patch(
                "amc.commands.admin.transfer_money", new=AsyncMock()
            ) as mock_transfer,
            patch(
                "amc.commands.admin.player_donation", new=AsyncMock()
            ) as mock_donation,
            patch(
                "amc.commands.admin.Character.objects.aget",
                new=AsyncMock(return_value=target_char),
            ),
        ):
            await cmd_bill(self.ctx, "BillTarget")

            # Full amount at max level
            mock_transfer.assert_called_with(
                self.ctx.http_client_mod, -50_000, "Public service bill", "pid-bill"
            )
            mock_donation.assert_called_with(
                50_000, target_char, description="Public service bill"
            )
            target_char.asave.assert_called_with(
                update_fields=["gov_employee_contributions"]
            )
            self.ctx.reply.assert_called()
            self.ctx.announce.assert_called()

    async def test_cmd_bill_scales_by_driver_level(self):
        self.ctx.player_info["bIsAdmin"] = True
        target_char = MagicMock()
        target_char.name = "HalfLevel"
        target_char.driver_level = 200  # half of MAX_LEVEL=400
        target_char.gov_employee_contributions = 0
        target_char.asave = AsyncMock()

        mock_players = [
            ("pid-half", {"name": "HalfLevel", "character_guid": "guid-half-level"}),
        ]

        with (
            patch(
                "amc.commands.admin.get_players",
                new=AsyncMock(return_value=mock_players),
            ),
            patch(
                "amc.commands.admin.transfer_money", new=AsyncMock()
            ) as mock_transfer,
            patch(
                "amc.commands.admin.player_donation", new=AsyncMock()
            ) as mock_donation,
            patch(
                "amc.commands.admin.Character.objects.aget",
                new=AsyncMock(return_value=target_char),
            ),
        ):
            await cmd_bill(self.ctx, "HalfLevel")

            # Half level => 25,000
            mock_transfer.assert_called_with(
                self.ctx.http_client_mod, -25_000, "Public service bill", "pid-half"
            )
            mock_donation.assert_called_with(
                25_000, target_char, description="Public service bill"
            )

    async def test_cmd_bill_no_driver_level(self):
        self.ctx.player_info["bIsAdmin"] = True
        target_char = MagicMock()
        target_char.name = "NoLevel"
        target_char.driver_level = None
        target_char.asave = AsyncMock()

        mock_players = [
            ("pid-nolevel", {"name": "NoLevel", "character_guid": "guid-no-level"}),
        ]

        with (
            patch(
                "amc.commands.admin.get_players",
                new=AsyncMock(return_value=mock_players),
            ),
            patch(
                "amc.commands.admin.transfer_money", new=AsyncMock()
            ) as mock_transfer,
            patch(
                "amc.commands.admin.Character.objects.aget",
                new=AsyncMock(return_value=target_char),
            ),
        ):
            await cmd_bill(self.ctx, "NoLevel")
            mock_transfer.assert_not_called()
            self.ctx.reply.assert_called()
            self.assertIn("no driver level", self.ctx.reply.call_args[0][0])

    async def test_cmd_spawn_displays(self):
        self.ctx.player_info["bIsAdmin"] = True
        mock_v = MagicMock()
        mock_v.id = 1
        mock_v.character = MagicMock()  # has character

        with (
            patch("amc.models.CharacterVehicle.objects.select_related") as mock_qs,
            patch("amc.commands.admin.despawn_by_tag", new=AsyncMock()),
            patch(
                "amc.commands.admin.spawn_registered_vehicle", new=AsyncMock()
            ) as mock_spawn,
        ):
            mock_qs.return_value.filter.return_value.__aiter__.return_value = [mock_v]

            await cmd_spawn_displays(self.ctx)
            mock_spawn.assert_called()

    async def test_cmd_spawn_dealerships(self):
        self.ctx.player_info["bIsAdmin"] = True
        mock_vd = MagicMock()
        mock_vd.spawn = AsyncMock()  # ERR FIX

        with patch("amc.models.VehicleDealership.objects.filter") as mock_filter:
            mock_filter.return_value.__aiter__.return_value = [mock_vd]

            await cmd_spawn_dealerships(self.ctx)
            mock_vd.spawn.assert_called()

    async def test_cmd_spawn_assets(self):
        self.ctx.player_info["bIsAdmin"] = True
        with (
            patch("amc.models.WorldText.objects.all") as mock_wt,
            patch("amc.models.WorldObject.objects.all") as mock_wo,
            patch("amc.commands.admin.spawn_assets", new=AsyncMock()) as mock_spawn,
        ):
            mock_wt.return_value.__aiter__.return_value = [MagicMock()]
            mock_wo.return_value.__aiter__.return_value = [MagicMock()]

            await cmd_spawn_assets(self.ctx)
            # Should be called twice (looping through mock iterables)
            mock_spawn.assert_called()

    async def test_cmd_spawn_garages(self):
        self.ctx.player_info["bIsAdmin"] = True
        mock_g = MagicMock()
        mock_g.config = {"Location": {}, "Rotation": {}}
        mock_g.asave = AsyncMock()  # ERR FIX

        with (
            patch("amc.models.Garage.objects.filter") as mock_filter,
            patch(
                "amc.commands.admin.spawn_garage",
                new=AsyncMock(return_value={"tag": "t"}),
            ),
        ):
            mock_filter.return_value.__aiter__.return_value = [mock_g]

            await cmd_spawn_garages(self.ctx)
            mock_g.asave.assert_called()

    async def test_cmd_spawn_garage_single(self):
        self.ctx.player_info["bIsAdmin"] = True
        self.ctx.player_info["Location"] = {"X": 0, "Y": 0, "Z": 0}

        with (
            patch(
                "amc.commands.admin.spawn_garage",
                new=AsyncMock(return_value={"tag": "t"}),
            ),
            patch("amc.models.Garage.objects.acreate", new=AsyncMock()) as mock_create,
        ):
            await cmd_spawn_garage_single(self.ctx, "MyGarage")
            mock_create.assert_called()
            self.ctx.announce.assert_called()

    async def test_cmd_spawn(self):
        self.ctx.player_info["bIsAdmin"] = True
        self.ctx.player_info["Location"] = {"X": 0, "Y": 0, "Z": 0}

        # Test numeric ID (existing vehicle)
        with (
            patch(
                "amc.models.CharacterVehicle.objects.aget",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "amc.commands.admin.spawn_registered_vehicle", new=AsyncMock()
            ) as mock_srv,
        ):
            await cmd_spawn(self.ctx, "123")
            mock_srv.assert_called()

        # Test string (raw spawn)
        with patch("amc.commands.admin.spawn_vehicle", new=AsyncMock()) as mock_sv:
            await cmd_spawn(self.ctx, "Truck")
            mock_sv.assert_called()

    # --- Vehicle Management Tests ---

    # --- Teleport Tests ---

    async def test_cmd_exit(self):
        self.ctx.player_info["bIsAdmin"] = True
        players_list = [{"PlayerName": "Target", "CharacterGuid": "guid1"}]

        with (
            patch(
                "amc.commands.admin.get_players_mod",
                new=AsyncMock(return_value=players_list),
            ),
            patch(
                "amc.commands.admin.force_exit_vehicle", new=AsyncMock()
            ) as mock_force,
        ):
            await cmd_exit(self.ctx, "Target")
            mock_force.assert_called_with(self.ctx.http_client_mod, "guid1")

    # --- Finance Tests ---

    async def test_cmd_withdraw(self):
        with (
            patch(
                "amc.commands.finance.with_verification_code",
                return_value=("CODE", False),
            ),
            patch("amc.commands.finance.register_player_withdrawal", new=AsyncMock()),
            patch(
                "amc.commands.finance.transfer_money", new=AsyncMock()
            ) as mock_transfer,
        ):
            await cmd_withdraw(self.ctx, "100")
            mock_transfer.assert_called()

    async def test_cmd_loan(self):
        with (
            patch(
                "amc.commands.finance.get_player_loan_balance",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "amc.commands.finance.get_character_max_loan",
                new=AsyncMock(return_value=(1000, "Ok")),
            ),
            patch(
                "amc.commands.finance.register_player_take_loan",
                new=AsyncMock(return_value=(1100, 100)),
            ),
            patch(
                "amc.commands.finance.transfer_money", new=AsyncMock()
            ) as mock_transfer,
            patch("amc.models.Delivery.objects.filter") as mock_del_filter,
        ):
            # Case 1: No deliveries
            mock_del_filter.return_value.aexists = AsyncMock(return_value=False)
            await cmd_loan(self.ctx, "500", "CODE")
            self.ctx.announce.assert_called_with(
                "You must have done at least one delivery"
            )

            mock_del_filter.return_value.aexists = AsyncMock(return_value=True)
            from amc.utils import generate_verification_code

            code = generate_verification_code((500, self.ctx.character.id))
            await cmd_loan(self.ctx, "500", code)
            mock_transfer.assert_called()

    async def test_cmd_thank(self):
        mock_p = {"name": "Other", "character_guid": "guid2"}
        mock_char = MagicMock()

        with (
            patch(
                "amc.commands.social.get_players",
                new=AsyncMock(return_value=[(1, mock_p)]),
            ),
            patch(
                "amc.models.Character.objects.aget",
                new=AsyncMock(return_value=mock_char),
            ),
            patch("amc.models.Thank.objects.filter") as mock_t_filter,
            patch("amc.models.Thank.objects.acreate", new=AsyncMock()) as mock_create,
        ):
            # Cooldown check
            mock_t_filter.return_value.aexists = AsyncMock(return_value=False)

            # Mock update and system message
            with (
                patch("amc.models.Player.objects.filter") as mock_p_filter,
                patch(
                    "amc.commands.social.send_system_message", new=AsyncMock()
                ) as mock_sys_msg,
            ):
                mock_p_filter.return_value.aupdate = AsyncMock()

                await cmd_thank(self.ctx, "Other")
                mock_create.assert_called()
                mock_p_filter.return_value.aupdate.assert_called()  # Check for social score update
                self.assertEqual(
                    mock_sys_msg.call_count, 2
                )  # Check for 2 system messages

    async def test_cmd_set_rates(self):
        with patch("amc.commands.finance.show_popup", new=AsyncMock()) as mock_popup:
            await cmd_set_saving_rate(self.ctx, "10%")
            self.assertEqual(float(round(self.ctx.character.saving_rate, 1)), 0.1)
            mock_popup.assert_called()

            mock_popup.reset_mock()
            await cmd_set_repayment_rate(self.ctx, "20%")
            self.assertEqual(
                float(round(self.ctx.character.loan_repayment_rate, 1)), 0.2
            )
            mock_popup.assert_called()

    async def test_cmd_toggle_ubi(self):
        initial = self.ctx.character.reject_ubi
        with patch("amc.commands.finance.show_popup", new=AsyncMock()) as mock_popup:
            await cmd_toggle_ubi(self.ctx)
            self.assertNotEqual(initial, self.ctx.character.reject_ubi)
            mock_popup.assert_called()

    # --- Misc Tests ---

    async def test_cmd_verify(self):
        self.ctx.discord_client = MagicMock()
        self.ctx.discord_client.loop = MagicMock()

        with (
            patch(
                "amc.commands.general.verify_player",
                new=AsyncMock(return_value="discord_id"),
            ),
            patch("amc.commands.general.add_discord_verified_role", new=AsyncMock()),
            patch("amc.commands.general.show_popup", new=AsyncMock()) as mock_popup,
            patch("asyncio.run_coroutine_threadsafe") as mock_run_coro,
        ):
            # Need to setup run_coroutine_threadsafe to await the coro or just check it's called?
            # Since it's fire-and-forget in cmd, we just verify call.

            await cmd_verify(self.ctx, "sig")

            mock_run_coro.assert_called()
            mock_popup.assert_called()
            self.assertIn("You are now verified", mock_popup.call_args[0][1])

            # Since mock_run_coro is called with the coroutine, we can't easily assert mock_role was awaited unless we execute it.
            # But the existence of the call in code is verified by run_coroutine_threadsafe.

    async def test_cmd_rename(self):
        with patch(
            "amc.commands.general.set_character_name", new=AsyncMock()
        ) as mock_set:
            await cmd_rename(self.ctx, "NewName")
            self.assertEqual(self.ctx.character.custom_name, "NewName")
            mock_set.assert_called()

    async def test_cmd_bot(self):
        with patch(
            "amc.models.BotInvocationLog.objects.acreate", new=AsyncMock()
        ) as mock_log:
            await cmd_bot(self.ctx, "prompt")
            mock_log.assert_called()

    async def test_cmd_song_request(self):
        with patch(
            "amc.models.SongRequestLog.objects.acreate", new=AsyncMock()
        ) as mock_log:
            # Case 1: No event
            self.ctx.is_current_event = False
            await cmd_song_request(self.ctx, "song")
            mock_log.assert_called()
            self.ctx.reply.assert_called_with("Song request received")

            # Case 2: Event
            self.ctx.is_current_event = True
            with patch(
                "amc.commands.general.show_popup", new=AsyncMock()
            ) as mock_popup:
                await cmd_song_request(self.ctx, "song")
                mock_popup.assert_called()

    # --- Moved Legacy Commands Tests ---

    async def test_cmd_burn(self):
        from amc.utils import generate_verification_code

        amount = 100
        code = generate_verification_code((amount, self.ctx.character.id))

        with (
            patch(
                "amc.commands.finance.transfer_money", new=AsyncMock()
            ) as mock_transfer,
            patch("amc.commands.finance.show_popup", new=AsyncMock()) as mock_popup,
        ):
            # 1. No code
            await cmd_burn(self.ctx, "100", "")
            # It uses show_popup, so no reply
            mock_popup.assert_called()
            mock_popup.reset_mock()

            # 2. Correct code
            await cmd_burn(self.ctx, "100", code)
            mock_transfer.assert_called_with(
                self.ctx.http_client_mod, -100, "Burn", str(self.ctx.player.unique_id)
            )

    async def test_cmd_repay_loan_deprecated(self):
        with patch("amc.commands.finance.show_popup", new=AsyncMock()) as mock_popup:
            await cmd_repay_loan(self.ctx)
            mock_popup.assert_called()
            self.assertIn("Command Removed", mock_popup.call_args[0][1])

    async def test_cmd_language_list(self):
        await cmd_language(self.ctx)
        self.ctx.reply.assert_called()
        args, _ = self.ctx.reply.call_args
        self.assertIn("Available languages", args[0])

    async def test_cmd_language_set(self):
        await cmd_language(self.ctx, "id")
        await self.player.arefresh_from_db()
        self.assertEqual(self.player.language, "id")
        self.ctx.reply.assert_called()
        args, _ = self.ctx.reply.call_args
        self.assertIn("id", args[0])

    async def test_registry_translation_override(self):
        from amc.command_framework import registry
        from django.utils import translation

        self.player.language = "id"
        await self.player.asave()

        current_lang_inside = None

        async def mock_cmd(ctx):
            nonlocal current_lang_inside
            current_lang_inside = translation.get_language()

        @registry.register("/test_lang")
        async def test_lang_func(ctx):
            await mock_cmd(ctx)

        try:
            await registry.execute("/test_lang", self.ctx)
            self.assertEqual(current_lang_inside, "id")
        finally:
            registry.commands = [
                c for c in registry.commands if c["name"] != "/test_lang"
            ]

    async def test_indonesian_translation_output(self):
        from amc.command_framework import registry

        # Set player language to Indonesian
        self.player.language = "id"
        await self.player.asave()

        # We need a command that returns a translated string.
        # /thank when player not found returns "Player not found"
        # In id: "Pemain tidak ditemukan"

        with patch("amc.commands.social.get_players", new=AsyncMock(return_value=[])):
            await registry.execute("/thank NoSuchPlayer", self.ctx)

            self.ctx.reply.assert_called()
            args, _ = self.ctx.reply.call_args
            # Verify the output is in Indonesian
            self.assertEqual(args[0], "Pemain tidak ditemukan")

    async def test_help_command_translation_output(self):
        from amc.command_framework import registry

        self.player.language = "id"
        await self.player.asave()

        await registry.execute("/help", self.ctx)

        self.ctx.reply.assert_called()
        args, _ = self.ctx.reply.call_args

        # Check for Indonesian translation of "Available Commands"
        # self.assertIn("Perintah Tersedia", msg)

        # Check for Indonesian translation of a command description, e.g. /register_vehicles
        # "Register your vehicles" -> "Daftarkan kendaraan Anda"
        # self.assertIn("Daftarkan kendaraan Anda", msg)

    async def test_cmd_help_shows_all_for_admin(self):
        self.ctx.player_info["bIsAdmin"] = True

        mock_commands = [
            {
                "name": "/general",
                "aliases": ["/general"],
                "description": "Gen",
                "category": "General",
            },
            {
                "name": "/admin",
                "aliases": ["/admin"],
                "description": "Adm",
                "category": "Admin",
            },
        ]

        with patch("amc.command_framework.registry.commands", mock_commands):
            await cmd_help(self.ctx)

            self.ctx.reply.assert_called()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("/general", output)
            self.assertIn("/admin", output)

    async def test_cmd_help_hides_admin_for_non_admin(self):
        self.ctx.player_info["bIsAdmin"] = False

        mock_commands = [
            {
                "name": "/general",
                "aliases": ["/general"],
                "description": "Gen",
                "category": "General",
            },
            {
                "name": "/admin",
                "aliases": ["/admin"],
                "description": "Adm",
                "category": "Admin",
            },
        ]

        with patch("amc.command_framework.registry.commands", mock_commands):
            await cmd_help(self.ctx)

            self.ctx.reply.assert_called()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("/general", output)
            self.assertNotIn("/admin", output)

    # --- Check Mods Tests ---

    async def test_cmd_check_mods_self(self):
        """When no target name is given, checks the caller's own vehicle."""

        mock_last_vehicle = {
            "vehicle": {
                "vehicleId": 1001,
                "fullName": "Jemusi_C Default__Jemusi",
                "classFullName": "Class /Game/Vehicles/Jemusi",
            }
        }
        mock_parts = {
            "vehicleId": 1001,
            "parts": [
                {"Key": "StockEngine", "Slot": 0},
                {"Key": "CustomTurbo_XYZ", "Slot": 5},
            ],
        }

        with (
            patch(
                "amc.commands.vehicles.get_player_last_vehicle",
                new=AsyncMock(return_value=mock_last_vehicle),
            ),
            patch(
                "amc.commands.vehicles.get_player_last_vehicle_parts",
                new=AsyncMock(return_value=mock_parts),
            ),
            patch(
                "amc.commands.vehicles.detect_custom_parts",
                return_value=[
                    {"key": "CustomTurbo_XYZ", "slot": "Turbocharger", "slot_value": 5}
                ],
            ),
            patch(
                "amc.commands.vehicles.detect_incompatible_parts",
                return_value=[],
            ),
        ):
            await cmd_check_mods(self.ctx)

            self.ctx.reply.assert_called()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("Mod Check", output)
            self.assertIn("CustomTurbo_XYZ", output)

    async def test_cmd_check_mods_target(self):
        """When an admin gives a target name, fuzzy finds the player and checks their vehicle."""
        self.ctx.player_info["bIsAdmin"] = True

        mock_players = [("pid-99", {"name": "SomePlayer"})]
        mock_last_vehicle = {
            "vehicle": {
                "vehicleId": 2002,
                "fullName": "Miramar_C Default__Miramar",
                "classFullName": "Class /Game/Vehicles/Miramar",
            }
        }
        mock_parts = {
            "vehicleId": 2002,
            "parts": [{"Key": "StockBrake", "Slot": 1}],
        }

        with (
            patch(
                "amc.commands.vehicles.get_players",
                new=AsyncMock(return_value=mock_players),
            ),
            patch(
                "amc.commands.vehicles.get_player_last_vehicle",
                new=AsyncMock(return_value=mock_last_vehicle),
            ),
            patch(
                "amc.commands.vehicles.get_player_last_vehicle_parts",
                new=AsyncMock(return_value=mock_parts),
            ),
            patch(
                "amc.commands.vehicles.detect_custom_parts",
                return_value=[],
            ),
            patch(
                "amc.commands.vehicles.detect_incompatible_parts",
                return_value=[],
            ),
        ):
            await cmd_check_mods(self.ctx, "SomePlayer")

            self.ctx.reply.assert_called()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("Parts Check", output)
            self.assertIn("All stock parts", output)

    async def test_cmd_check_mods_no_vehicle(self):
        """When player has no active vehicle, shows appropriate message."""
        with (
            patch(
                "amc.commands.vehicles.get_player_last_vehicle",
                new=AsyncMock(return_value={"vehicle": None}),
            ),
            patch(
                "amc.commands.vehicles.get_player_last_vehicle_parts",
                new=AsyncMock(return_value={"parts": []}),
            ),
        ):
            await cmd_check_mods(self.ctx)

            self.ctx.reply.assert_called()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("No Vehicle", output)
            self.assertIn("no active vehicle", output)

    async def test_cmd_check_mods_non_admin(self):
        """Non-admin users can also use check_mods."""
        self.ctx.player_info["bIsAdmin"] = False

        mock_last_vehicle = {
            "vehicle": {
                "vehicleId": 3003,
                "fullName": "Jemusi_C Default__Jemusi",
                "classFullName": "Class /Game/Vehicles/Jemusi",
            }
        }
        mock_parts = {
            "vehicleId": 3003,
            "parts": [{"Key": "StockEngine", "Slot": 0}],
        }

        with (
            patch(
                "amc.commands.vehicles.get_player_last_vehicle",
                new=AsyncMock(return_value=mock_last_vehicle),
            ),
            patch(
                "amc.commands.vehicles.get_player_last_vehicle_parts",
                new=AsyncMock(return_value=mock_parts),
            ),
            patch(
                "amc.commands.vehicles.detect_custom_parts",
                return_value=[],
            ),
            patch(
                "amc.commands.vehicles.detect_incompatible_parts",
                return_value=[],
            ),
        ):
            await cmd_check_mods(self.ctx)
            self.ctx.reply.assert_called()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("Parts Check", output)

    async def test_cmd_check_mods_self_applies_mod_tag(self):
        """When custom parts are found on caller's own vehicle, refresh_player_name is called with has_custom_parts=True."""

        mock_last_vehicle = {
            "vehicle": {
                "vehicleId": 1001,
                "fullName": "Jemusi_C Default__Jemusi",
                "classFullName": "Class /Game/Vehicles/Jemusi",
            }
        }
        mock_parts = {
            "vehicleId": 1001,
            "parts": [
                {"Key": "CustomTurbo_XYZ", "Slot": 5},
            ],
        }

        with (
            patch(
                "amc.commands.vehicles.get_player_last_vehicle",
                new=AsyncMock(return_value=mock_last_vehicle),
            ),
            patch(
                "amc.commands.vehicles.get_player_last_vehicle_parts",
                new=AsyncMock(return_value=mock_parts),
            ),
            patch(
                "amc.commands.vehicles.detect_custom_parts",
                return_value=[
                    {"key": "CustomTurbo_XYZ", "slot": "Turbocharger", "slot_value": 5}
                ],
            ),
            patch(
                "amc.commands.vehicles.detect_incompatible_parts",
                return_value=[],
            ),
            patch(
                "amc.commands.vehicles.refresh_player_name", new=AsyncMock()
            ) as mock_refresh,
        ):
            await cmd_check_mods(self.ctx)

            mock_refresh.assert_awaited_once_with(
                self.ctx.character, self.ctx.http_client_mod, has_custom_parts=True
            )

    async def test_cmd_check_mods_self_removes_mod_tag(self):
        """When no custom parts are found on caller's own vehicle, refresh_player_name is called with has_custom_parts=False."""

        mock_last_vehicle = {
            "vehicle": {
                "vehicleId": 3003,
                "fullName": "Jemusi_C Default__Jemusi",
                "classFullName": "Class /Game/Vehicles/Jemusi",
            }
        }
        mock_parts = {
            "vehicleId": 3003,
            "parts": [{"Key": "StockEngine", "Slot": 0}],
        }

        with (
            patch(
                "amc.commands.vehicles.get_player_last_vehicle",
                new=AsyncMock(return_value=mock_last_vehicle),
            ),
            patch(
                "amc.commands.vehicles.get_player_last_vehicle_parts",
                new=AsyncMock(return_value=mock_parts),
            ),
            patch(
                "amc.commands.vehicles.detect_custom_parts",
                return_value=[],
            ),
            patch(
                "amc.commands.vehicles.detect_incompatible_parts",
                return_value=[],
            ),
            patch(
                "amc.commands.vehicles.refresh_player_name", new=AsyncMock()
            ) as mock_refresh,
        ):
            await cmd_check_mods(self.ctx)

            mock_refresh.assert_awaited_once_with(
                self.ctx.character, self.ctx.http_client_mod, has_custom_parts=False
            )

    # --- Check Parts Tests (wider-scope popup + powercalc integration) ---

    async def test_cmd_check_parts_self_full_popup(self):
        """Power block + drivetrain + inline [unknown] marker + flags line."""
        mock_last_vehicle = {
            "vehicle": {
                "vehicleId": 1001,
                "fullName": "Elisa2_C Default__Elisa2",
                "classFullName": "Class /Game/Cars/Models/Elisa2",
                "DriveInfo": {
                    "drive_type": "RWD",
                    "driven_wheel_count": 2,
                    "total_wheel_count": 4,
                    "total_axle_count": 2,
                    "driven_axle_indices": [1],
                },
            }
        }
        mock_parts = {
            "vehicleId": 1001,
            "parts": [
                {"Key": "SmallBlock_240HP", "Slot": 2},
                {"Key": "201", "Slot": 5},
                {"Key": "Turbocharger_Stage1", "Slot": 7},
                {"Key": "CustomTurbo_XYZ", "Slot": 8},
            ],
        }

        with (
            patch(
                "amc.commands.vehicles.get_player_last_vehicle",
                new=AsyncMock(return_value=mock_last_vehicle),
            ),
            patch(
                "amc.commands.vehicles.get_player_last_vehicle_parts",
                new=AsyncMock(return_value=mock_parts),
            ),
            patch(
                "amc.commands.vehicles.detect_custom_parts",
                return_value=[
                    {"key": "CustomTurbo_XYZ", "slot": "LSD0", "slot_value": 8}
                ],
            ),
            patch(
                "amc.commands.vehicles.detect_incompatible_parts",
                return_value=[],
            ),
            patch(
                "amc.commands.vehicles.refresh_player_name", new=AsyncMock()
            ) as mock_refresh,
        ):
            await cmd_check_parts(self.ctx)

            self.ctx.reply.assert_called()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("Parts Check", output)
            # power block: validated golden dyno build (SmallBlock_240HP+201+Stage1)
            self.assertIn("Power: 293.2 hp @ 6,216 rpm", output)
            self.assertIn("413.0 Nm @ 4,355 rpm", output)
            self.assertIn("Intake 201 · Turbo Turbocharger_Stage1", output)
            self.assertIn("model 1.0.0 · data 2026.09.2", output)
            # drivetrain line from DriveInfo
            self.assertIn("Drivetrain: RWD — 2/4 wheels, 1/2 axles", output)
            # inline marker + summary line (format_key_string space-cases keys)
            self.assertIn("LSD0: Custom Turbo X Y Z [unknown]", output)
            self.assertIn("1 unknown part(s)", output)
            self.assertNotIn("[incompatible]", output)
            # self-check re-syncs the [MODS] tag like /check_mods
            mock_refresh.assert_awaited_once_with(
                self.ctx.character, self.ctx.http_client_mod, has_custom_parts=True
            )

    async def test_cmd_check_parts_stock_no_markers(self):
        """All-stock vehicle: no markers, no flags line, tag cleared."""
        mock_last_vehicle = {
            "vehicle": {
                "vehicleId": 1001,
                "fullName": "Elisa2_C Default__Elisa2",
                "classFullName": "Class /Game/Cars/Models/Elisa2",
            }
        }
        mock_parts = {
            "vehicleId": 1001,
            "parts": [
                {"Key": "SmallBlock_240HP", "Slot": 2},
                {"Key": "201", "Slot": 5},
            ],
        }

        with (
            patch(
                "amc.commands.vehicles.get_player_last_vehicle",
                new=AsyncMock(return_value=mock_last_vehicle),
            ),
            patch(
                "amc.commands.vehicles.get_player_last_vehicle_parts",
                new=AsyncMock(return_value=mock_parts),
            ),
            patch(
                "amc.commands.vehicles.detect_custom_parts",
                return_value=[],
            ),
            patch(
                "amc.commands.vehicles.detect_incompatible_parts",
                return_value=[],
            ),
            patch(
                "amc.commands.vehicles.refresh_player_name", new=AsyncMock()
            ) as mock_refresh,
        ):
            await cmd_check_parts(self.ctx)

            output = self.ctx.reply.call_args[0][0]
            self.assertIn("Power: 244.4 hp", output)  # intake 201 + NA (no turbo slot)
            self.assertIn("Turbo none", output)
            self.assertNotIn("[unknown]", output)
            self.assertNotIn("unknown part(s)", output)
            self.assertIn("Drivetrain: Unknown", output)  # no DriveInfo in payload
            mock_refresh.assert_awaited_once_with(
                self.ctx.character, self.ctx.http_client_mod, has_custom_parts=False
            )

    async def test_cmd_check_parts_unknown_engine(self):
        """Engine outside the model data -> explicit Power: Unknown, never fabricated."""
        mock_last_vehicle = {
            "vehicle": {
                "vehicleId": 1001,
                "fullName": "Elisa2_C Default__Elisa2",
                "classFullName": "Class /Game/Cars/Models/Elisa2",
            }
        }
        mock_parts = {
            "vehicleId": 1001,
            "parts": [{"Key": "Ghost_Engine_999", "Slot": 2}],
        }

        with (
            patch(
                "amc.commands.vehicles.get_player_last_vehicle",
                new=AsyncMock(return_value=mock_last_vehicle),
            ),
            patch(
                "amc.commands.vehicles.get_player_last_vehicle_parts",
                new=AsyncMock(return_value=mock_parts),
            ),
            patch(
                "amc.commands.vehicles.detect_custom_parts",
                return_value=[],
            ),
            patch(
                "amc.commands.vehicles.detect_incompatible_parts",
                return_value=[],
            ),
            patch(
                "amc.commands.vehicles.refresh_player_name", new=AsyncMock()
            ),
        ):
            await cmd_check_parts(self.ctx)

            output = self.ctx.reply.call_args[0][0]
            self.assertIn("Power: Unknown", output)
            self.assertNotIn("hp @", output)

    async def test_cmd_check_parts_ev_fixed_rating(self):
        """Vanilla Electric_* engine (asset-only in the model) -> fixed rating."""
        mock_last_vehicle = {
            "vehicle": {
                "vehicleId": 1001,
                "fullName": "SomeEV_C Default__SomeEV",
                "classFullName": "Class /Game/Cars/Models/SomeEV",
            }
        }
        mock_parts = {
            "vehicleId": 1001,
            "parts": [{"Key": "Electric_300HP", "Slot": 2}],
        }

        with (
            patch(
                "amc.commands.vehicles.get_player_last_vehicle",
                new=AsyncMock(return_value=mock_last_vehicle),
            ),
            patch(
                "amc.commands.vehicles.get_player_last_vehicle_parts",
                new=AsyncMock(return_value=mock_parts),
            ),
            patch(
                "amc.commands.vehicles.detect_custom_parts",
                return_value=[],
            ),
            patch(
                "amc.commands.vehicles.detect_incompatible_parts",
                return_value=[],
            ),
            patch(
                "amc.commands.vehicles.refresh_player_name", new=AsyncMock()
            ),
        ):
            await cmd_check_parts(self.ctx)

            output = self.ctx.reply.call_args[0][0]
            self.assertIn("Power: 308 hp (fixed motor rating)", output)

    async def test_cmd_check_parts_no_parts(self):
        """Empty parts payload keeps the existing 'No parts found' path."""
        with (
            patch(
                "amc.commands.vehicles.get_player_last_vehicle",
                new=AsyncMock(
                    return_value={
                        "vehicle": {
                            "vehicleId": 1001,
                            "fullName": "Jemusi_C Default__Jemusi",
                            "classFullName": "Class /Game/Vehicles/Jemusi",
                        }
                    }
                ),
            ),
            patch(
                "amc.commands.vehicles.get_player_last_vehicle_parts",
                new=AsyncMock(return_value={"parts": []}),
            ),
        ):
            await cmd_check_parts(self.ctx)

            output = self.ctx.reply.call_args[0][0]
            self.assertIn("No parts found", output)

    async def test_cmd_check_parts_no_vehicle(self):
        with (
            patch(
                "amc.commands.vehicles.get_player_last_vehicle",
                new=AsyncMock(return_value={"vehicle": None}),
            ),
            patch(
                "amc.commands.vehicles.get_player_last_vehicle_parts",
                new=AsyncMock(return_value={"parts": []}),
            ),
        ):
            await cmd_check_parts(self.ctx)

            output = self.ctx.reply.call_args[0][0]
            self.assertIn("No Vehicle", output)

    async def test_cmd_check_parts_target(self):
        """Admin cross-check resolves the target's vehicle."""
        self.ctx.player_info["bIsAdmin"] = True

        mock_players = [("pid-99", {"name": "SomePlayer", "character_guid": "cg-99"})]
        mock_last_vehicle = {
            "vehicle": {
                "vehicleId": 2002,
                "fullName": "Miramar_C Default__Miramar",
                "classFullName": "Class /Game/Vehicles/Miramar",
            }
        }
        mock_parts = {
            "vehicleId": 2002,
            "parts": [{"Key": "SmallBlock_240HP", "Slot": 2}],
        }

        with (
            patch(
                "amc.commands.vehicles.get_players",
                new=AsyncMock(return_value=mock_players),
            ),
            patch(
                "amc.commands.vehicles.get_player_last_vehicle",
                new=AsyncMock(return_value=mock_last_vehicle),
            ),
            patch(
                "amc.commands.vehicles.get_player_last_vehicle_parts",
                new=AsyncMock(return_value=mock_parts),
            ),
            patch(
                "amc.commands.vehicles.detect_custom_parts",
                return_value=[],
            ),
            patch(
                "amc.commands.vehicles.detect_incompatible_parts",
                return_value=[],
            ),
        ):
            await cmd_check_parts(self.ctx, "SomePlayer")

            self.ctx.reply.assert_called()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("Parts Check", output)
            self.assertIn("Power: 238.6 hp", output)

    def test_format_driveline_game_missing(self):
        self.assertEqual(format_driveline_game(None), "Drivetrain: Unknown")
        self.assertEqual(format_driveline_game({}), "Drivetrain: Unknown")

    def test_format_driveline_game_full(self):
        info = {
            "drive_type": "AWD",
            "driven_wheel_count": 4,
            "total_wheel_count": 4,
            "total_axle_count": 2,
            "driven_axle_indices": [0, 1],
        }
        self.assertEqual(
            format_driveline_game(info), "Drivetrain: AWD — 4/4 wheels, 2/2 axles"
        )

    def test_format_driveline_game_effective_label(self):
        info = {
            "drive_type": "AWD",
            "effective_drive_type": "RWD",
            "driven_wheel_count": 2,
            "total_wheel_count": 4,
            "total_axle_count": 2,
            "driven_axle_indices": [1],
        }
        self.assertEqual(
            format_driveline_game(info),
            "Drivetrain: AWD (RWD) — 2/4 wheels, 1/2 axles",
        )

    # --- Incompatible Parts Detection Tests ---

    async def test_detect_incompatible_parts_flags_wrong_type(self):
        """A Bike engine on a Small vehicle should be flagged as incompatible."""
        from amc import mod_detection

        # Save originals and mock caches
        orig_compat = mod_detection._part_compatible_types
        orig_vtype = mod_detection._vehicle_type_map
        orig_stock = mod_detection._stock_part_keys
        try:
            mod_detection._part_compatible_types = {
                "bike_i2_30hp": {"Bike"},
                "smallblock_140hp": {"Small", "Pickup"},
            }
            mod_detection._vehicle_type_map = {"Jemusi": "Small"}
            mod_detection._stock_part_keys = {
                "bike_i2_30hp",
                "smallblock_140hp",
                "stockbrake",
            }

            parts = [
                {"Key": "Bike_I2_30HP", "Slot": 0},
                {"Key": "StockBrake", "Slot": 1},
            ]
            result = mod_detection.detect_incompatible_parts(
                parts, "Jemusi_C Default__Jemusi"
            )
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["key"], "Bike_I2_30HP")
            self.assertEqual(result[0]["vehicle_type"], "Small")
            self.assertIn("Bike", result[0]["allowed_types"])
        finally:
            mod_detection._part_compatible_types = orig_compat
            mod_detection._vehicle_type_map = orig_vtype
            mod_detection._stock_part_keys = orig_stock

    async def test_detect_incompatible_parts_allows_correct_type(self):
        """A Small engine on a Small vehicle should pass."""
        from amc import mod_detection

        orig_compat = mod_detection._part_compatible_types
        orig_vtype = mod_detection._vehicle_type_map
        orig_stock = mod_detection._stock_part_keys
        try:
            mod_detection._part_compatible_types = {
                "smallblock_140hp": {"Small", "Pickup"},
            }
            mod_detection._vehicle_type_map = {"Jemusi": "Small"}
            mod_detection._stock_part_keys = {"smallblock_140hp"}

            parts = [{"Key": "SmallBlock_140HP", "Slot": 0}]
            result = mod_detection.detect_incompatible_parts(
                parts, "Jemusi_C Default__Jemusi"
            )
            self.assertEqual(len(result), 0)
        finally:
            mod_detection._part_compatible_types = orig_compat
            mod_detection._vehicle_type_map = orig_vtype
            mod_detection._stock_part_keys = orig_stock

    async def test_detect_incompatible_parts_unknown_vehicle(self):
        """When vehicle name is not in DB, return empty (graceful no-op)."""
        from amc import mod_detection

        orig_compat = mod_detection._part_compatible_types
        orig_vtype = mod_detection._vehicle_type_map
        try:
            mod_detection._part_compatible_types = {"bike_i2_30hp": {"Bike"}}
            mod_detection._vehicle_type_map = {}

            parts = [{"Key": "Bike_I2_30HP", "Slot": 0}]
            result = mod_detection.detect_incompatible_parts(
                parts, "UnknownCar_C Default__UnknownCar"
            )
            self.assertEqual(len(result), 0)
        finally:
            mod_detection._part_compatible_types = orig_compat
            mod_detection._vehicle_type_map = orig_vtype

    async def test_detect_incompatible_parts_no_db(self):
        """When DB fails to load, return empty."""
        from amc import mod_detection

        orig_compat = mod_detection._part_compatible_types
        orig_vtype = mod_detection._vehicle_type_map
        try:
            mod_detection._part_compatible_types = {}
            mod_detection._vehicle_type_map = {}

            parts = [{"Key": "Bike_I2_30HP", "Slot": 0}]
            result = mod_detection.detect_incompatible_parts(
                parts, "Jemusi_C Default__Jemusi"
            )
            self.assertEqual(len(result), 0)
        finally:
            mod_detection._part_compatible_types = orig_compat
            mod_detection._vehicle_type_map = orig_vtype

    async def test_cmd_check_mods_shows_incompatible(self):
        """check_mods should show incompatible parts section."""

        mock_last_vehicle = {
            "vehicle": {
                "vehicleId": 1001,
                "fullName": "Jemusi_C Default__Jemusi",
                "classFullName": "Class /Game/Vehicles/Jemusi",
            }
        }
        mock_parts = {
            "vehicleId": 1001,
            "parts": [
                {"Key": "Bike_I2_30HP", "Slot": 0},
            ],
        }

        with (
            patch(
                "amc.commands.vehicles.get_player_last_vehicle",
                new=AsyncMock(return_value=mock_last_vehicle),
            ),
            patch(
                "amc.commands.vehicles.get_player_last_vehicle_parts",
                new=AsyncMock(return_value=mock_parts),
            ),
            patch(
                "amc.commands.vehicles.detect_custom_parts",
                return_value=[],
            ),
            patch(
                "amc.commands.vehicles.detect_incompatible_parts",
                return_value=[
                    {
                        "key": "Bike_I2_30HP",
                        "slot": "Engine",
                        "slot_value": 0,
                        "vehicle_type": "Small",
                        "allowed_types": ["Bike"],
                    }
                ],
            ),
        ):
            await cmd_check_mods(self.ctx)

            self.ctx.reply.assert_called()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("Mod Check", output)
            self.assertIn("incompatible", output)

    async def test_cmd_check_mods_shows_drive_info(self):
        """DriveInfo from the mod server should appear in the output."""

        mock_last_vehicle = {
            "vehicle": {
                "vehicleId": 4004,
                "fullName": "Jemusi_C Default__Jemusi",
                "classFullName": "Class /Game/Vehicles/Jemusi",
                "DriveInfo": {
                    "drive_type": "RWD",
                    "effective_drive_type": "RWD",
                    "driven_wheel_count": 2,
                    "total_wheel_count": 4,
                    "driven_axle_indices": [1],
                    "total_axle_count": 2,
                    "num_differentials": 1,
                },
            }
        }
        mock_parts = {
            "vehicleId": 4004,
            "parts": [{"Key": "StockEngine", "Slot": 0}],
        }

        with (
            patch(
                "amc.commands.vehicles.get_player_last_vehicle",
                new=AsyncMock(return_value=mock_last_vehicle),
            ),
            patch(
                "amc.commands.vehicles.get_player_last_vehicle_parts",
                new=AsyncMock(return_value=mock_parts),
            ),
            patch("amc.commands.vehicles.detect_custom_parts", return_value=[]),
            patch("amc.commands.vehicles.detect_incompatible_parts", return_value=[]),
        ):
            await cmd_check_mods(self.ctx)

            self.ctx.reply.assert_called()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("Drivetrain: RWD", output)
            self.assertIn("2/4 wheels", output)
            self.assertIn("1/2 axles", output)

    async def test_cmd_check_mods_shows_parttime_awd(self):
        """Part-time AWD should show the effective drive type in parentheses."""

        mock_last_vehicle = {
            "vehicle": {
                "vehicleId": 5005,
                "fullName": "Longhorn_C Default__Longhorn",
                "classFullName": "Class /Game/Vehicles/Longhorn",
                "DriveInfo": {
                    "drive_type": "AWD",
                    "effective_drive_type": "Part-time",
                    "driven_wheel_count": 4,
                    "total_wheel_count": 4,
                    "driven_axle_indices": [0, 1],
                    "total_axle_count": 2,
                    "num_differentials": 3,
                    "current_disconnected_diffs": ["FrontDiff"],
                },
            }
        }
        mock_parts = {
            "vehicleId": 5005,
            "parts": [{"Key": "CustomTurbo_XYZ", "Slot": 5}],
        }

        with (
            patch(
                "amc.commands.vehicles.get_player_last_vehicle",
                new=AsyncMock(return_value=mock_last_vehicle),
            ),
            patch(
                "amc.commands.vehicles.get_player_last_vehicle_parts",
                new=AsyncMock(return_value=mock_parts),
            ),
            patch(
                "amc.commands.vehicles.detect_custom_parts",
                return_value=[
                    {"key": "CustomTurbo_XYZ", "slot": "Turbocharger", "slot_value": 5}
                ],
            ),
            patch("amc.commands.vehicles.detect_incompatible_parts", return_value=[]),
        ):
            await cmd_check_mods(self.ctx)

            self.ctx.reply.assert_called()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("Mod Check", output)
            self.assertIn("AWD (Part-time)", output)
            self.assertIn("4/4 wheels", output)
            self.assertIn("2/2 axles", output)


class ArrestCommandTestCase(TestCase):
    """Tests for /arrest command."""

    def setUp(self):
        self.ctx = MagicMock(spec=CommandContext)
        self.ctx.reply = AsyncMock()
        self.ctx.announce = AsyncMock()
        self.ctx.http_client_mod = MagicMock()
        self.ctx.http_client = MagicMock()
        self.ctx.discord_client = None
        self.ctx.player_info = {}

        self.ctx.http_client_mod.get.return_value = MockResponse()
        self.ctx.http_client_mod.post.return_value = MockResponse()
        self.ctx.http_client.get.return_value = MockResponse()

        self.player = Player.objects.create(unique_id="76561198000000001")
        self.character = Character.objects.create(
            name="CopPlayer", player=self.player, guid="COP_GUID_001"
        )
        self.ctx.character = self.character
        self.ctx.player = self.player
        self.ctx.timestamp = timezone.now()

        # Criminal player
        self.criminal_player = Player.objects.create(unique_id="76561198000000002")
        self.criminal_character = Character.objects.create(
            name="CriminalPlayer", player=self.criminal_player, guid="CRIM_GUID_001"
        )

    def _make_player_list(self, cop_loc, criminals=None):
        """Build a mock game server player list.

        criminals: list of (loc_tuple, has_vehicle) or None
        """
        players = [
            (
                str(self.player.unique_id),
                {
                    "name": "CopPlayer",
                    "unique_id": str(self.player.unique_id),
                    "character_guid": self.character.guid,
                    "location": f"X={cop_loc[0]} Y={cop_loc[1]} Z={cop_loc[2]}",
                },
            )
        ]
        if criminals is not None:
            for i, (loc, has_vehicle) in enumerate(criminals):
                # Use first criminal as default, create extras on the fly
                if i == 0:
                    guid = self.criminal_character.guid
                    uid = str(self.criminal_player.unique_id)
                    name = "CriminalPlayer"
                else:
                    guid = f"CRIM_GUID_{i + 1:03d}"
                    uid = f"7656119800000{i + 10:04d}"
                    name = f"Criminal{i + 1}"
                crim_data = {
                    "name": name,
                    "unique_id": uid,
                    "character_guid": guid,
                    "location": f"X={loc[0]} Y={loc[1]} Z={loc[2]}",
                }
                if has_vehicle:
                    crim_data["vehicle"] = {
                        "name": "Longhorn Semi",
                        "unique_id": 12345 + i,
                    }
                players.append((uid, crim_data))
        return players

    def test_parse_location_string(self):
        x, y, z = parse_location_string("X=-53918.590 Y=153629.920 Z=-20901.710")
        self.assertAlmostEqual(x, -53918.590)
        self.assertAlmostEqual(y, 153629.920)
        self.assertAlmostEqual(z, -20901.710)

    async def test_cmd_arrest_deprecated(self):
        """/arrest now replies with a deprecation message."""
        await cmd_arrest(self.ctx)
        self.ctx.reply.assert_called_once()
        msg = self.ctx.reply.call_args[0][0]
        self.assertIn("Deprecated", msg)
        self.assertIn("no longer available", msg)

    # --- Wanted Command Tests ---

    async def test_cmd_wanted_no_records(self):
        """No active criminal records → reply 'No wanted criminals'."""
        await cmd_wanted(self.ctx)
        self.ctx.reply.assert_called_with("No wanted criminals")

    async def test_cmd_wanted_with_records(self):
        """Active records shown online-first, sorted by confiscatable_amount desc."""
        timezone.now()

        # Create characters (IDs avoid setUp collision)
        player_a = await Player.objects.acreate(unique_id="76561198000000101")
        char_online = await Character.objects.acreate(
            name="OnlineCriminal",
            player=player_a,
            guid="guid-online",
            criminal_laundered_total=250_000,  # level 6
        )
        player_b = await Player.objects.acreate(unique_id="76561198000000102")
        char_offline = await Character.objects.acreate(
            name="OfflineCriminal",
            player=player_b,
            guid="guid-offline",
            criminal_laundered_total=500_000,  # level 11
        )
        player_c = await Player.objects.acreate(unique_id="76561198000000103")
        char_online2 = await Character.objects.acreate(
            name="OnlineCriminal2",
            player=player_c,
            guid="guid-online2",
            criminal_laundered_total=100_000,  # level 3
        )

        # Create active records with confiscatable_amount driving sort order
        await CriminalRecord.objects.acreate(
            character=char_online,
            reason="Money delivery",
            cleared_at=None,  # active
            amount=250_000,
            confiscatable_amount=250_000,
        )
        await CriminalRecord.objects.acreate(
            character=char_offline,
            reason="Money delivery",
            cleared_at=None,  # active
            amount=500_000,
            confiscatable_amount=500_000,
        )
        await CriminalRecord.objects.acreate(
            character=char_online2,
            reason="Money delivery",
            cleared_at=None,  # active
            amount=100_000,
            confiscatable_amount=100_000,
        )

        # Mock online players: only guid-online and guid-online2 are online
        mock_players = [
            (
                "76561198000000101",
                {"character_guid": "guid-online", "name": "OnlineCriminal"},
            ),
            (
                "76561198000000103",
                {"character_guid": "guid-online2", "name": "OnlineCriminal2"},
            ),
        ]

        with patch(
            "amc.commands.wanted.get_players",
            new=AsyncMock(return_value=mock_players),
        ):
            await cmd_wanted(self.ctx)

        self.ctx.reply.assert_called()
        output = self.ctx.reply.call_args[0][0]

        # Verify structure
        self.assertIn("Wanted List", output)
        self.assertIn("Criminal Record", output)

        # Online section: OnlineCriminal ($250k) must appear before OnlineCriminal2 ($100k)
        online_pos = output.index("OnlineCriminal <")
        online2_pos = output.index("OnlineCriminal2")
        self.assertLess(online_pos, online2_pos)

        # Offline section contains OfflineCriminal with its criminal level
        self.assertIn("OfflineCriminal", output)
        self.assertIn("C11", output)

        # Only confiscatable_amount shown (not laundered total)
        self.assertIn("$250,000", output)  # OnlineCriminal confiscatable
        self.assertIn("$500,000", output)  # OfflineCriminal confiscatable
        self.assertIn("$100,000", output)  # OnlineCriminal2 confiscatable

        # Criminal Record section header present
        self.assertIn("<Title>Criminal Record</>", output)

    async def test_cmd_wanted_active_bounty_shows_confiscatable(self):
        """Active Wanted records show confiscatable_amount from CriminalRecord, not Wanted.amount."""
        from amc.models import Wanted

        player_a = await Player.objects.acreate(unique_id="76561198000000301")
        char_a = await Character.objects.acreate(
            name="BountyOnline",
            player=player_a,
            guid="guid-bounty-online",
            criminal_laundered_total=100_000,
        )
        player_b = await Player.objects.acreate(unique_id="76561198000000302")
        char_b = await Character.objects.acreate(
            name="BountyOffline",
            player=player_b,
            guid="guid-bounty-offline",
            criminal_laundered_total=50_000,
        )

        # Active Wanted records (Wanted.amount may differ from confiscatable_amount)
        await Wanted.objects.acreate(
            character=char_a,
            wanted_remaining=180,
            amount=9_999,  # intentionally low — should NOT be shown
        )
        await Wanted.objects.acreate(
            character=char_b,
            wanted_remaining=60,
            amount=9_999,
        )

        # CriminalRecord with the real confiscatable values
        await CriminalRecord.objects.acreate(
            character=char_a,
            reason="Money delivery",
            cleared_at=None,
            amount=300_000,
            confiscatable_amount=300_000,
        )
        await CriminalRecord.objects.acreate(
            character=char_b,
            reason="Money delivery",
            cleared_at=None,
            amount=150_000,
            confiscatable_amount=150_000,
        )

        # Only char_a is online
        mock_players = [
            (
                "76561198000000301",
                {"character_guid": "guid-bounty-online", "name": "BountyOnline"},
            ),
        ]

        with patch(
            "amc.commands.wanted.get_players",
            new=AsyncMock(return_value=mock_players),
        ):
            await cmd_wanted(self.ctx)

        output = self.ctx.reply.call_args[0][0]

        # Active Bounties section shown
        self.assertIn("Active Bounties", output)

        # Confiscatable amounts shown, not Wanted.amount (9999)
        self.assertIn("$300,000", output)
        self.assertIn("$150,000", output)
        self.assertNotIn("$9,999", output)

        # Online player appears before offline
        online_pos = output.index("BountyOnline")
        offline_pos = output.index("BountyOffline")
        self.assertLess(online_pos, offline_pos)

    async def test_cmd_wanted_expired_excluded(self):
        """Expired records are not shown."""
        timezone.now()
        player_x = await Player.objects.acreate(unique_id="76561198000000104")
        char = await Character.objects.acreate(
            name="ExpiredCriminal",
            player=player_x,
            guid="guid-expired",
            criminal_laundered_total=100_000,
        )
        await CriminalRecord.objects.acreate(
            character=char,
            reason="Money delivery",
            cleared_at=timezone.now(),  # cleared = no longer active
        )

        with patch(
            "amc.commands.wanted.get_players",
            new=AsyncMock(return_value=[]),
        ):
            await cmd_wanted(self.ctx)

        self.ctx.reply.assert_called_with("No wanted criminals")

    async def test_cmd_wanted_excludes_active_police(self):
        """Characters with active police sessions are excluded from wanted list."""
        timezone.now()

        # Criminal with active police session — should be excluded
        player_cop = await Player.objects.acreate(unique_id="76561198000000201")
        char_cop = await Character.objects.acreate(
            name="CopWithRecord",
            player=player_cop,
            guid="guid-cop-record",
            criminal_laundered_total=300_000,
        )
        await CriminalRecord.objects.acreate(
            character=char_cop,
            reason="Money delivery",
            cleared_at=None,  # active
        )
        await PoliceSession.objects.acreate(character=char_cop)  # active session

        # Regular criminal — should appear
        player_crim = await Player.objects.acreate(unique_id="76561198000000202")
        char_crim = await Character.objects.acreate(
            name="RegularCriminal",
            player=player_crim,
            guid="guid-regular-crim",
            criminal_laundered_total=200_000,
        )
        await CriminalRecord.objects.acreate(
            character=char_crim,
            reason="Money delivery",
            cleared_at=None,  # active
        )

        with patch(
            "amc.commands.wanted.get_players",
            new=AsyncMock(return_value=[]),
        ):
            await cmd_wanted(self.ctx)

        self.ctx.reply.assert_called()
        output = self.ctx.reply.call_args[0][0]
        self.assertNotIn("CopWithRecord", output)
        self.assertIn("RegularCriminal", output)


# ---------------------------------------------------------------------------
# SetWantedCooldownTestCase
# ---------------------------------------------------------------------------


class SetWantedCooldownTestCase(TestCase):
    """Tests for the 1-hour cooldown on /setwanted after a Wanted record expires."""

    def setUp(self):
        from amc.commands.police import cmd_setwanted
        from amc.models import Player, Character, PoliceSession

        self.cmd_setwanted = cmd_setwanted

        self.cop_player = Player.objects.create(unique_id="76561199000001001")
        self.cop_char = Character.objects.create(
            name="OfficerCool",
            player=self.cop_player,
            guid="cop-guid-cool",
        )
        PoliceSession.objects.create(character=self.cop_char)

        self.target_player = Player.objects.create(unique_id="76561199000001002")
        self.target_char = Character.objects.create(
            name="CooldownTarget",
            player=self.target_player,
            guid="target-guid-cool",
        )

        self.mock_players = [
            (
                "76561199000001002",
                {
                    "character_guid": "target-guid-cool",
                    "name": "CooldownTarget",
                    "location": "X=0 Y=0 Z=0",
                },
            )
        ]

        class _MockAsyncIterable:
            def __init__(self, items):
                self.items = items

            def __aiter__(self):
                self._index = 0
                return self

            async def __anext__(self):
                if self._index >= len(self.items):
                    raise StopAsyncIteration
                item = self.items[self._index]
                self._index += 1
                return item

        async def _empty_police():
            return _MockAsyncIterable([])

        self.empty_police = _empty_police

        async def _mock_get_player(*args, **kwargs):
            return {"bAFK": False}

        self.mock_get_player = _mock_get_player

        self.ctx = MagicMock()
        self.ctx.player = self.cop_player
        self.ctx.character = self.cop_char
        self.ctx.http_client = MagicMock()
        self.ctx.http_client_mod = MagicMock()
        self.ctx.reply = AsyncMock()
        self.ctx.announce = AsyncMock()

    async def test_cooldown_blocks_within_one_hour(self):
        """Target arrested 30m ago -> setwanted blocked with remaining minutes shown."""
        from amc.models import Wanted

        await Wanted.objects.acreate(
            character=self.target_char,
            wanted_remaining=0,
            expired_at=timezone.now() - timedelta(minutes=30),
        )

        with (
            patch(
                "amc.commands.police.get_players",
                new=AsyncMock(return_value=self.mock_players),
            ),
            patch(
                "amc.commands.police.get_active_police_characters",
                new=self.empty_police,
            ),
        ):
            await self.cmd_setwanted(self.ctx, "CooldownTarget")

        self.ctx.reply.assert_called_once()
        reply = self.ctx.reply.call_args[0][0]
        self.assertIn("Cooldown", reply)
        self.assertIn("CooldownTarget", reply)
        # Remaining is ~30 minutes (+/- rounding); just verify a number is present
        self.assertIn("minute", reply)

    async def test_cooldown_allows_after_one_hour(self):
        """Target arrested 61m ago -> setwanted allowed."""
        from amc.models import Wanted

        await Wanted.objects.acreate(
            character=self.target_char,
            wanted_remaining=0,
            expired_at=timezone.now() - timedelta(minutes=61),
        )

        with (
            patch(
                "amc.commands.police.get_players",
                new=AsyncMock(return_value=self.mock_players),
            ),
            patch(
                "amc.commands.police.create_or_refresh_wanted", new=AsyncMock()
            ) as mock_create,
            patch(
                "amc.commands.police.get_active_police_characters",
                new=self.empty_police,
            ),
            patch(
                "amc.commands.police.get_player",
                new=self.mock_get_player,
            ),
            patch("amc.commands.police.make_suspect", new=AsyncMock()),
        ):
            await self.cmd_setwanted(self.ctx, "CooldownTarget")

        mock_create.assert_called_once_with(
            self.target_char, self.ctx.http_client_mod, amount=0
        )

    async def test_no_previous_wanted_no_cooldown(self):
        """Target never wanted before -> setwanted always allowed."""
        with (
            patch(
                "amc.commands.police.get_players",
                new=AsyncMock(return_value=self.mock_players),
            ),
            patch(
                "amc.commands.police.create_or_refresh_wanted", new=AsyncMock()
            ) as mock_create,
            patch(
                "amc.commands.police.get_active_police_characters",
                new=self.empty_police,
            ),
            patch(
                "amc.commands.police.get_player",
                new=self.mock_get_player,
            ),
            patch("amc.commands.police.make_suspect", new=AsyncMock()),
        ):
            await self.cmd_setwanted(self.ctx, "CooldownTarget")

        mock_create.assert_called_once_with(
            self.target_char, self.ctx.http_client_mod, amount=0
        )


class PoliceCommandTestCase(TestCase):
    """Tests for /police command teleport to nearest station."""

    def setUp(self):
        self.ctx = MagicMock(spec=CommandContext)
        self.ctx.reply = AsyncMock()
        self.ctx.announce = AsyncMock()
        self.ctx.http_client_mod = MagicMock()
        self.ctx.http_client = MagicMock()
        self.ctx.discord_client = None
        self.ctx.player_info = {}

        self.ctx.http_client_mod.get.return_value = MockResponse()
        self.ctx.http_client_mod.post.return_value = MockResponse()
        self.ctx.http_client.get.return_value = MockResponse()

        self.player = Player.objects.create(unique_id="76561198000000000")
        self.character = Character.objects.create(
            name="TestChar", player=self.player, guid="guid-123"
        )
        self.ctx.character = self.character
        self.ctx.player = self.player
        self.ctx.timestamp = timezone.now()

    async def test_cmd_police_on_duty_in_vehicle(self):
        """Going on duty while in a vehicle is blocked."""
        with (
            patch("amc.commands.police.is_police", new=AsyncMock(return_value=False)),
            patch(
                "amc.commands.police.get_players",
                new=AsyncMock(
                    return_value=[
                        (
                            str(self.player.unique_id),
                            {"name": "TestChar", "vehicle": {"name": "SomeCar"}},
                        )
                    ]
                ),
            ),
            patch(
                "amc.commands.police.get_player_customization",
                new=AsyncMock(return_value={"Costume": "Costume_Police_01"}),
            ),
            patch(
                "amc.commands.police.activate_police", new=AsyncMock()
            ) as mock_activate,
            patch("amc.commands.police.teleport_player", new=AsyncMock()) as mock_tp,
            patch("amc.player_tags.refresh_player_name", new=AsyncMock()),
        ):
            await cmd_police(self.ctx)
            self.ctx.reply.assert_called()
            args, _ = self.ctx.reply.call_args
            self.assertIn("Please exit the vehicle first", args[0])
            mock_tp.assert_not_called()
            mock_activate.assert_not_called()

    async def test_cmd_police_on_duty_teleports_to_nearest_station(self):
        """Going on duty teleports player to the nearest police station."""
        with (
            patch("amc.commands.police.is_police", new=AsyncMock(return_value=False)),
            patch(
                "amc.commands.police.get_players",
                new=AsyncMock(
                    return_value=[
                        (
                            str(self.player.unique_id),
                            {
                                "name": "TestChar",
                                "location": "X=-40000 Y=-140000 Z=-21000",
                            },
                        )
                    ]
                ),
            ),
            patch(
                "amc.commands.police.get_player_customization",
                new=AsyncMock(return_value={"Costume": "Costume_Police_01"}),
            ),
            patch(
                "amc.commands.police.activate_police", new=AsyncMock()
            ) as mock_activate,
            patch("amc.commands.police.teleport_player", new=AsyncMock()) as mock_tp,
            patch("amc.player_tags.refresh_player_name", new=AsyncMock()),
        ):
            await cmd_police(self.ctx)
            mock_tp.assert_called_with(
                self.ctx.http_client_mod,
                str(self.player.unique_id),
                {"X": -42361, "Y": -141792, "Z": -21094},
                no_vehicles=True,
            )
            mock_activate.assert_called_once()

    async def test_cmd_police_off_duty_no_teleport(self):
        """Going off duty does not teleport."""
        with (
            patch("amc.commands.police.is_police", new=AsyncMock(return_value=True)),
            patch(
                "amc.commands.police.deactivate_police", new=AsyncMock()
            ) as mock_deactivate,
            patch("amc.player_tags.refresh_player_name", new=AsyncMock()),
            patch("amc.commands.police.teleport_player", new=AsyncMock()) as mock_tp,
        ):
            await cmd_police(self.ctx)
            mock_deactivate.assert_called_once()
            mock_tp.assert_not_called()

    async def test_cmd_police_blocked_recent_criminal_delivery(self):
        """Going on duty is blocked if the player had a criminal delivery in the last 24h."""
        from amc.models import Delivery

        crim_record = await CriminalRecord.objects.acreate(
            character=self.character,
            reason="Money delivery",
            cleared_at=timezone.now(),
        )
        await Delivery.objects.acreate(
            character=self.character,
            cargo_key="Money",
            quantity=1,
            payment=1000,
            timestamp=timezone.now() - timezone.timedelta(hours=12),
            criminal_record=crim_record,
        )

        with (
            patch("amc.commands.police.is_police", new=AsyncMock(return_value=False)),
            patch(
                "amc.commands.police.get_players",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "amc.commands.police.activate_police", new=AsyncMock()
            ) as mock_activate,
            patch(
                "amc.commands.police.send_system_message", new=AsyncMock()
            ) as mock_ssm,
        ):
            await cmd_police(self.ctx)
            mock_ssm.assert_called()
            msg = mock_ssm.call_args[0][1]
            self.assertIn("24 hours", msg)
            mock_activate.assert_not_called()

    async def test_cmd_police_allowed_old_criminal_delivery(self):
        """Going on duty is allowed if the last criminal delivery was over 24h ago."""
        from amc.models import Delivery

        crim_record = await CriminalRecord.objects.acreate(
            character=self.character,
            reason="Money delivery",
            cleared_at=timezone.now(),
        )
        await Delivery.objects.acreate(
            character=self.character,
            cargo_key="Money",
            quantity=1,
            payment=1000,
            timestamp=timezone.now() - timezone.timedelta(hours=25),
            criminal_record=crim_record,
        )

        with (
            patch("amc.commands.police.is_police", new=AsyncMock(return_value=False)),
            patch(
                "amc.commands.police.get_players",
                new=AsyncMock(
                    return_value=[
                        (
                            str(self.player.unique_id),
                            {"name": "TestChar"},
                        )
                    ]
                ),
            ),
            patch(
                "amc.commands.police.get_player_customization",
                new=AsyncMock(return_value={"Costume": "Costume_Police_01"}),
            ),
            patch(
                "amc.commands.police.activate_police", new=AsyncMock()
            ) as mock_activate,
            patch("amc.commands.police.teleport_player", new=AsyncMock()),
            patch("amc.player_tags.refresh_player_name", new=AsyncMock()),
        ):
            await cmd_police(self.ctx)
            mock_activate.assert_called_once()

    async def test_cmd_police_allowed_no_criminal_delivery(self):
        """Going on duty is allowed if the player has no criminal deliveries at all."""
        with (
            patch("amc.commands.police.is_police", new=AsyncMock(return_value=False)),
            patch(
                "amc.commands.police.get_players",
                new=AsyncMock(
                    return_value=[
                        (
                            str(self.player.unique_id),
                            {"name": "TestChar"},
                        )
                    ]
                ),
            ),
            patch(
                "amc.commands.police.get_player_customization",
                new=AsyncMock(return_value={"Costume": "Costume_Police_01"}),
            ),
            patch(
                "amc.commands.police.activate_police", new=AsyncMock()
            ) as mock_activate,
            patch("amc.commands.police.teleport_player", new=AsyncMock()),
            patch("amc.player_tags.refresh_player_name", new=AsyncMock()),
        ):
            await cmd_police(self.ctx)
            mock_activate.assert_called_once()


class PoliceCostumeGateTestCase(TestCase):
    """Tests for /police command costume gate."""

    def setUp(self):
        self.ctx = MagicMock(spec=CommandContext)
        self.ctx.reply = AsyncMock()
        self.ctx.announce = AsyncMock()
        self.ctx.http_client_mod = MagicMock()
        self.ctx.http_client = MagicMock()
        self.ctx.discord_client = None
        self.ctx.player_info = {}

        self.ctx.http_client_mod.get.return_value = MockResponse()
        self.ctx.http_client_mod.post.return_value = MockResponse()
        self.ctx.http_client.get.return_value = MockResponse()

        self.player = Player.objects.create(unique_id="76561198000000000")
        self.character = Character.objects.create(
            name="TestChar", player=self.player, guid="guid-123"
        )
        self.ctx.character = self.character
        self.ctx.player = self.player
        self.ctx.timestamp = timezone.now()

    async def test_cmd_police_rejected_without_police_costume(self):
        """Going on duty without police costume is rejected."""
        with (
            patch("amc.commands.police.is_police", new=AsyncMock(return_value=False)),
            patch(
                "amc.commands.police.get_player_customization",
                new=AsyncMock(return_value={"Costume": "Costume_Butcher_01"}),
            ),
            patch(
                "amc.commands.police.activate_police", new=AsyncMock()
            ) as mock_activate,
            patch(
                "amc.commands.police.show_popup", new_callable=AsyncMock
            ) as mock_popup,
        ):
            await cmd_police(self.ctx)
            mock_popup.assert_called_once()
            msg = mock_popup.call_args[0][1]
            self.assertIn("police uniform", msg)
            mock_activate.assert_not_called()

    async def test_cmd_police_rejected_with_no_costume(self):
        """Going on duty with no costume is rejected."""
        with (
            patch("amc.commands.police.is_police", new=AsyncMock(return_value=False)),
            patch(
                "amc.commands.police.get_player_customization",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "amc.commands.police.activate_police", new=AsyncMock()
            ) as mock_activate,
            patch(
                "amc.commands.police.show_popup", new_callable=AsyncMock
            ) as mock_popup,
        ):
            await cmd_police(self.ctx)
            mock_popup.assert_called_once()
            mock_activate.assert_not_called()

    async def test_cmd_police_allowed_with_police_costume(self):
        """Going on duty with police costume succeeds."""
        with (
            patch("amc.commands.police.is_police", new=AsyncMock(return_value=False)),
            patch(
                "amc.commands.police.get_players",
                new=AsyncMock(
                    return_value=[
                        (
                            str(self.player.unique_id),
                            {"name": "TestChar"},
                        )
                    ]
                ),
            ),
            patch(
                "amc.commands.police.get_player_customization",
                new=AsyncMock(return_value={"Costume": "Costume_Police_01"}),
            ),
            patch(
                "amc.commands.police.activate_police", new=AsyncMock()
            ) as mock_activate,
            patch("amc.commands.police.teleport_player", new=AsyncMock()),
            patch("amc.player_tags.refresh_player_name", new=AsyncMock()),
        ):
            await cmd_police(self.ctx)
            mock_activate.assert_called_once()


class RentGroupingTestCase(SimpleTestCase):
    def setUp(self):
        self.ctx = MagicMock(spec=CommandContext)
        self.ctx.reply = AsyncMock()
        self.ctx.http_client_mod = MagicMock()
        self.ctx.player_info = {"Location": {"X": 0, "Y": 0, "Z": 0}}
        self.ctx.character = MagicMock()
        self.ctx.character.guid = "test-guid"
        self.ctx.character.name = "TestRenter"

    def _make_vehicle(self, pk, name, company_name=None):
        v = MagicMock()
        v.id = pk
        v.pk = pk
        v.config = {"VehicleName": name}
        if company_name:
            v.config["CompanyName"] = company_name
        return v

    async def test_rent_list_groups_by_company(self):
        """Vehicles should be grouped by CompanyName in the listing."""
        v1 = self._make_vehicle(1, "Truck A", "Corp One")
        v2 = self._make_vehicle(2, "Truck B", "Corp One")
        v3 = self._make_vehicle(3, "Van C", "Corp Two")

        with patch("amc.commands.vehicles.CharacterVehicle.objects.filter") as mock_filter:
            mock_qs = MagicMock()

            async def async_iter(items):
                for item in items:
                    yield item

            mock_qs.__aiter__ = lambda self_=None: async_iter([v1, v2, v3])
            mock_filter.return_value = mock_qs

            await cmd_rent(self.ctx)

            self.ctx.reply.assert_called_once()
            output = self.ctx.reply.call_args[0][0]
            corp_one_pos = output.index("Corp One")
            corp_two_pos = output.index("Corp Two")
            truck_a_pos = output.index("Truck A")
            truck_b_pos = output.index("Truck B")
            van_c_pos = output.index("Van C")
            self.assertLess(corp_one_pos, truck_a_pos)
            self.assertLess(truck_a_pos, truck_b_pos)
            self.assertLess(truck_b_pos, corp_two_pos)
            self.assertLess(corp_two_pos, van_c_pos)

    async def test_rent_list_no_rentals(self):
        """When no rentals exist, shows appropriate message."""
        with patch("amc.commands.vehicles.CharacterVehicle.objects.filter") as mock_filter:
            mock_qs = MagicMock()

            async def async_iter(items):
                for item in items:
                    yield item

            mock_qs.__aiter__ = lambda self_=None: async_iter([])
            mock_filter.return_value = mock_qs

            await cmd_rent(self.ctx)

            self.ctx.reply.assert_called_once()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("No rentals found", output)

    async def test_rent_list_search_filter(self):
        """Search term should filter vehicles by name."""
        v1 = self._make_vehicle(1, "SportCar_X", "Corp A")
        v2 = self._make_vehicle(2, "Truck_Y", "Corp B")

        with patch("amc.commands.vehicles.CharacterVehicle.objects.filter") as mock_filter:
            mock_qs = MagicMock()

            async def async_iter(items):
                for item in items:
                    yield item

            mock_qs.__aiter__ = lambda self_=None: async_iter([v1, v2])
            mock_filter.return_value = mock_qs

            await cmd_rent(self.ctx, "sport")

            self.ctx.reply.assert_called_once()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("SportCar_X", output)
            self.assertNotIn("Truck_Y", output)

    async def test_rent_spawn_success(self):
        """Renting by ID should spawn the vehicle."""
        mock_vehicle = MagicMock()
        mock_vehicle.config = {"VehicleName": "TestCar", "CompanyName": "Corp"}

        with (
            patch(
                "amc.commands.vehicles.CharacterVehicle.objects.aget",
                new=AsyncMock(return_value=mock_vehicle),
            ),
            patch(
                "amc.commands.vehicles.spawn_registered_vehicle", new=AsyncMock()
            ) as mock_spawn,
        ):
            await cmd_rent(self.ctx, "42")

            mock_spawn.assert_called_once()
            call_kwargs = mock_spawn.call_args
            self.assertEqual(call_kwargs[0][0], self.ctx.http_client_mod)
            self.assertEqual(call_kwargs[0][1], mock_vehicle)
            self.ctx.reply.assert_called_once()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("Corp", output)

    async def test_rent_spawn_not_found(self):
        """Renting a nonexistent ID should show error."""
        from amc.models import CharacterVehicle as CV

        with patch(
            "amc.commands.vehicles.CharacterVehicle.objects.aget",
            new=AsyncMock(side_effect=CV.DoesNotExist),
        ):
            await cmd_rent(self.ctx, "999")

            self.ctx.reply.assert_called_once()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("Rental not found", output)


class AdminVehicleSaveTestCase(SimpleTestCase):
    def setUp(self):
        self.ctx = MagicMock(spec=CommandContext)
        self.ctx.reply = AsyncMock()
        self.ctx.http_client_mod = MagicMock()
        self.ctx.player_info = {
            "bIsAdmin": True,
            "Location": {"X": 100, "Y": 200, "Z": 300},
        }
        self.ctx.character = MagicMock()
        self.ctx.character.guid = "admin-guid"
        self.ctx.character.name = "AdminChar"
        self.ctx.player = MagicMock()

    async def test_save_vehicle_no_name(self):
        await cmd_save_vehicle(self.ctx)
        self.ctx.reply.assert_called_once()
        self.assertIn("Usage", self.ctx.reply.call_args[0][0])

    async def test_save_vehicle_no_vehicle(self):
        with patch(
            "amc.commands.admin.register_player_vehicles",
            new=AsyncMock(return_value=None),
        ):
            await cmd_save_vehicle(self.ctx, "MyCar")
            self.ctx.reply.assert_called_once()
            self.assertIn("No vehicle found", self.ctx.reply.call_args[0][0])

    async def test_save_vehicle_success(self):
        mock_v = MagicMock()
        mock_v.id = 42
        mock_v.config = {"VehicleName": "Jemusi"}
        mock_v.alias = None
        mock_v.asave = AsyncMock()

        with patch(
            "amc.commands.admin.register_player_vehicles",
            new=AsyncMock(return_value=[mock_v]),
        ):
            await cmd_save_vehicle(self.ctx, "MyRide")

            self.assertEqual(mock_v.alias, "MyRide")
            mock_v.asave.assert_awaited_once_with(update_fields=["alias"])
            self.ctx.reply.assert_called_once()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("MyRide", output)
            self.assertIn("42", output)

    async def test_spawn_vehicle_no_name(self):
        await cmd_spawn_vehicle(self.ctx)
        self.ctx.reply.assert_called_once()
        self.assertIn("Usage", self.ctx.reply.call_args[0][0])

    async def test_spawn_vehicle_not_found(self):
        with patch(
            "amc.commands.admin.CharacterVehicle.objects.filter"
        ) as mock_filter:
            mock_filter.return_value.afirst = AsyncMock(return_value=None)
            await cmd_spawn_vehicle(self.ctx, "NoSuchCar")
            self.ctx.reply.assert_called_once()
            self.assertIn("No saved vehicle", self.ctx.reply.call_args[0][0])

    async def test_spawn_vehicle_success(self):
        mock_v = MagicMock()
        mock_v.config = {"VehicleName": "Jemusi"}
        mock_v.alias = "MyRide"

        with (
            patch(
                "amc.commands.admin.CharacterVehicle.objects.filter"
            ) as mock_filter,
            patch(
                "amc.commands.admin.spawn_registered_vehicle", new=AsyncMock()
            ) as mock_spawn,
        ):
            mock_filter.return_value.afirst = AsyncMock(return_value=mock_v)
            await cmd_spawn_vehicle(self.ctx, "MyRide")

            mock_spawn.assert_awaited_once()
            call_args = mock_spawn.call_args
            self.assertEqual(call_args[0][0], self.ctx.http_client_mod)
            self.assertEqual(call_args[0][1], mock_v)
            loc = call_args[0][2]
            self.assertEqual(loc["Z"], 200)  # 300 - 100
            self.ctx.reply.assert_called_once()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("MyRide", output)
