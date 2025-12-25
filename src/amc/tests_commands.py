from django.test import SimpleTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass
from datetime import timedelta
from django.utils import timezone
from amc.command_framework import registry, CommandContext, CommandRegistry
from amc import commands  # Ensure commands are registered
from amc.models import Character, Player
# Import other models as needed for mocking or actual DB tests if we go that route

class RentGroupingTestCase(SimpleTestCase):
    def setUp(self):
        self.ctx = MagicMock(spec=CommandContext)
        self.ctx.reply = AsyncMock()
        self.ctx.announce = AsyncMock()
        self.ctx.http_client_mod = MagicMock()
        self.ctx.character = MagicMock()
        self.ctx.player = MagicMock()
        self.ctx.player_info = {}

    async def test_cmd_rent_grouping(self):
        from amc import commands
        
        # Create mock vehicles with different companies
        v1 = MagicMock()
        v1.id = 1
        v1.config = {'CompanyName': 'Alpha Corp', 'VehicleName': 'Car A'}
        
        v2 = MagicMock()
        v2.id = 2
        v2.config = {'CompanyName': 'Beta Inc', 'VehicleName': 'Car B'}
        
        v3 = MagicMock()
        v3.id = 3
        v3.config = {'CompanyName': 'Alpha Corp', 'VehicleName': 'Car C'}

        # Mock objects.filter to return these
        # We need to mock VehicleName formatting call inside cmd_rent if used?
        # Actually cmd_rent reads vehicle_id.strip() or lists all.
        
        with patch('amc.models.CharacterVehicle.objects.filter') as mock_filter:
            mock_filter.return_value.__aiter__.return_value = [v1, v2, v3]
            
            await commands.cmd_rent(self.ctx, "")
            
            self.ctx.reply.assert_called()
            output = self.ctx.reply.call_args[0][0]
            
            # Check for company headers
            self.assertIn("Alpha Corp", output)
            self.assertIn("Beta Inc", output)
            
            # Check for vehicle listings
            self.assertIn("Car A", output)
            self.assertIn("Car B", output)


class CommandRegistryTestCase(SimpleTestCase):
    def setUp(self):
        self.registry = CommandRegistry()
        
    def test_build_regex_simple(self):
        async def func(ctx, arg1: str): pass
        
        # Test registering
        decorator = self.registry.register("/test")
        decorator(func)
        
        cmd = self.registry.commands[0]
        pattern = cmd['pattern']
        
        self.assertTrue(pattern.match("/test hello"))
        match = pattern.match("/test hello")
        self.assertEqual(match.group('arg1'), "hello")

    def test_build_regex_int(self):
        async def func(ctx, number: int): pass
        decorator = self.registry.register("/num")
        decorator(func)
        
        cmd = self.registry.commands[0]
        match = cmd['pattern'].match("/num 123")
        self.assertTrue(match)
        self.assertEqual(match.group('number'), "123")
        
        match = cmd['pattern'].match("/num -50")
        self.assertTrue(match)
        self.assertEqual(match.group('number'), "-50")

    def test_build_regex_multiple_args(self):
        async def func(ctx, name: str, age: int): pass
        decorator = self.registry.register("/person")
        decorator(func)
        
        match = self.registry.commands[0]['pattern'].match("/person John 30")
        self.assertTrue(match)
        self.assertEqual(match.group('name'), "John")
        self.assertEqual(match.group('age'), "30")

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

class MockResponse:
    def __init__(self, data=None):
        self.status = 200
        self.data = data or {}
        self.json = AsyncMock(return_value=self.data)
        
    def __await__(self):
        async def _ret(): return self
        return _ret().__await__()
        
    async def __aenter__(self):
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
            name="TestChar",
            player=self.player,
            guid="guid-123"
        )
        self.ctx.character = self.character
        self.ctx.player = self.player
        self.ctx.timestamp = timezone.now()

    async def test_cmd_help(self):
        from amc import commands
        
        # Test that we get a reply
        await commands.cmd_help(self.ctx)
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
        from amc import commands
        await commands.cmd_credits(self.ctx)
        self.ctx.reply.assert_called()

    async def test_cmd_bank(self):
        from amc import commands
        mock_bal = 1000
        mock_loan = 500
        mock_max_loan = 5000
        
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
        
        with patch('amc.commands.finance.get_player_bank_balance', new=AsyncMock(return_value=mock_bal)), \
             patch('amc.commands.finance.get_player_loan_balance', new=AsyncMock(return_value=mock_loan)), \
             patch('amc.commands.finance.get_character_max_loan', new=AsyncMock(return_value=(mock_max_loan, 'Ok'))), \
             patch('amc_finance.models.LedgerEntry.objects.filter') as mock_filter:

             mock_filter.return_value.select_related.return_value.order_by.return_value.__getitem__.return_value = mock_slice

             await commands.cmd_bank(self.ctx)
            
             self.ctx.reply.assert_called()
             output = self.ctx.reply.call_args[0][0]
             self.assertIn("<Title>Your Bank ASEAN Account</>", output)
             self.assertIn("<Bold>Balance:</> <Money>1,000</>", output)
             self.assertIn("Test Tx", output)
             self.assertIn("Daily (IRL) Interest Rate", output)

    async def test_cmd_tp_admin(self):
        from amc import commands
        self.ctx.player_info['bIsAdmin'] = True
        with patch('amc.commands.teleport.teleport_player', new=AsyncMock()) as mock_tp:
            await commands.cmd_tp_coords(self.ctx, 100, 200, 300)
            mock_tp.assert_called_with(self.ctx.http_client_mod, "76561198000000000", {'X': 100, 'Y': 200, 'Z': 300}, no_vehicles=False)

    async def test_cmd_tp_non_admin(self):
        from amc import commands
        self.ctx.player_info['bIsAdmin'] = False
        with patch('amc.commands.teleport.teleport_player', new=AsyncMock()) as mock_tp:
            await commands.cmd_tp_coords(self.ctx, 100, 200, 300)
            mock_tp.assert_not_called()
            self.ctx.reply.assert_called_with("Admin Only")

    async def test_cmd_tp_name(self):
        from amc import commands
        from amc.models import TeleportPoint
        
        mock_tp = MagicMock()
        mock_tp.location.x = 100
        mock_tp.location.y = 200
        mock_tp.location.z = 300
        
        # Test valid name
        with patch('amc.models.TeleportPoint.objects.aget', new=AsyncMock(return_value=mock_tp)), \
             patch('amc.commands.teleport.teleport_player', new=AsyncMock()) as mock_teleport:
            await commands.cmd_tp_name(self.ctx, "Home")
            mock_teleport.assert_called_with(
                self.ctx.http_client_mod,
                self.ctx.player.unique_id,
                {'X': 100, 'Y': 200, 'Z': 300},
                no_vehicles=True, # Non-admin defaults to True
                reset_trailers=True,
                reset_carried_vehicles=True
            )

        # Test invalid name (popup)
        with patch('amc.models.TeleportPoint.objects.aget', side_effect=TeleportPoint.DoesNotExist), \
             patch('amc.commands.teleport.show_popup', new=AsyncMock()) as mock_popup:
             await commands.cmd_tp_name(self.ctx, "Invalid")
             mock_popup.assert_called()

        # Test empty name (Usage popup)
        with patch('amc.commands.teleport.show_popup', new=AsyncMock()) as mock_popup:
            await commands.cmd_tp_name(self.ctx, "")
            mock_popup.assert_called()
            args = mock_popup.call_args[0]
            self.assertIn("Choose from one of the following locations", args[1])

    async def test_cmd_donate_flow(self):
        from amc import commands
        from amc.utils import generate_verification_code
        
        self.ctx.character.id = 1
        amount = 500
        code = generate_verification_code((amount, self.ctx.character.id))
        
        # 1. First call without code
        await commands.cmd_donate(self.ctx, "500", "")
        self.ctx.reply.assert_called()
        args, _ = self.ctx.reply.call_args
        self.assertIn("Confirm:", args[0])
        
        # 2. Second call with code
        with patch('amc.commands.finance.register_player_withdrawal', new=AsyncMock()) as mock_withdraw, \
             patch('amc.commands.finance.player_donation', new=AsyncMock()) as mock_donate:
             
             await commands.cmd_donate(self.ctx, "500", code)
             
             mock_withdraw.assert_called_with(500, self.ctx.character, self.ctx.player)
             mock_donate.assert_called_with(500, self.ctx.character)
             self.assertTrue(self.ctx.reply.call_count >= 2) # Confirm reply sent


    async def test_integration_registry_execute(self):
        """
        Test that the registry actually routes a string to a command function.
        """
        with patch('amc.commands.general.cmd_help', new=AsyncMock()) as mock_help:
             self.ctx.reply.reset_mock()
             result = await registry.execute("/help", self.ctx)
             self.assertTrue(result)
             # Side effect depends on what we mocked. 
             # If we mocked cmd_help, stripped of side effects, we just check return True.

    # --- General Info Tests ---

    async def test_cmd_coords(self):
        from amc import commands
        with patch('amc.commands.general.get_player', new=AsyncMock(return_value={'Location': {'X': 100.5, 'Y': 200.5, 'Z': 300.5}})):
            await commands.cmd_coords(self.ctx)
            self.ctx.announce.assert_called_with("100, 200, 300")

    async def test_cmd_shortcutcheck(self):
        from amc import commands
        # Mock CharacterLocation exists checks
        with patch('amc.models.CharacterLocation.objects.filter') as mock_filter:
            mock_qs = MagicMock()
            mock_qs.aexists = AsyncMock(side_effect=[True, False]) # True for in_shortcut, False for used_shortcut
            mock_filter.return_value = mock_qs
            
            await commands.cmd_shortcutcheck(self.ctx)
            self.ctx.reply.assert_called()
            args, _ = self.ctx.reply.call_args
            self.assertIn("inside the forbidden zone", args[0])

    # --- Decal Tests ---

    async def test_cmd_decals(self):
        from amc import commands
        # Mock VehicleDecal queryset iteration
        mock_decal = MagicMock()
        mock_decal.hash = "1234567890"
        mock_decal.name = "Test Decal"
        mock_decal.vehicle_key = "Jemusi"
        
        # Async iterator mock is tricky, let's patch objects.filter
        with patch('amc.models.VehicleDecal.objects.filter') as mock_filter:
            mock_qs = MagicMock()
            mock_qs.__aiter__.return_value = [mock_decal]
            mock_filter.return_value = mock_qs
            
            await commands.cmd_decals(self.ctx)
            self.ctx.reply.assert_called()
            args, _ = self.ctx.reply.call_args
            self.assertIn("Test Decal", args[0])

    async def test_cmd_save_decal(self):
        from amc import commands
        decal_config = {'some': 'config'}
        self.ctx.player_info = {'VehicleKey': 'Truck'}
        
        with patch('amc.commands.decals.get_decal', new=AsyncMock(return_value=decal_config)), \
             patch('amc.models.VehicleDecal.calculate_hash', return_value="hash123"), \
             patch('amc.models.VehicleDecal.objects.acreate', new=AsyncMock()) as mock_create:
            
            mock_create.return_value = MagicMock(name="NewDecal", hash="hash123")
            
            await commands.cmd_save_decal(self.ctx, "MyDecal")
            
            mock_create.assert_called()
            self.ctx.reply.assert_called()

    async def test_cmd_apply_decal(self):
        from amc import commands
        from amc.models import VehicleDecal
        mock_decal = MagicMock()
        mock_decal.config = {'color': 'red'}
        
        with patch('amc.models.VehicleDecal.objects.aget', new=AsyncMock(return_value=mock_decal)), \
             patch('amc.commands.decals.set_decal', new=AsyncMock()) as mock_set:
            
            await commands.cmd_apply_decal(self.ctx, "DecalName")
            mock_set.assert_called_with(self.ctx.http_client_mod, "76561198000000000", {'color': 'red'})

    async def test_cmd_apply_decal_not_found(self):
        from amc import commands
        from amc.models import VehicleDecal
        
        async def async_iter(items):
            for i in items:
                yield i
        
        mock_qs = MagicMock()
        mock_qs.__aiter__.side_effect = lambda: async_iter([])

        with patch('amc.models.VehicleDecal.objects.aget', new=AsyncMock(side_effect=VehicleDecal.DoesNotExist)), \
             patch('amc.models.VehicleDecal.objects.filter', return_value=mock_qs):
            
            await commands.cmd_apply_decal(self.ctx, "Missing")
            self.ctx.reply.assert_called()
            args, _ = self.ctx.reply.call_args
            self.assertIn("Decal not found", args[0])

    # --- Jobs & Economy Tests ---

    async def test_cmd_jobs(self):
        from amc import commands
        mock_job = MagicMock()
        mock_job.quantity_fulfilled = 0
        mock_job.quantity_requested = 10
        mock_job.name = "Test Job"
        mock_job.bonus_multiplier = 0.5
        mock_job.completion_bonus = 1000
        mock_job.rp_mode = False
        mock_job.expired_at = self.ctx.timestamp + timedelta(hours=1)
        mock_job.get_cargo_key_display.return_value = "Boxes"

        with patch('amc.models.DeliveryJob.objects.filter') as mock_filter:
            mock_qs = MagicMock()
            mock_qs.prefetch_related.return_value.__aiter__.return_value = [mock_job]
            mock_filter.return_value = mock_qs
            
            sp_mock = MagicMock(spec=['name']); sp_mock.name = 'Source A'
            dp_mock = MagicMock(spec=['name']); dp_mock.name = 'Dest B'
            mock_job.source_points.all.return_value = [sp_mock]
            mock_job.destination_points.all.return_value = [dp_mock]

            with patch('amc.commands.jobs.get_rp_mode', new=AsyncMock(return_value=False)):
                 await commands.cmd_jobs(self.ctx)
                 self.ctx.reply.assert_called()
                 args, _ = self.ctx.reply.call_args
                 self.assertIn("Test Job", args[0])
                 self.assertIn("Source A", args[0])
                 self.assertIn("Dest B", args[0])

    async def test_cmd_subsidies(self):
        from amc import commands
        await commands.cmd_subsidies(self.ctx)
        self.ctx.reply.assert_called()

    # --- Events & Racing Tests ---

    async def test_cmd_staggered_start(self):
        from amc import commands
        mock_event = MagicMock()
        
        with patch('amc.models.GameEvent.objects.filter') as mock_filter:
            mock_qs = MagicMock()
            mock_qs.select_related.return_value.alatest = AsyncMock(return_value=mock_event)
            mock_filter.return_value = mock_qs
            
            with patch('amc.commands.events.staggered_start', new=AsyncMock()) as mock_start:
                await commands.cmd_staggered_start(self.ctx, 5)
                mock_start.assert_called()

    async def test_cmd_auto_grid(self):
        from amc import commands
        mock_event = MagicMock()
        
        with patch('amc.models.GameEvent.objects.filter') as mock_filter:
            mock_qs = MagicMock()
            mock_qs.select_related.return_value.alatest = AsyncMock(return_value=mock_event)
            mock_filter.return_value = mock_qs
            
            with patch('amc.commands.events.auto_starting_grid', new=AsyncMock()) as mock_grid:
                await commands.cmd_auto_grid(self.ctx)
                mock_grid.assert_called()

    async def test_cmd_results(self):
        from amc import commands
        mock_event = MagicMock()
        
        with patch('amc.models.ScheduledEvent.objects.filter_active_at') as mock_filter:
            mock_qs = MagicMock()
            mock_qs.select_related.return_value.afirst = AsyncMock(return_value=mock_event)
            mock_filter.return_value = mock_qs
            
            with patch('amc.commands.events.show_scheduled_event_results_popup', new=AsyncMock()) as mock_popup:
                await commands.cmd_results(self.ctx)
                mock_popup.assert_called()

    async def test_cmd_setup_event(self):
        from amc import commands
        mock_event = MagicMock()
        
        with patch('amc.models.ScheduledEvent.objects.select_related') as mock_sl:
             # Logic is complex, mocking filter chains
             mock_qs = MagicMock()
             mock_qs.filter.return_value.afirst = AsyncMock(return_value=mock_event)
             # Handle active_at shortcut too
             # Let's mock filter_active_at on manager instead if possible or chain
             pass 

        # Creating a more robust mock for filter chains
        with patch('amc.models.ScheduledEvent.objects.filter_active_at') as mock_filter_active:
             mock_filter_active.return_value.select_related.return_value.filter.return_value.afirst = AsyncMock(return_value=mock_event)
             
             with patch('amc.commands.events.setup_event', new=AsyncMock(return_value=True)) as mock_setup:
                await commands.cmd_setup_event(self.ctx)
                mock_setup.assert_called()

    async def test_cmd_events_list(self):
        from amc import commands
        mock_event = MagicMock()
        mock_event.name = "Race"
        mock_event.start_time = self.ctx.timestamp + timedelta(hours=1)
        mock_event.end_time = self.ctx.timestamp + timedelta(hours=2)
        mock_event.description = "Test Description"
        
        with patch('amc.models.ScheduledEvent.objects.filter') as mock_filter:
            mock_qs = MagicMock()
            mock_qs.order_by.return_value.__aiter__.return_value = [mock_event]
            mock_filter.return_value = mock_qs
            
            await commands.cmd_events_list(self.ctx)
            self.ctx.reply.assert_called()
            self.assertIn("Race", self.ctx.reply.call_args[0][0])

    async def test_cmd_countdown(self):
        from amc import commands
        with patch('amc.commands.events.countdown', new=AsyncMock()) as mock_cd:
             await commands.cmd_countdown(self.ctx)
             mock_cd.assert_called()

    # --- RP Mode & Rescue Tests ---

    async def test_cmd_rp_mode_info(self):
        from amc import commands
        with patch('amc.commands.rp_rescue.get_rp_mode', new=AsyncMock(return_value=False)):
            await commands.cmd_rp_mode(self.ctx)
            self.ctx.reply.assert_called()
            args, _ = self.ctx.reply.call_args
            self.assertIn("Status: <Warning>OFF</>", args[0])

    async def test_cmd_rp_mode_toggle_invalid(self):
        from amc import commands
        with patch('amc.commands.rp_rescue.get_rp_mode', new=AsyncMock(return_value=False)):
            await commands.cmd_rp_mode(self.ctx, "WRONGCODE")
            self.ctx.reply.assert_called()
            args, _ = self.ctx.reply.call_args
            self.assertIn("Code Incorrect", args[0])

    async def test_cmd_rp_mode_toggle_valid(self):
        from amc import commands
        from amc.utils import generate_verification_code
        
        # Calculate valid code
        # The logic in commands.py uses: code_gen, code_verified = with_verification_code((ctx.character.guid, is_rp_mode), verification_code)
        # We need to match that.
        is_rp = False
        code = generate_verification_code((self.ctx.character.guid, is_rp))
        
        with patch('amc.commands.rp_rescue.get_rp_mode', new=AsyncMock(side_effect=[False, True])): # Initial -> After toggle
            with patch('amc.commands.rp_rescue.toggle_rp_session', new=AsyncMock()) as mock_toggle, \
                 patch('amc.commands.rp_rescue.despawn_player_vehicle', new=AsyncMock()) as mock_despawn, \
                 patch('amc.commands.rp_rescue.despawn_by_tag', new=AsyncMock()) as mock_dbt, \
                 patch('amc.commands.rp_rescue.set_character_name', new=AsyncMock()) as mock_set_name:
                
                await commands.cmd_rp_mode(self.ctx, code)
                mock_toggle.assert_called()
                self.ctx.reply.assert_called()

    async def test_cmd_rescue_cooldown(self):
        from amc import commands
        with patch('amc.models.RescueRequest.objects.filter') as mock_filter:
            mock_filter.return_value.aexists = AsyncMock(return_value=True) # Recently requested
            
            await commands.cmd_rescue(self.ctx, "Help!")
            self.ctx.reply.assert_called_with("You have requested a rescue less than 5 minutes ago")

    async def test_cmd_rescue_success(self):
        from amc import commands
        mock_req = MagicMock()
        mock_req.id = 123
        self.ctx.is_current_event = True
        
        with patch('amc.models.RescueRequest.objects.filter') as mock_filter, \
             patch('amc.commands.rp_rescue.get_players_mod', new=AsyncMock(return_value=[])), \
             patch('amc.commands.rp_rescue.list_player_vehicles', new=AsyncMock(return_value={})), \
             patch('amc.models.RescueRequest.objects.acreate', new=AsyncMock(return_value=mock_req)):
            
            mock_filter.return_value.aexists = AsyncMock(return_value=False)
            
            await commands.cmd_rescue(self.ctx, "Help!")
            self.ctx.announce.assert_called()
            self.ctx.reply.assert_called()

    async def test_cmd_respond(self):
        from amc import commands
        mock_req = MagicMock()
        mock_req.discord_message_id = 123
        
        # Setup discord mocks
        mock_cog = MagicMock()
        mock_cog.add_reaction_to_rescue_message = AsyncMock() # ERR FIX: needs to return coroutine
        self.ctx.discord_client = MagicMock()
        self.ctx.discord_client.get_cog.return_value = mock_cog
        self.ctx.discord_client.loop = MagicMock() # Needs loop for run_coroutine_threadsafe
        
        with patch('amc.models.RescueRequest.objects.select_related') as mock_sr, \
             patch('asyncio.run_coroutine_threadsafe') as mock_run:
             mock_sr.return_value.aget = AsyncMock(return_value=mock_req)
             mock_req.responders.aadd = AsyncMock() # ERR FIX
             
             await commands.cmd_respond(self.ctx, 123)
             mock_req.responders.aadd.assert_called_with(self.ctx.player)
             
             # Handle unawaited coroutine from run_coroutine_threadsafe
             if mock_run.called:
                 coro = mock_run.call_args[0][0]
                 coro.close()
             self.ctx.announce.assert_called()

    # --- Admin & Spawning Tests ---

    async def test_cmd_tp_player(self):
        from amc import commands
        from amc.models import TeleportPoint
        self.ctx.player_info['bIsAdmin'] = True
        
        
        # Mock Data with Multiple Players to test fuzzy selection
        mock_p1 = {'name': 'TargetPlayer', 'character_guid': 'guid-1', 'player_id': 'pid-1'}
        mock_p2 = {'name': 'TargetDummy', 'character_guid': 'guid-2', 'player_id': 'pid-2'}
        mock_p3 = {'name': 'OtherPerson', 'character_guid': 'guid-3', 'player_id': 'pid-3'}
        
        # get_players returns a list of tuples (unique_id, player_dict)
        mock_players = [
            ('pid-1', mock_p1),
            ('pid-2', mock_p2),
            ('pid-3', mock_p3)
        ]
        
        mock_tp = MagicMock()
        mock_tp.location.x = 100
        mock_tp.location.y = 200
        mock_tp.location.z = 300
        
        # Test Successful Teleport (Exact)
        with patch('amc.commands.admin.get_players', new=AsyncMock(return_value=mock_players)), \
             patch('amc.models.TeleportPoint.objects.aget', new=AsyncMock(return_value=mock_tp)), \
             patch('amc.commands.admin.teleport_player', new=AsyncMock()) as mock_teleport:
            
            await commands.cmd_tp_player(self.ctx, "TargetPlayer", "Home")
            
            mock_teleport.assert_called_with(
                self.ctx.http_client_mod,
                "pid-1",
                {'X': 100, 'Y': 200, 'Z': 300},
                no_vehicles=False,
                reset_trailers=False,
                reset_carried_vehicles=False
            )

        # Test Successful Teleport (Fuzzy - "TargetP" should match "TargetPlayer" better than "TargetDummy")
        with patch('amc.commands.admin.get_players', new=AsyncMock(return_value=mock_players)), \
             patch('amc.models.TeleportPoint.objects.aget', new=AsyncMock(return_value=mock_tp)), \
             patch('amc.commands.admin.teleport_player', new=AsyncMock()) as mock_teleport:
            
            await commands.cmd_tp_player(self.ctx, "TargetP", "Home")
            
            mock_teleport.assert_called_with(
                self.ctx.http_client_mod,
                "pid-1",
                {'X': 100, 'Y': 200, 'Z': 300},
                no_vehicles=False,
                reset_trailers=False,
                reset_carried_vehicles=False
            )

        # Test Successful Teleport (Fuzzy - "Dummy" should match "TargetDummy")
        with patch('amc.commands.admin.get_players', new=AsyncMock(return_value=mock_players)), \
             patch('amc.models.TeleportPoint.objects.aget', new=AsyncMock(return_value=mock_tp)), \
             patch('amc.commands.admin.teleport_player', new=AsyncMock()) as mock_teleport:
            
            await commands.cmd_tp_player(self.ctx, "Dummy", "Home")
            
            mock_teleport.assert_called_with(
                self.ctx.http_client_mod,
                "pid-2",
                {'X': 100, 'Y': 200, 'Z': 300},
                no_vehicles=False,
                reset_trailers=False,
                reset_carried_vehicles=False
            )

        # Test Player Not Found
        with patch('amc.commands.admin.get_players', new=AsyncMock(return_value={})), \
             patch('amc.commands.admin.show_popup', new=AsyncMock()) as mock_popup:
            
            await commands.cmd_tp_player(self.ctx, "Ghost", "Home")
            mock_popup.assert_called()
            self.assertIn("Player not found", mock_popup.call_args[0][1])

        # Test Location Not Found
        with patch('amc.commands.admin.get_players', new=AsyncMock(return_value=mock_players)), \
             patch('amc.models.TeleportPoint.objects.aget', side_effect=TeleportPoint.DoesNotExist), \
             patch('amc.commands.admin.show_popup', new=AsyncMock()) as mock_popup:
            
            await commands.cmd_tp_player(self.ctx, "Target", "InvalidLoc")
            mock_popup.assert_called()
            self.assertIn("Teleport point not found", mock_popup.call_args[0][1])

    async def test_cmd_spawn_displays(self):
        from amc import commands
        self.ctx.player_info['bIsAdmin'] = True
        mock_v = MagicMock()
        mock_v.id = 1
        mock_v.character = MagicMock() # has character
        
        with patch('amc.models.CharacterVehicle.objects.select_related') as mock_qs, \
             patch('amc.commands.admin.despawn_by_tag', new=AsyncMock()), \
             patch('amc.commands.admin.spawn_registered_vehicle', new=AsyncMock()) as mock_spawn:
            
            mock_qs.return_value.filter.return_value.__aiter__.return_value = [mock_v]
            
            await commands.cmd_spawn_displays(self.ctx)
            mock_spawn.assert_called()

    async def test_cmd_spawn_dealerships(self):
        from amc import commands
        self.ctx.player_info['bIsAdmin'] = True
        mock_vd = MagicMock()
        mock_vd.spawn = AsyncMock() # ERR FIX
        
        with patch('amc.models.VehicleDealership.objects.filter') as mock_filter:
            mock_filter.return_value.__aiter__.return_value = [mock_vd]
            
            await commands.cmd_spawn_dealerships(self.ctx)
            mock_vd.spawn.assert_called()

    async def test_cmd_spawn_assets(self):
        from amc import commands
        self.ctx.player_info['bIsAdmin'] = True
        with patch('amc.models.WorldText.objects.all') as mock_wt, \
             patch('amc.models.WorldObject.objects.all') as mock_wo, \
             patch('amc.commands.admin.spawn_assets', new=AsyncMock()) as mock_spawn:
            
            mock_wt.return_value.__aiter__.return_value = [MagicMock()]
            mock_wo.return_value.__aiter__.return_value = [MagicMock()]
            
            await commands.cmd_spawn_assets(self.ctx)
            # Should be called twice (looping through mock iterables)
            mock_spawn.assert_called()

    async def test_cmd_spawn_garages(self):
        from amc import commands
        self.ctx.player_info['bIsAdmin'] = True
        mock_g = MagicMock()
        mock_g.config = {'Location': {}, 'Rotation': {}}
        mock_g.asave = AsyncMock() # ERR FIX
        
        with patch('amc.models.Garage.objects.filter') as mock_filter, \
             patch('amc.commands.admin.spawn_garage', new=AsyncMock(return_value={'tag':'t'})):
            
            mock_filter.return_value.__aiter__.return_value = [mock_g]
            
            await commands.cmd_spawn_garages(self.ctx)
            mock_g.asave.assert_called()

    async def test_cmd_spawn_garage_single(self):
        from amc import commands
        self.ctx.player_info['bIsAdmin'] = True
        self.ctx.player_info['Location'] = {'X':0,'Y':0,'Z':0}
        
        with patch('amc.commands.admin.spawn_garage', new=AsyncMock(return_value={'tag':'t'})), \
             patch('amc.models.Garage.objects.acreate', new=AsyncMock()) as mock_create:
            
            await commands.cmd_spawn_garage_single(self.ctx, "MyGarage")
            mock_create.assert_called()
            self.ctx.announce.assert_called()

    async def test_cmd_spawn(self):
        from amc import commands
        self.ctx.player_info['bIsAdmin'] = True
        self.ctx.player_info['Location'] = {'X':0,'Y':0,'Z':0}
        
        # Test numeric ID (existing vehicle)
        with patch('amc.models.CharacterVehicle.objects.aget', new=AsyncMock(return_value=MagicMock())) as mock_get_v, \
             patch('amc.commands.admin.spawn_registered_vehicle', new=AsyncMock()) as mock_srv:
            
            await commands.cmd_spawn(self.ctx, "123")
            mock_srv.assert_called()

        # Test string (raw spawn)
        with patch('amc.commands.admin.spawn_vehicle', new=AsyncMock()) as mock_sv:
            await commands.cmd_spawn(self.ctx, "Truck")
            mock_sv.assert_called()

    # --- Vehicle Management Tests ---

    async def test_cmd_register_vehicles(self):
        from amc import commands
        mock_v = MagicMock()
        mock_v.id = 1
        mock_v.config = {'VehicleName': 'Car'}
        
        with patch('amc.commands.vehicles.register_player_vehicles', new=AsyncMock(return_value=[mock_v])):
            await commands.cmd_register_vehicles(self.ctx)
            self.ctx.reply.assert_called()
            self.assertIn("Car", self.ctx.reply.call_args[0][0])

    async def test_cmd_unrental(self):
        from amc import commands
        mock_v = MagicMock()
        mock_v.rental = True
        mock_v.id = 1
        mock_v.asave = AsyncMock() # ERR FIX
        
        # Test 'all'
        with patch('amc.models.CharacterVehicle.objects.filter') as mock_filter, \
             patch('amc.commands.vehicles.despawn_by_tag', new=AsyncMock()):
            mock_filter.return_value.__aiter__.return_value = [mock_v]
            
            await commands.cmd_unrental(self.ctx, "all")
            mock_v.asave.assert_called()
            self.ctx.reply.assert_called_with("Rentals removed")

    async def test_cmd_rental(self):
        from amc import commands
        mock_v = MagicMock()
        mock_v.rental = False
        mock_v.config = {'CompanyName': 'Corp', 'VehicleName': 'Truck'}
        mock_v.company_guid = "corp-123"
        mock_v.asave = AsyncMock() # ERR FIX
        self.ctx.player_info['OwnCompanyGuid'] = "corp-123"
        
        with patch('amc.commands.vehicles.register_player_vehicles', new=AsyncMock(return_value=[mock_v])):
            await commands.cmd_rental(self.ctx, "Alias")
            mock_v.asave.assert_called()
            self.assertEqual(mock_v.alias, "Alias")
            self.ctx.reply.assert_called()

    async def test_cmd_rent(self):
        from amc import commands
        mock_v = MagicMock()
        mock_v.id = 1
        mock_v.config = {'CompanyName': 'Corp', 'VehicleName': 'Truck'}
        self.ctx.player_info['Location'] = {'X':0,'Y':0,'Z':0}

        # Test listing
        with patch('amc.models.CharacterVehicle.objects.filter') as mock_filter:
            mock_filter.return_value.__aiter__.return_value = [mock_v]
            await commands.cmd_rent(self.ctx, "")
            self.ctx.reply.assert_called()
            self.assertIn("Available Rentals", self.ctx.reply.call_args[0][0])
            self.assertIn("Truck", self.ctx.reply.call_args[0][0])
            
        # Test spawn
        with patch('amc.models.CharacterVehicle.objects.aget', new=AsyncMock(return_value=mock_v)), \
             patch('amc.commands.vehicles.spawn_registered_vehicle', new=AsyncMock()) as mock_spawn:
            
            await commands.cmd_rent(self.ctx, "1")
            mock_spawn.assert_called()

    async def test_cmd_sell(self):
        from amc import commands
        self.ctx.player_info['bIsAdmin'] = True
        mock_v = MagicMock()
        mock_v.id = 1
        mock_v.config = {'Location': {}, 'Rotation': {}}
        mock_v.asave = AsyncMock() # ERR FIX
        
        with patch('amc.commands.vehicles.register_player_vehicles', new=AsyncMock(return_value=[mock_v])), \
             patch('amc.commands.vehicles.despawn_player_vehicle', new=AsyncMock()), \
             patch('amc.commands.vehicles.despawn_by_tag', new=AsyncMock()), \
             patch('amc.commands.vehicles.spawn_registered_vehicle', new=AsyncMock()) as mock_spawn:
            
            await commands.cmd_sell(self.ctx)
            mock_v.asave.assert_called()
            mock_spawn.assert_called()

    # --- Teleport Tests ---

    async def test_cmd_exit(self):
        from amc import commands
        self.ctx.player_info['bIsAdmin'] = True
        players_list = [{'PlayerName': 'Target', 'CharacterGuid': 'guid1'}]
        
        with patch('amc.commands.admin.get_players_mod', new=AsyncMock(return_value=players_list)), \
             patch('amc.commands.admin.force_exit_vehicle', new=AsyncMock()) as mock_force:
            
            await commands.cmd_exit(self.ctx, "Target")
            mock_force.assert_called_with(self.ctx.http_client_mod, 'guid1')

    # --- Finance Tests ---


    async def test_cmd_withdraw(self):
        from amc import commands
        with patch('amc.commands.finance.with_verification_code', return_value=('CODE', False)), \
             patch('amc.commands.finance.register_player_withdrawal', new=AsyncMock()) as mock_reg, \
             patch('amc.commands.finance.transfer_money', new=AsyncMock()) as mock_transfer:
             
             await commands.cmd_withdraw(self.ctx, "100")
             mock_transfer.assert_called()

    async def test_cmd_loan(self):
        from amc import commands
        with patch('amc.commands.finance.get_player_loan_balance', new=AsyncMock(return_value=0)), \
             patch('amc.commands.finance.get_character_max_loan', new=AsyncMock(return_value=(1000, 'Ok'))), \
             patch('amc.commands.finance.register_player_take_loan', new=AsyncMock(return_value=(1100, 100))) as mock_take, \
             patch('amc.commands.finance.transfer_money', new=AsyncMock()) as mock_transfer, \
             patch('amc.models.Delivery.objects.filter') as mock_del_filter:
             
            # Case 1: No deliveries
            mock_del_filter.return_value.aexists = AsyncMock(return_value=False)
            await commands.cmd_loan(self.ctx, "500", "CODE")
            self.ctx.announce.assert_called_with("You must have done at least one delivery")

            mock_del_filter.return_value.aexists = AsyncMock(return_value=True)
            from amc.utils import generate_verification_code
            code = generate_verification_code((500, self.ctx.character.id))
            await commands.cmd_loan(self.ctx, "500", code)
            mock_transfer.assert_called()

    async def test_cmd_thank(self):
        from amc import commands
        mock_p = {'name': 'Other', 'character_guid': 'guid2'}
        mock_char = MagicMock()
        
        with patch('amc.commands.social.get_players', new=AsyncMock(return_value=[(1, mock_p)])), \
             patch('amc.models.Character.objects.aget', new=AsyncMock(return_value=mock_char)), \
             patch('amc.models.Thank.objects.filter') as mock_t_filter, \
             patch('amc.models.Thank.objects.acreate', new=AsyncMock()) as mock_create:
            
            # Cooldown check
            mock_t_filter.return_value.aexists = AsyncMock(return_value=False)
            
            # Mock update and system message
            with patch('amc.models.Player.objects.filter') as mock_p_filter, \
                 patch('amc.commands.social.send_system_message', new=AsyncMock()) as mock_sys_msg:
                
                mock_p_filter.return_value.aupdate = AsyncMock() 

                await commands.cmd_thank(self.ctx, "Other")
                mock_create.assert_called()
                mock_p_filter.return_value.aupdate.assert_called() # Check for social score update
                self.assertEqual(mock_sys_msg.call_count, 2) # Check for 2 system messages

    async def test_cmd_set_rates(self):
        from amc import commands
        with patch('amc.commands.finance.show_popup', new=AsyncMock()) as mock_popup:
            await commands.cmd_set_saving_rate(self.ctx, "10%")
            self.assertEqual(float(round(self.ctx.character.saving_rate, 1)), 0.1)
            mock_popup.assert_called()
            
            mock_popup.reset_mock()
            await commands.cmd_set_repayment_rate(self.ctx, "20%")
            self.assertEqual(float(round(self.ctx.character.loan_repayment_rate, 1)), 0.2)
            mock_popup.assert_called()

    async def test_cmd_toggle_ubi(self):
        from amc import commands
        initial = self.ctx.character.reject_ubi
        with patch('amc.commands.finance.show_popup', new=AsyncMock()) as mock_popup:
            await commands.cmd_toggle_ubi(self.ctx)
            self.assertNotEqual(initial, self.ctx.character.reject_ubi)
            mock_popup.assert_called()

    # --- Misc Tests ---

    async def test_cmd_verify(self):
        from amc import commands
        self.ctx.discord_client = MagicMock()
        self.ctx.discord_client.loop = MagicMock()
        
        with patch('amc.commands.general.verify_player', new=AsyncMock(return_value="discord_id")), \
             patch('amc.commands.general.add_discord_verified_role', new=AsyncMock()) as mock_role, \
             patch('amc.commands.general.show_popup', new=AsyncMock()) as mock_popup, \
             patch('asyncio.run_coroutine_threadsafe') as mock_run_coro:
             
             # Need to setup run_coroutine_threadsafe to await the coro or just check it's called?
             # Since it's fire-and-forget in cmd, we just verify call.
             
            await commands.cmd_verify(self.ctx, "sig")
            
            mock_run_coro.assert_called()
            mock_popup.assert_called()
            self.assertIn("You are now verified", mock_popup.call_args[0][1])
            
            # Since mock_run_coro is called with the coroutine, we can't easily assert mock_role was awaited unless we execute it.
            # But the existence of the call in code is verified by run_coroutine_threadsafe.

    async def test_cmd_rename(self):
        from amc import commands
        with patch('amc.commands.general.set_character_name', new=AsyncMock()) as mock_set:
            await commands.cmd_rename(self.ctx, "NewName")
            self.assertEqual(self.ctx.character.custom_name, "NewName")
            mock_set.assert_called()

    async def test_cmd_bot(self):
        from amc import commands
        with patch('amc.models.BotInvocationLog.objects.acreate', new=AsyncMock()) as mock_log:
            await commands.cmd_bot(self.ctx, "prompt")
            mock_log.assert_called()

    async def test_cmd_song_request(self):
        from amc import commands
        with patch('amc.models.SongRequestLog.objects.acreate', new=AsyncMock()) as mock_log:
            # Case 1: No event
            self.ctx.is_current_event = False
            await commands.cmd_song_request(self.ctx, "song")
            mock_log.assert_called()
            self.ctx.reply.assert_called_with("Song request received")
            
            # Case 2: Event
            self.ctx.is_current_event = True
            with patch('amc.commands.general.show_popup', new=AsyncMock()) as mock_popup:
                 await commands.cmd_song_request(self.ctx, "song")
                 mock_popup.assert_called()

    # --- Moved Legacy Commands Tests ---

    async def test_cmd_undisplay(self):
        from amc import commands
        
        # Mock admin status
        self.ctx.player_info['bIsAdmin'] = True
        
        # Mock vehicles queryset
        mock_v = MagicMock()
        mock_v.spawn_on_restart = True
        mock_v.id = 1
        mock_v.asave = AsyncMock()
        
        with patch('amc.models.CharacterVehicle.objects.filter') as mock_filter, \
             patch('amc.commands.vehicles.register_player_vehicles', new=AsyncMock(return_value=[mock_v])) as mock_reg, \
             patch('amc.commands.vehicles.despawn_by_tag', new=AsyncMock()) as mock_despawn:
            
            # 1. undisplay all
            mock_filter.return_value.__aiter__.return_value = [mock_v]
            await commands.cmd_undisplay(self.ctx, "all")
            mock_despawn.assert_called_with(self.ctx.http_client_mod, 'display-1')
            mock_v.asave.assert_called()
            self.assertFalse(mock_v.spawn_on_restart)
            
            # 2. undisplay specific (via register_player_vehicles logic in command)
            mock_v.spawn_on_restart = True # Reset
            await commands.cmd_undisplay(self.ctx, "")
            mock_reg.assert_called()
            mock_despawn.assert_called_with(self.ctx.http_client_mod, 'display-1')

    async def test_cmd_display(self):
        from amc import commands
        self.ctx.player_info['bIsAdmin'] = True
        
        mock_v = MagicMock()
        mock_v.id = 1
        mock_v.asave = AsyncMock()
        mock_v.config = {'Location': {}, 'Rotation': {}, 'VehicleName': 'TestCar'}
        
        with patch('amc.commands.vehicles.register_player_vehicles', new=AsyncMock(return_value=[mock_v])), \
             patch('amc.commands.vehicles.despawn_player_vehicle', new=AsyncMock()) as mock_despawn, \
             patch('amc.commands.vehicles.despawn_by_tag', new=AsyncMock()), \
             patch('amc.commands.vehicles.spawn_registered_vehicle', new=AsyncMock()) as mock_spawn:
             
             await commands.cmd_display(self.ctx, "all")
             
             mock_despawn.assert_called() 
             mock_v.asave.assert_called()
             mock_spawn.assert_called()
             
    async def test_cmd_burn(self):
        from amc import commands
        from amc.utils import generate_verification_code
        
        amount = 100
        code = generate_verification_code((amount, self.ctx.character.id))
        
        with patch('amc.commands.finance.transfer_money', new=AsyncMock()) as mock_transfer, \
             patch('amc.commands.finance.show_popup', new=AsyncMock()) as mock_popup:
             # 1. No code
             await commands.cmd_burn(self.ctx, "100", "")
             # It uses show_popup, so no reply
             mock_popup.assert_called()
             mock_popup.reset_mock()
             
             # 2. Correct code
             await commands.cmd_burn(self.ctx, "100", code)
             mock_transfer.assert_called_with(self.ctx.http_client_mod, -100, 'Burn', str(self.ctx.player.unique_id))

    async def test_cmd_repay_loan_deprecated(self):
        from amc import commands
        with patch('amc.commands.finance.show_popup', new=AsyncMock()) as mock_popup:
            await commands.cmd_repay_loan(self.ctx)
            mock_popup.assert_called()
            self.assertIn("Command Removed", mock_popup.call_args[0][1])

    async def test_cmd_language_list(self):
        from amc import commands
        await commands.cmd_language(self.ctx)
        self.ctx.reply.assert_called()
        args, _ = self.ctx.reply.call_args
        self.assertIn("Available languages", args[0])

    async def test_cmd_language_set(self):
        from amc import commands
        await commands.cmd_language(self.ctx, "id")
        await self.player.arefresh_from_db()
        self.assertEqual(self.player.language, "id")
        self.ctx.reply.assert_called()
        args, _ = self.ctx.reply.call_args
        self.assertIn("id", args[0])

    async def test_registry_translation_override(self):
        from amc.command_framework import registry
        from django.utils import translation
        
        self.player.language = 'id'
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
            registry.commands = [c for c in registry.commands if c['name'] != "/test_lang"]

    async def test_indonesian_translation_output(self):
        from amc.command_framework import registry
        
        # Set player language to Indonesian
        self.player.language = 'id'
        await self.player.asave()
        
        # We need a command that returns a translated string.
        # /thank when player not found returns "Player not found"
        # In id: "Pemain tidak ditemukan"
        
        with patch('amc.commands.social.get_players', new=AsyncMock(return_value=[])):
            await registry.execute("/thank NoSuchPlayer", self.ctx)
            
            self.ctx.reply.assert_called()
            args, _ = self.ctx.reply.call_args
            # Verify the output is in Indonesian
            self.assertEqual(args[0], "Pemain tidak ditemukan")

    async def test_help_command_translation_output(self):
        from amc.command_framework import registry
        
        self.player.language = 'id'
        await self.player.asave()
        
        await registry.execute("/help", self.ctx)
        
        self.ctx.reply.assert_called()
        args, _ = self.ctx.reply.call_args
        msg = args[0]
        
        # Check for Indonesian translation of "Available Commands"
        self.assertIn("Perintah Tersedia", msg)
        
        # Check for Indonesian translation of a command description, e.g. /register_vehicles
        # "Register your vehicles" -> "Daftarkan kendaraan Anda"
        self.assertIn("Daftarkan kendaraan Anda", msg)

    async def test_cmd_help_shows_all_for_admin(self):
        from amc import commands
        self.ctx.player_info['bIsAdmin'] = True
        
        mock_commands = [
            {'name': '/general', 'aliases': ['/general'], 'description': 'Gen', 'category': 'General'},
            {'name': '/admin', 'aliases': ['/admin'], 'description': 'Adm', 'category': 'Admin'}
        ]
        
        with patch('amc.command_framework.registry.commands', mock_commands):
            await commands.cmd_help(self.ctx)
            
            self.ctx.reply.assert_called()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("/general", output)
            self.assertIn("/admin", output)

    async def test_cmd_help_hides_admin_for_non_admin(self):
        from amc import commands
        self.ctx.player_info['bIsAdmin'] = False
        
        mock_commands = [
            {'name': '/general', 'aliases': ['/general'], 'description': 'Gen', 'category': 'General'},
            {'name': '/admin', 'aliases': ['/admin'], 'description': 'Adm', 'category': 'Admin'}
        ]
        
        with patch('amc.command_framework.registry.commands', mock_commands):
            await commands.cmd_help(self.ctx)
            
            self.ctx.reply.assert_called()
            output = self.ctx.reply.call_args[0][0]
            self.assertIn("/general", output)
            self.assertNotIn("/admin", output)






