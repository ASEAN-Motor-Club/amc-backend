from django.test import SimpleTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import timedelta
from decimal import Decimal
from django.utils import timezone
from amc.command_framework import registry, CommandContext, CommandRegistry
from amc.commands.admin import (
    cmd_bill,
    cmd_exit,
    cmd_spawn,
    cmd_spawn_assets,
    cmd_spawn_dealerships,
    cmd_spawn_displays,
    cmd_spawn_garage_single,
    cmd_spawn_garages,
    cmd_tp_player,
)
from amc.commands.vehicles import cmd_check_mods
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
            patch("amc.commands.teleport.enter_last_vehicle", new=AsyncMock(return_value={"status": "success"})) as mock_enter,
        ):
            mock_filter.return_value.aexists = AsyncMock(return_value=True)

            await cmd_tp_vehicle(self.ctx)

            mock_enter.assert_called_with(self.ctx.http_client_mod, "guid-123")
            
    async def test_cmd_tp_vehicle_not_police(self):
        with (
            patch("amc.commands.teleport.PoliceSession.objects.filter") as mock_filter,
            patch("amc.commands.teleport.enter_last_vehicle", new=AsyncMock()) as mock_enter,
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
            "amc.commands.general.get_player",
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
        mock_job.bonus_multiplier = 0.5
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

            with patch("amc.commands.jobs.calculate_treasury_multiplier", new=MagicMock(return_value=1.5)):
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

        with patch(
            "amc.models.ScheduledEvent.objects.select_related"
        ) as mock_sr:
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
                "amc.commands.rp_rescue.list_player_vehicles",
                new=AsyncMock(return_value={}),
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

        mock_vehicles = {
            "1001": {
                "fullName": "Jemusi_C Default__Jemusi",
                "classFullName": "Class /Game/Vehicles/Jemusi",
                "parts": [
                    {"Key": "StockEngine", "Slot": 0},
                    {"Key": "CustomTurbo_XYZ", "Slot": 5},
                ],
                "isLastVehicle": True,
                "index": 0,
            }
        }

        with (
            patch(
                "amc.commands.vehicles.list_player_vehicles",
                new=AsyncMock(return_value=mock_vehicles),
            ),
            patch(
                "amc.commands.vehicles.detect_custom_parts",
                return_value=[{"key": "CustomTurbo_XYZ", "slot": "Turbocharger", "slot_value": 5}],
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
        mock_vehicles = {
            "2002": {
                "fullName": "Miramar_C Default__Miramar",
                "classFullName": "Class /Game/Vehicles/Miramar",
                "parts": [{"Key": "StockBrake", "Slot": 1}],
                "isLastVehicle": True,
                "index": 0,
            }
        }

        with (
            patch(
                "amc.commands.vehicles.get_players",
                new=AsyncMock(return_value=mock_players),
            ),
            patch(
                "amc.commands.vehicles.list_player_vehicles",
                new=AsyncMock(return_value=mock_vehicles),
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
        with patch(
            "amc.commands.vehicles.list_player_vehicles",
            new=AsyncMock(return_value={}),
        ):
            await cmd_check_mods(self.ctx)

            self.ctx.reply.assert_called()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("No Vehicle", output)
            self.assertIn("no active vehicle", output)

    async def test_cmd_check_mods_non_admin(self):
        """Non-admin users can also use check_mods."""
        self.ctx.player_info["bIsAdmin"] = False

        mock_vehicles = {
            "3003": {
                "fullName": "Jemusi_C Default__Jemusi",
                "classFullName": "Class /Game/Vehicles/Jemusi",
                "parts": [{"Key": "StockEngine", "Slot": 0}],
                "isLastVehicle": True,
                "index": 0,
            }
        }

        with (
            patch(
                "amc.commands.vehicles.list_player_vehicles",
                new=AsyncMock(return_value=mock_vehicles),
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

        mock_vehicles = {
            "1001": {
                "fullName": "Jemusi_C Default__Jemusi",
                "classFullName": "Class /Game/Vehicles/Jemusi",
                "parts": [
                    {"Key": "CustomTurbo_XYZ", "Slot": 5},
                ],
                "isLastVehicle": True,
                "index": 0,
            }
        }

        with (
            patch(
                "amc.commands.vehicles.list_player_vehicles",
                new=AsyncMock(return_value=mock_vehicles),
            ),
            patch(
                "amc.commands.vehicles.detect_custom_parts",
                return_value=[{"key": "CustomTurbo_XYZ", "slot": "Turbocharger", "slot_value": 5}],
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

        mock_vehicles = {
            "3003": {
                "fullName": "Jemusi_C Default__Jemusi",
                "classFullName": "Class /Game/Vehicles/Jemusi",
                "parts": [{"Key": "StockEngine", "Slot": 0}],
                "isLastVehicle": True,
                "index": 0,
            }
        }

        with (
            patch(
                "amc.commands.vehicles.list_player_vehicles",
                new=AsyncMock(return_value=mock_vehicles),
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
                "bike_i2_30hp", "smallblock_140hp", "stockbrake",
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

        mock_vehicles = {
            "1001": {
                "fullName": "Jemusi_C Default__Jemusi",
                "classFullName": "Class /Game/Vehicles/Jemusi",
                "parts": [
                    {"Key": "Bike_I2_30HP", "Slot": 0},
                ],
                "isLastVehicle": True,
                "index": 0,
            }
        }

        with (
            patch(
                "amc.commands.vehicles.list_player_vehicles",
                new=AsyncMock(return_value=mock_vehicles),
            ),
            patch(
                "amc.commands.vehicles.detect_custom_parts",
                return_value=[],
            ),
            patch(
                "amc.commands.vehicles.detect_incompatible_parts",
                return_value=[{
                    "key": "Bike_I2_30HP",
                    "slot": "Engine",
                    "slot_value": 0,
                    "vehicle_type": "Small",
                    "allowed_types": ["Bike"],
                }],
            ),
        ):
            await cmd_check_mods(self.ctx)

            self.ctx.reply.assert_called()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("Mod Check", output)
            self.assertIn("incompatible", output)

    async def test_cmd_check_mods_shows_drive_info(self):
        """DriveInfo from the mod server should appear in the output."""

        mock_vehicles = {
            "4004": {
                "fullName": "Jemusi_C Default__Jemusi",
                "classFullName": "Class /Game/Vehicles/Jemusi",
                "parts": [{"Key": "StockEngine", "Slot": 0}],
                "isLastVehicle": True,
                "index": 0,
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

        with (
            patch(
                "amc.commands.vehicles.list_player_vehicles",
                new=AsyncMock(return_value=mock_vehicles),
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

        mock_vehicles = {
            "5005": {
                "fullName": "Longhorn_C Default__Longhorn",
                "classFullName": "Class /Game/Vehicles/Longhorn",
                "parts": [{"Key": "CustomTurbo_XYZ", "Slot": 5}],
                "isLastVehicle": True,
                "index": 0,
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

        with (
            patch(
                "amc.commands.vehicles.list_player_vehicles",
                new=AsyncMock(return_value=mock_vehicles),
            ),
            patch(
                "amc.commands.vehicles.detect_custom_parts",
                return_value=[{"key": "CustomTurbo_XYZ", "slot": "Turbocharger", "slot_value": 5}],
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
                    guid = f"CRIM_GUID_{i+1:03d}"
                    uid = f"7656119800000{i+10:04d}"
                    name = f"Criminal{i+1}"
                crim_data = {
                    "name": name,
                    "unique_id": uid,
                    "character_guid": guid,
                    "location": f"X={loc[0]} Y={loc[1]} Z={loc[2]}",
                }
                if has_vehicle:
                    crim_data["vehicle"] = {"name": "Longhorn Semi", "unique_id": 12345 + i}
                players.append((uid, crim_data))
        return players

    def test_parse_location_string(self):
        x, y, z = parse_location_string("X=-53918.590 Y=153629.920 Z=-20901.710")
        self.assertAlmostEqual(x, -53918.590)
        self.assertAlmostEqual(y, 153629.920)
        self.assertAlmostEqual(z, -20901.710)

    async def test_cmd_arrest_not_cop(self):
        """Non-cop player gets rejection."""
        with patch("amc.commands.faction.send_system_message", new=AsyncMock()) as mock_sys:
            await cmd_arrest(self.ctx)
            mock_sys.assert_called()
            self.assertIn("police duty", mock_sys.call_args[0][1])

    async def test_cmd_arrest_no_criminals_nearby(self):
        """Cop with no criminals in range."""

        await PoliceSession.objects.acreate(character=self.character)

        mock_players = self._make_player_list(cop_loc=(100, 200, 300))

        with (
            patch(
                "amc.commands.faction.get_players",
                new=AsyncMock(return_value=mock_players),
            ),
            patch("amc.commands.faction.send_system_message", new=AsyncMock()) as mock_sys,
        ):
            await cmd_arrest(self.ctx)
            mock_sys.assert_called()
            self.assertIn("No players nearby", mock_sys.call_args[0][1])

    async def test_cmd_arrest_criminal_out_of_range(self):
        """Suspect exists but is too far away (>5000 units / 50m on foot)."""
        from amc.models import FactionMembership, FactionChoice

        await PoliceSession.objects.acreate(character=self.character)
        await FactionMembership.objects.acreate(
            player=self.criminal_player, faction=FactionChoice.CRIMINAL
        )

        mock_players = self._make_player_list(
            cop_loc=(100, 200, 300), criminals=[((6200, 200, 300), False)]
        )

        with (
            patch(
                "amc.commands.faction.get_players",
                new=AsyncMock(return_value=mock_players),
            ),
            patch("amc.commands.faction.send_system_message", new=AsyncMock()) as mock_sys,
        ):
            await cmd_arrest(self.ctx)
            mock_sys.assert_called()
            self.assertIn("within arrest range", mock_sys.call_args[0][1])

    async def test_cmd_arrest_suspect_speeding(self):
        """Suspect moves too fast (>1500 units/tick) → removed from arrest."""
        from amc.models import FactionMembership, FactionChoice

        await PoliceSession.objects.acreate(character=self.character)
        await FactionMembership.objects.acreate(
            player=self.criminal_player, faction=FactionChoice.CRIMINAL
        )

        # Criminal starts at (200,200,300), then jumps 2000 units (>SUSPECT_SPEED_LIMIT=1500)
        # Criminal must be in a vehicle for speed check to apply
        initial_players = self._make_player_list(
            cop_loc=(100, 200, 300), criminals=[((200, 200, 300), True)]
        )
        speeding_players = self._make_player_list(
            cop_loc=(100, 200, 300), criminals=[((2200, 200, 300), True)]
        )

        with (
            patch(
                "amc.commands.faction.get_players",
                new=AsyncMock(side_effect=[initial_players] + [speeding_players] * 3),
            ),
            patch("amc.commands.faction.asyncio.sleep", new=AsyncMock()),
            patch("amc.commands.faction.cache") as mock_cache,
            patch("amc.commands.faction.send_system_message", new=AsyncMock()) as mock_sys,
        ):
            mock_cache.get.return_value = None
            await cmd_arrest(self.ctx)
            msgs = [call[0][1] for call in mock_sys.call_args_list]
            self.assertTrue(
                any("too fast" in m for m in msgs), f"Expected 'too fast' in msgs: {msgs}"
            )

    async def test_cmd_arrest_cop_moved_still_succeeds(self):
        """Cop moves during polling but stays within range → arrest succeeds."""
        from amc.models import FactionMembership, FactionChoice, TeleportPoint
        from django.contrib.gis.geos import Point

        await PoliceSession.objects.acreate(character=self.character)
        await FactionMembership.objects.acreate(
            player=self.criminal_player, faction=FactionChoice.CRIMINAL
        )
        await TeleportPoint.objects.acreate(
            name="jail", location=Point(1000, 2000, 3000)
        )

        # Cop moves 200 units but stays within 10m of criminal
        initial_players = self._make_player_list(
            cop_loc=(100, 200, 300), criminals=[((200, 200, 300), False)]
        )
        moved_players = self._make_player_list(
            cop_loc=(300, 200, 300), criminals=[((200, 200, 300), False)]
        )

        with (
            patch(
                "amc.commands.faction.get_players",
                new=AsyncMock(side_effect=[initial_players] + [moved_players] * 3),
            ),
            patch("amc.commands.faction.asyncio.sleep", new=AsyncMock()),
            patch("amc.commands.faction.cache") as mock_cache,
            patch("amc.commands.faction.force_exit_vehicle", new=AsyncMock()),
            patch("amc.commands.faction.teleport_player", new=AsyncMock()) as mock_tp,
            patch("amc.commands.faction.show_popup", new=AsyncMock()),
            patch("amc.commands.faction.send_system_message", new=AsyncMock()),
        ):
            mock_cache.get.return_value = None
            await cmd_arrest(self.ctx)

            # Arrest should succeed despite cop movement
            mock_tp.assert_called()
            self.ctx.announce.assert_called()
            self.assertIn("arrested", self.ctx.announce.call_args[0][0])

    async def test_cmd_arrest_criminal_disconnected(self):
        """Criminal goes offline during polling → removed from arrest."""
        from amc.models import FactionMembership, FactionChoice

        await PoliceSession.objects.acreate(character=self.character)
        await FactionMembership.objects.acreate(
            player=self.criminal_player, faction=FactionChoice.CRIMINAL
        )

        initial_players = self._make_player_list(
            cop_loc=(100, 200, 300), criminals=[((200, 200, 300), False)]
        )
        cop_only = self._make_player_list(cop_loc=(100, 200, 300))

        with (
            patch(
                "amc.commands.faction.get_players",
                new=AsyncMock(side_effect=[initial_players, cop_only]),
            ),
            patch("amc.commands.faction.asyncio.sleep", new=AsyncMock()),
            patch("amc.commands.faction.cache") as mock_cache,
            patch("amc.commands.faction.send_system_message", new=AsyncMock()) as mock_sys,
        ):
            mock_cache.get.return_value = None
            await cmd_arrest(self.ctx)
            msgs = [call[0][1] for call in mock_sys.call_args_list]
            self.assertTrue(
                any("offline" in m for m in msgs),
                f"Expected 'offline' in msgs: {msgs}",
            )

    async def test_cmd_arrest_cooldown(self):
        """Second arrest within cooldown is rejected."""

        await PoliceSession.objects.acreate(character=self.character)

        with (
            patch("amc.commands.faction.cache") as mock_cache,
            patch("amc.commands.faction.send_system_message", new=AsyncMock()) as mock_sys,
        ):
            mock_cache.get.return_value = True  # cooldown active
            await cmd_arrest(self.ctx)
            mock_sys.assert_called()
            self.assertIn("wait", mock_sys.call_args[0][1])

    async def test_cmd_arrest_success(self):
        """Happy path: criminal arrested, exited vehicle, teleported to jail, popup shown."""
        from amc.models import FactionMembership, FactionChoice, TeleportPoint
        from django.contrib.gis.geos import Point

        await PoliceSession.objects.acreate(character=self.character)
        await FactionMembership.objects.acreate(
            player=self.criminal_player, faction=FactionChoice.CRIMINAL
        )
        await TeleportPoint.objects.acreate(
            name="jail", location=Point(1000, 2000, 3000)
        )

        mock_players = self._make_player_list(
            cop_loc=(100, 200, 300),
            criminals=[((200, 200, 300), True)],
        )

        with (
            patch(
                "amc.commands.faction.get_players",
                new=AsyncMock(return_value=mock_players),
            ),
            patch("amc.commands.faction.asyncio.sleep", new=AsyncMock()),
            patch("amc.commands.faction.cache") as mock_cache,
            patch(
                "amc.commands.faction.force_exit_vehicle", new=AsyncMock()
            ) as mock_exit,
            patch(
                "amc.commands.faction.teleport_player", new=AsyncMock()
            ) as mock_tp,
            patch(
                "amc.commands.faction.show_popup", new=AsyncMock()
            ) as mock_popup,
            patch("amc.commands.faction.send_system_message", new=AsyncMock()) as mock_sys,
        ):
            mock_cache.get.return_value = None
            await cmd_arrest(self.ctx)

            # Vehicle exit called
            mock_exit.assert_called_with(
                self.ctx.http_client_mod, "CRIM_GUID_001"
            )

            # Teleported to jail
            mock_tp.assert_called_with(
                self.ctx.http_client_mod,
                str(self.criminal_player.unique_id),
                {"X": 1000.0, "Y": 2000.0, "Z": 3000.0},
                no_vehicles=True,
            )

            # Popup shown to arrested player
            mock_popup.assert_called_with(
                self.ctx.http_client_mod,
                "You have been arrested!",
                player_id=str(self.criminal_player.unique_id),
            )

            # Confirmation sent via system message
            msgs = [call[0][1] for call in mock_sys.call_args_list]
            self.assertTrue(
                any("arrested and sent to jail" in m for m in msgs),
                f"Expected jail confirmation in msgs: {msgs}",
            )

            # Server-wide announcement
            self.ctx.announce.assert_called()
            self.assertIn(
                "arrested", self.ctx.announce.call_args[0][0]
            )

            # Cooldown set
            mock_cache.set.assert_called()

    async def test_cmd_arrest_retroactive_confiscates_money(self):
        """Criminal arrested — 1-min-old Money delivery seized at 90% (linear scaling)."""
        from amc.models import FactionMembership, FactionChoice, TeleportPoint, Delivery, Confiscation
        from django.contrib.gis.geos import Point

        await PoliceSession.objects.acreate(character=self.character)
        await FactionMembership.objects.acreate(
            player=self.criminal_player, faction=FactionChoice.CRIMINAL
        )
        await TeleportPoint.objects.acreate(
            name="jail", location=Point(1000, 2000, 3000)
        )

        self.criminal_character.criminal_laundered_total = 50_000
        await self.criminal_character.asave(update_fields=["criminal_laundered_total"])

        fixed_now = timezone.now().replace(microsecond=0)
        # Delivery 1 minute ago → 90% rate → int(25000 * 0.9) = 22500
        await Delivery.objects.acreate(
            character=self.criminal_character,
            cargo_key="Money", payment=25_000, quantity=1,
            timestamp=fixed_now - timedelta(minutes=1),
        )

        mock_players = self._make_player_list(
            cop_loc=(100, 200, 300),
            criminals=[((200, 200, 300), False)],
        )

        with (
            patch("amc.commands.faction.get_players", new=AsyncMock(return_value=mock_players)),
            patch("amc.commands.faction.asyncio.sleep", new=AsyncMock()),
            patch("amc.commands.faction.cache") as mock_cache,
            patch("amc.commands.faction.force_exit_vehicle", new=AsyncMock()),
            patch("amc.commands.faction.teleport_player", new=AsyncMock()),
            patch("amc.commands.faction.show_popup", new=AsyncMock()),
            patch("amc.commands.faction.send_system_message", new=AsyncMock()) as mock_sys,
            patch("amc.commands.faction.transfer_money", new=AsyncMock()) as mock_transfer,
            patch("amc.commands.faction.record_treasury_confiscation_income", new=AsyncMock()) as mock_treasury,
            patch("amc.commands.faction.record_confiscation_for_level", new=AsyncMock()) as mock_prog,
            patch("amc.commands.faction.send_fund_to_player_wallet", new=AsyncMock()) as mock_fund_wallet,
            patch("amc.commands.faction.timezone") as mock_tz,
        ):
            mock_tz.now.return_value = fixed_now
            mock_cache.get.return_value = None
            await cmd_arrest(self.ctx)

            self.assertEqual(mock_transfer.call_count, 2)
            mock_transfer.assert_any_call(
                self.ctx.http_client_mod, -22500,
                "Money Confiscated", str(self.criminal_player.unique_id),
            )
            mock_transfer.assert_any_call(
                self.ctx.http_client_mod, 22500,
                "Confiscation Reward", str(self.player.unique_id),
            )
            mock_treasury.assert_called_once_with(22500, "Police Confiscation")
            mock_prog.assert_called_once_with(
                self.character, 22500,
                http_client=self.ctx.http_client, session=self.ctx.http_client_mod,
            )
            mock_fund_wallet.assert_called_once_with(22500, self.character, "Confiscation Reward")

            await self.criminal_character.arefresh_from_db(fields=["criminal_laundered_total"])
            self.assertEqual(self.criminal_character.criminal_laundered_total, 27_500)

            self.assertTrue(
                await Confiscation.objects.filter(
                    amount=22500, officer=self.character, character=self.criminal_character,
                ).aexists()
            )

            msgs = [call[0][1] for call in mock_sys.call_args_list]
            self.assertTrue(
                any("Confiscated $22,500" in m for m in msgs),
                f"Expected confiscation alert in msgs: {msgs}",
            )
            self.assertTrue(
                any("confiscation reward" in m for m in msgs),
                f"Expected reward notification in msgs: {msgs}",
            )

    async def test_cmd_arrest_confiscation_5min_half(self):
        """Delivery at 5 minutes → 50% confiscation."""
        from amc.models import FactionMembership, FactionChoice, TeleportPoint, Delivery
        from django.contrib.gis.geos import Point

        await PoliceSession.objects.acreate(character=self.character)
        await FactionMembership.objects.acreate(
            player=self.criminal_player, faction=FactionChoice.CRIMINAL
        )
        await TeleportPoint.objects.acreate(
            name="jail", location=Point(1000, 2000, 3000)
        )

        fixed_now = timezone.now().replace(microsecond=0)
        # 5 min ago → 50% rate → int(20000 * 0.5) = 10000
        await Delivery.objects.acreate(
            character=self.criminal_character,
            cargo_key="Money", payment=20_000, quantity=1,
            timestamp=fixed_now - timedelta(minutes=5),
        )

        mock_players = self._make_player_list(
            cop_loc=(100, 200, 300),
            criminals=[((200, 200, 300), False)],
        )

        with (
            patch("amc.commands.faction.get_players", new=AsyncMock(return_value=mock_players)),
            patch("amc.commands.faction.asyncio.sleep", new=AsyncMock()),
            patch("amc.commands.faction.cache") as mock_cache,
            patch("amc.commands.faction.force_exit_vehicle", new=AsyncMock()),
            patch("amc.commands.faction.teleport_player", new=AsyncMock()),
            patch("amc.commands.faction.show_popup", new=AsyncMock()),
            patch("amc.commands.faction.send_system_message", new=AsyncMock()),
            patch("amc.commands.faction.transfer_money", new=AsyncMock()) as mock_transfer,
            patch("amc.commands.faction.record_treasury_confiscation_income", new=AsyncMock()) as mock_treasury,
            patch("amc.commands.faction.record_confiscation_for_level", new=AsyncMock()),
            patch("amc.commands.faction.send_fund_to_player_wallet", new=AsyncMock()),
            patch("amc.commands.faction.timezone") as mock_tz,
        ):
            mock_tz.now.return_value = fixed_now
            mock_cache.get.return_value = None
            await cmd_arrest(self.ctx)

            mock_transfer.assert_any_call(
                self.ctx.http_client_mod, -10000, "Money Confiscated",
                str(self.criminal_player.unique_id),
            )
            mock_treasury.assert_called_once_with(10000, "Police Confiscation")

    async def test_cmd_arrest_confiscation_9min_minimal(self):
        """Delivery at 9 minutes → 10% confiscation."""
        from amc.models import FactionMembership, FactionChoice, TeleportPoint, Delivery
        from django.contrib.gis.geos import Point

        await PoliceSession.objects.acreate(character=self.character)
        await FactionMembership.objects.acreate(
            player=self.criminal_player, faction=FactionChoice.CRIMINAL
        )
        await TeleportPoint.objects.acreate(
            name="jail", location=Point(1000, 2000, 3000)
        )

        fixed_now = timezone.now().replace(microsecond=0)
        # 9 min ago → 10% rate → int(50000 * 0.1) = 5000
        await Delivery.objects.acreate(
            character=self.criminal_character,
            cargo_key="Money", payment=50_000, quantity=1,
            timestamp=fixed_now - timedelta(minutes=9),
        )

        mock_players = self._make_player_list(
            cop_loc=(100, 200, 300),
            criminals=[((200, 200, 300), False)],
        )

        with (
            patch("amc.commands.faction.get_players", new=AsyncMock(return_value=mock_players)),
            patch("amc.commands.faction.asyncio.sleep", new=AsyncMock()),
            patch("amc.commands.faction.cache") as mock_cache,
            patch("amc.commands.faction.force_exit_vehicle", new=AsyncMock()),
            patch("amc.commands.faction.teleport_player", new=AsyncMock()),
            patch("amc.commands.faction.show_popup", new=AsyncMock()),
            patch("amc.commands.faction.send_system_message", new=AsyncMock()),
            patch("amc.commands.faction.transfer_money", new=AsyncMock()) as mock_transfer,
            patch("amc.commands.faction.record_treasury_confiscation_income", new=AsyncMock()) as mock_treasury,
            patch("amc.commands.faction.record_confiscation_for_level", new=AsyncMock()),
            patch("amc.commands.faction.send_fund_to_player_wallet", new=AsyncMock()),
            patch("amc.commands.faction.timezone") as mock_tz,
        ):
            mock_tz.now.return_value = fixed_now
            mock_cache.get.return_value = None
            await cmd_arrest(self.ctx)

            mock_transfer.assert_any_call(
                self.ctx.http_client_mod, -5000, "Money Confiscated",
                str(self.criminal_player.unique_id),
            )
            mock_treasury.assert_called_once_with(5000, "Police Confiscation")

    async def test_cmd_arrest_confiscation_expired(self):
        """Delivery at 11 minutes (outside window) → $0 confiscated."""
        from amc.models import FactionMembership, FactionChoice, TeleportPoint, Delivery, Confiscation
        from django.contrib.gis.geos import Point

        await PoliceSession.objects.acreate(character=self.character)
        await FactionMembership.objects.acreate(
            player=self.criminal_player, faction=FactionChoice.CRIMINAL
        )
        await TeleportPoint.objects.acreate(
            name="jail", location=Point(1000, 2000, 3000)
        )

        fixed_now = timezone.now().replace(microsecond=0)
        # 11 min ago — outside the 10-minute window entirely
        await Delivery.objects.acreate(
            character=self.criminal_character,
            cargo_key="Money", payment=100_000, quantity=1,
            timestamp=fixed_now - timedelta(minutes=11),
        )

        mock_players = self._make_player_list(
            cop_loc=(100, 200, 300),
            criminals=[((200, 200, 300), False)],
        )

        with (
            patch("amc.commands.faction.get_players", new=AsyncMock(return_value=mock_players)),
            patch("amc.commands.faction.asyncio.sleep", new=AsyncMock()),
            patch("amc.commands.faction.cache") as mock_cache,
            patch("amc.commands.faction.force_exit_vehicle", new=AsyncMock()),
            patch("amc.commands.faction.teleport_player", new=AsyncMock()),
            patch("amc.commands.faction.show_popup", new=AsyncMock()),
            patch("amc.commands.faction.send_system_message", new=AsyncMock()),
            patch("amc.commands.faction.transfer_money", new=AsyncMock()) as mock_transfer,
            patch("amc.commands.faction.record_treasury_confiscation_income", new=AsyncMock()) as mock_treasury,
            patch("amc.commands.faction.record_confiscation_for_level", new=AsyncMock()),
            patch("amc.commands.faction.send_fund_to_player_wallet", new=AsyncMock()),
            patch("amc.commands.faction.timezone") as mock_tz,
        ):
            mock_tz.now.return_value = fixed_now
            mock_cache.get.return_value = None
            await cmd_arrest(self.ctx)

            mock_transfer.assert_not_called()
            mock_treasury.assert_not_called()
            self.assertEqual(await Confiscation.objects.acount(), 0)

    async def test_cmd_arrest_confiscation_multi_delivery_scaling(self):
        """Multiple deliveries at different ages — each scaled independently."""
        from amc.models import FactionMembership, FactionChoice, TeleportPoint, Delivery, Confiscation
        from django.contrib.gis.geos import Point

        await PoliceSession.objects.acreate(character=self.character)
        await FactionMembership.objects.acreate(
            player=self.criminal_player, faction=FactionChoice.CRIMINAL
        )
        await TeleportPoint.objects.acreate(
            name="jail", location=Point(1000, 2000, 3000)
        )

        self.criminal_character.criminal_laundered_total = 200_000
        await self.criminal_character.asave(update_fields=["criminal_laundered_total"])

        fixed_now = timezone.now().replace(microsecond=0)
        # Delivery A: 2 min ago, $50,000 → 80% → int(50000*0.8) = 40000
        await Delivery.objects.acreate(
            character=self.criminal_character,
            cargo_key="Money", payment=50_000, quantity=1,
            timestamp=fixed_now - timedelta(minutes=2),
        )
        # Delivery B: 8 min ago, $30,000 → 20% → int(30000*0.2) = 6000
        await Delivery.objects.acreate(
            character=self.criminal_character,
            cargo_key="Money", payment=30_000, quantity=1,
            timestamp=fixed_now - timedelta(minutes=8),
        )
        # Total: 40000 + 6000 = 46000

        mock_players = self._make_player_list(
            cop_loc=(100, 200, 300),
            criminals=[((200, 200, 300), False)],
        )

        with (
            patch("amc.commands.faction.get_players", new=AsyncMock(return_value=mock_players)),
            patch("amc.commands.faction.asyncio.sleep", new=AsyncMock()),
            patch("amc.commands.faction.cache") as mock_cache,
            patch("amc.commands.faction.force_exit_vehicle", new=AsyncMock()),
            patch("amc.commands.faction.teleport_player", new=AsyncMock()),
            patch("amc.commands.faction.show_popup", new=AsyncMock()),
            patch("amc.commands.faction.send_system_message", new=AsyncMock()),
            patch("amc.commands.faction.transfer_money", new=AsyncMock()) as mock_transfer,
            patch("amc.commands.faction.record_treasury_confiscation_income", new=AsyncMock()) as mock_treasury,
            patch("amc.commands.faction.record_confiscation_for_level", new=AsyncMock()),
            patch("amc.commands.faction.send_fund_to_player_wallet", new=AsyncMock()),
            patch("amc.commands.faction.timezone") as mock_tz,
        ):
            mock_tz.now.return_value = fixed_now
            mock_cache.get.return_value = None
            await cmd_arrest(self.ctx)

            mock_transfer.assert_any_call(
                self.ctx.http_client_mod, -46000, "Money Confiscated",
                str(self.criminal_player.unique_id),
            )
            mock_treasury.assert_called_once_with(46000, "Police Confiscation")

            await self.criminal_character.arefresh_from_db(fields=["criminal_laundered_total"])
            self.assertEqual(self.criminal_character.criminal_laundered_total, 154_000)

            self.assertTrue(
                await Confiscation.objects.filter(amount=46000).aexists()
            )

    async def test_cmd_arrest_no_jail(self):
        """Jail TeleportPoint doesn't exist → error message."""
        from amc.models import FactionMembership, FactionChoice

        await PoliceSession.objects.acreate(character=self.character)
        await FactionMembership.objects.acreate(
            player=self.criminal_player, faction=FactionChoice.CRIMINAL
        )

        mock_players = self._make_player_list(
            cop_loc=(100, 200, 300), criminals=[((200, 200, 300), False)]
        )

        with (
            patch(
                "amc.commands.faction.get_players",
                new=AsyncMock(return_value=mock_players),
            ),
            patch("amc.commands.faction.asyncio.sleep", new=AsyncMock()),
            patch("amc.commands.faction.cache") as mock_cache,
            patch("amc.commands.faction.send_system_message", new=AsyncMock()) as mock_sys,
        ):
            mock_cache.get.return_value = None
            await cmd_arrest(self.ctx)
            msgs = [call[0][1] for call in mock_sys.call_args_list]
            self.assertTrue(
                any("Jail" in m or "jail" in m.lower() for m in msgs),
                f"Expected jail error in msgs: {msgs}",
            )

    async def test_cmd_arrest_inside_zone(self):
        """Cop inside an active ArrestZone → arrest proceeds normally."""
        from amc.models import ArrestZone, FactionMembership, FactionChoice, TeleportPoint
        from django.contrib.gis.geos import Point, Polygon

        await PoliceSession.objects.acreate(character=self.character)
        await FactionMembership.objects.acreate(
            player=self.criminal_player, faction=FactionChoice.CRIMINAL
        )
        await TeleportPoint.objects.acreate(
            name="jail", location=Point(1000, 2000, 3000)
        )

        # Create a zone containing the cop's position (100, 200)
        zone_polygon = Polygon(
            ((0, 0), (500, 0), (500, 500), (0, 500), (0, 0)),
            srid=3857,
        )
        await ArrestZone.objects.acreate(
            name="Downtown", polygon=zone_polygon, active=True
        )

        mock_players = self._make_player_list(
            cop_loc=(100, 200, 300),
            criminals=[((200, 200, 300), False)],
        )

        with (
            patch(
                "amc.commands.faction.get_players",
                new=AsyncMock(return_value=mock_players),
            ),
            patch("amc.commands.faction.asyncio.sleep", new=AsyncMock()),
            patch("amc.commands.faction.cache") as mock_cache,
            patch("amc.commands.faction.force_exit_vehicle", new=AsyncMock()),
            patch("amc.commands.faction.teleport_player", new=AsyncMock()) as mock_tp,
            patch("amc.commands.faction.show_popup", new=AsyncMock()),
            patch("amc.commands.faction.send_system_message", new=AsyncMock()),
        ):
            mock_cache.get.return_value = None
            await cmd_arrest(self.ctx)

            # Arrest should succeed
            mock_tp.assert_called()
            self.ctx.announce.assert_called()
            self.assertIn("arrested", self.ctx.announce.call_args[0][0])

    async def test_cmd_arrest_outside_zone(self):
        """Cop outside all active ArrestZones → arrest rejected."""
        from amc.models import ArrestZone, FactionMembership, FactionChoice
        from django.contrib.gis.geos import Polygon

        await PoliceSession.objects.acreate(character=self.character)
        await FactionMembership.objects.acreate(
            player=self.criminal_player, faction=FactionChoice.CRIMINAL
        )

        # Create a zone far from the cop's position (100, 200)
        zone_polygon = Polygon(
            ((5000, 5000), (6000, 5000), (6000, 6000), (5000, 6000), (5000, 5000)),
            srid=3857,
        )
        await ArrestZone.objects.acreate(
            name="Other Area", polygon=zone_polygon, active=True
        )

        mock_players = self._make_player_list(
            cop_loc=(100, 200, 300),
            criminals=[((200, 200, 300), False)],
        )

        with (
            patch(
                "amc.commands.faction.get_players",
                new=AsyncMock(return_value=mock_players),
            ),
            patch("amc.commands.faction.cache") as mock_cache,
            patch("amc.commands.faction.send_system_message", new=AsyncMock()) as mock_sys,
        ):
            mock_cache.get.return_value = None
            await cmd_arrest(self.ctx)

            mock_sys.assert_called()
            msgs = [call[0][1] for call in mock_sys.call_args_list]
            self.assertTrue(
                any("designated" in m.lower() or "arrest zone" in m.lower() for m in msgs),
                f"Expected zone rejection in msgs: {msgs}",
            )

    async def test_cmd_arrest_no_zones_defined(self):
        """No ArrestZone rows → arrests allowed everywhere (backward-compat)."""
        from amc.models import ArrestZone, FactionMembership, FactionChoice, TeleportPoint
        from django.contrib.gis.geos import Point

        await PoliceSession.objects.acreate(character=self.character)
        await FactionMembership.objects.acreate(
            player=self.criminal_player, faction=FactionChoice.CRIMINAL
        )
        await TeleportPoint.objects.acreate(
            name="jail", location=Point(1000, 2000, 3000)
        )

        # Ensure no ArrestZone exists
        self.assertEqual(await ArrestZone.objects.acount(), 0)

        mock_players = self._make_player_list(
            cop_loc=(100, 200, 300),
            criminals=[((200, 200, 300), False)],
        )

        with (
            patch(
                "amc.commands.faction.get_players",
                new=AsyncMock(return_value=mock_players),
            ),
            patch("amc.commands.faction.asyncio.sleep", new=AsyncMock()),
            patch("amc.commands.faction.cache") as mock_cache,
            patch("amc.commands.faction.force_exit_vehicle", new=AsyncMock()),
            patch("amc.commands.faction.teleport_player", new=AsyncMock()) as mock_tp,
            patch("amc.commands.faction.show_popup", new=AsyncMock()),
            patch("amc.commands.faction.send_system_message", new=AsyncMock()),
        ):
            mock_cache.get.return_value = None
            await cmd_arrest(self.ctx)

            # Arrest should succeed without any zones
            mock_tp.assert_called()
            self.ctx.announce.assert_called()
            self.assertIn("arrested", self.ctx.announce.call_args[0][0])

    # --- Wanted Command Tests ---

    async def test_cmd_wanted_no_records(self):
        """No active criminal records → reply 'No wanted criminals'."""
        await cmd_wanted(self.ctx)
        self.ctx.reply.assert_called_with("No wanted criminals")

    async def test_cmd_wanted_with_records(self):
        """Active records shown grouped online-first, sorted by criminal level desc."""
        now = timezone.now()

        # Create characters with different criminal levels (IDs avoid setUp collision)
        player_a = await Player.objects.acreate(unique_id="76561198000000101")
        char_online = await Character.objects.acreate(
            name="OnlineCriminal", player=player_a, guid="guid-online",
            criminal_laundered_total=250_000,  # level 6
        )
        player_b = await Player.objects.acreate(unique_id="76561198000000102")
        char_offline = await Character.objects.acreate(
            name="OfflineCriminal", player=player_b, guid="guid-offline",
            criminal_laundered_total=500_000,  # level 11
        )
        player_c = await Player.objects.acreate(unique_id="76561198000000103")
        char_online2 = await Character.objects.acreate(
            name="OnlineCriminal2", player=player_c, guid="guid-online2",
            criminal_laundered_total=100_000,  # level 3
        )

        # Create active records
        await CriminalRecord.objects.acreate(
            character=char_online, reason="Money delivery",
            expires_at=now + timedelta(days=3),
        )
        await CriminalRecord.objects.acreate(
            character=char_offline, reason="Money delivery",
            expires_at=now + timedelta(days=5),
        )
        await CriminalRecord.objects.acreate(
            character=char_online2, reason="Money delivery",
            expires_at=now + timedelta(days=1),
        )

        # Mock online players: only guid-online and guid-online2 are online
        mock_players = [
            ("76561198000000101", {"character_guid": "guid-online", "name": "OnlineCriminal"}),
            ("76561198000000103", {"character_guid": "guid-online2", "name": "OnlineCriminal2"}),
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
        self.assertIn("Online", output)
        self.assertIn("Offline", output)

        # Online section: C6 before C3 (higher level first)
        online_pos_c6 = output.index("OnlineCriminal <")
        online_pos_c3 = output.index("OnlineCriminal2")
        self.assertLess(online_pos_c6, online_pos_c3)

        # Offline section: C11
        self.assertIn("OfflineCriminal", output)
        self.assertIn("C11", output)

        # Smuggled amounts shown
        self.assertIn("$250,000", output)
        self.assertIn("$500,000", output)
        self.assertIn("$100,000", output)

        # Online section appears before Offline
        online_section = output.index("<Title>Online</>")
        offline_section = output.index("<Title>Offline</>")
        self.assertLess(online_section, offline_section)

    async def test_cmd_wanted_expired_excluded(self):
        """Expired records are not shown."""
        now = timezone.now()
        player_x = await Player.objects.acreate(unique_id="76561198000000104")
        char = await Character.objects.acreate(
            name="ExpiredCriminal", player=player_x, guid="guid-expired",
            criminal_laundered_total=100_000,
        )
        await CriminalRecord.objects.acreate(
            character=char, reason="Money delivery",
            expires_at=now - timedelta(days=1),  # expired yesterday
        )

        with patch(
            "amc.commands.wanted.get_players",
            new=AsyncMock(return_value=[]),
        ):
            await cmd_wanted(self.ctx)

        self.ctx.reply.assert_called_with("No wanted criminals")

    async def test_cmd_wanted_excludes_active_police(self):
        """Characters with active police sessions are excluded from wanted list."""
        now = timezone.now()

        # Criminal with active police session — should be excluded
        player_cop = await Player.objects.acreate(unique_id="76561198000000201")
        char_cop = await Character.objects.acreate(
            name="CopWithRecord", player=player_cop, guid="guid-cop-record",
            criminal_laundered_total=300_000,
        )
        await CriminalRecord.objects.acreate(
            character=char_cop, reason="Money delivery",
            expires_at=now + timedelta(days=3),
        )
        await PoliceSession.objects.acreate(character=char_cop)  # active session

        # Regular criminal — should appear
        player_crim = await Player.objects.acreate(unique_id="76561198000000202")
        char_crim = await Character.objects.acreate(
            name="RegularCriminal", player=player_crim, guid="guid-regular-crim",
            criminal_laundered_total=200_000,
        )
        await CriminalRecord.objects.acreate(
            character=char_crim, reason="Money delivery",
            expires_at=now + timedelta(days=5),
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
