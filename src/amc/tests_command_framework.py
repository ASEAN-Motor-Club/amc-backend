from django.test import SimpleTestCase
from unittest.mock import MagicMock, AsyncMock
from amc.command_framework import CommandRegistry, CommandContext
import asyncio

class TestCommandFramework(SimpleTestCase):
    def setUp(self):
        self.registry = CommandRegistry()
        self.ctx = MagicMock(spec=CommandContext)
        self.ctx.reply = AsyncMock()
        self.ctx.player = MagicMock()
        self.ctx.player.language = 'en-gb'

    async def _execute_command(self, command_str):
        # Helper to execute command since SimpleTestCase doesn't support async methods directly efficiently 
        # normally, but let's try calling the async method and running it.
        # However, SimpleTestCase methods are synchronous.
        # We can use asyncio.run() if the code is purely standalone, but within Django/Amc context usually we want async test support.
        # Since the user asked for SimpleTestCase, we will wrap calls.
        return await self.registry.execute(command_str, self.ctx)

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_basic_command(self):
        @self.registry.register("/hello")
        async def cmd_hello(ctx):
            await ctx.reply("Hello world")
        
        self.run_async(self._execute_command("/hello"))
        
        self.ctx.reply.assert_called_with("Hello world")

    def test_optional_params(self):
        @self.registry.register("/greet")
        async def cmd_greet(ctx, name: str = "Stranger"):
            await ctx.reply(f"Hello {name}")

        # Case 1: No arg provided (use default)
        self.run_async(self._execute_command("/greet"))
        self.ctx.reply.assert_called_with("Hello Stranger")

        # Case 2: Arg provided
        self.ctx.reply.reset_mock()
        self.run_async(self._execute_command("/greet Bob"))
        self.ctx.reply.assert_called_with("Hello Bob")

    def test_param_type_validation_int(self):
        @self.registry.register("/add")
        async def cmd_add(ctx, a: int, b: int):
            await ctx.reply(f"{a + b}")

        # Valid ints
        self.run_async(self._execute_command("/add 5 10"))
        self.ctx.reply.assert_called_with("15")

        # Invalid int (should fail regex match or casting, currently regex handles it)
        # "abc" won't match the \d+ pattern for int, so execute returns False (no match)
        result = self.run_async(self._execute_command("/add 5 abc"))
        self.assertFalse(result)

    def test_param_type_validation_float(self):
        @self.registry.register("/scale")
        async def cmd_scale(ctx, factor: float):
            await ctx.reply(f"{factor * 2}")

        # Valid float
        self.run_async(self._execute_command("/scale 2.5"))
        self.ctx.reply.assert_called_with("5.0")
        
        # Valid int as float
        self.ctx.reply.reset_mock()
        self.run_async(self._execute_command("/scale 3"))
        self.ctx.reply.assert_called_with("6.0")

        # Invalid
        result = self.run_async(self._execute_command("/scale abc"))
        self.assertFalse(result)

    def test_multiple_args(self):
        @self.registry.register("/mix")
        async def cmd_mix(ctx, count: int, name: str, ratio: float):
            await ctx.reply(f"{count} {name} {ratio}")

        self.run_async(self._execute_command("/mix 10 Apple 0.5"))
        self.ctx.reply.assert_called_with("10 Apple 0.5")

    def test_string_args_spacing(self):
        @self.registry.register("/echo")
        async def cmd_echo(ctx, first: str, rest: str):
            await ctx.reply(f"{first}|{rest}")

        # "rest" is the last string arg, so it should capture everything including spaces
        self.run_async(self._execute_command("/echo Hello my friend"))
        self.ctx.reply.assert_called_with("Hello|my friend")
