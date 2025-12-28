import re
import inspect
import logging
import importlib
import pkgutil
from django.utils import translation
from typing import Callable, Any, get_type_hints, Dict, List, Union, Optional, TYPE_CHECKING
from django.utils.translation import gettext as _
if TYPE_CHECKING:
    from django.utils.functional import _StrPromise
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class CommandContext:
    """
    Holds the state for a single command execution.
    """
    timestamp: Any
    character: Any
    player: Any
    http_client: Any
    http_client_mod: Any
    discord_client: Any = None
    player_info: Optional[Dict] = None
    is_current_event: bool = False

    async def reply(self, message: str):
        """Generic reply via popup"""
        from amc.mod_server import show_popup
        if self.http_client_mod:
             await show_popup(
                 self.http_client_mod, 
                 message, 
                 character_guid=self.character.guid, 
                 player_id=str(self.player.unique_id)
            )
            
    async def announce(self, message: str, **kwargs):
        from amc.game_server import announce
        if self.http_client:
            await announce(message, self.http_client, **kwargs)

class CommandRegistry:
    def __init__(self):
        self.commands: List[Dict] = []

    def register(self, command: Union[str, List[str]], description: Union[str, "_StrPromise"] = "", category: str = "General"):
        """
        Decorator to register a command.
        
        Args:
            command: The command string (e.g. "/help") or list of aliases (e.g. ["/teleport", "/tp"])
            description: A short description of what the command does.
            category: The category the command belongs to (e.g. "General", "Events").
        """
        def decorator(func: Callable):
            aliases = [command] if isinstance(command, str) else command
            pattern = self._build_regex_from_signature(aliases, func)
            
            self.commands.append({
                'name': aliases[0],
                'aliases': aliases,
                'func': func,
                'pattern': re.compile(pattern, re.IGNORECASE),
                'hints': get_type_hints(func),
                'description': description,
                'category': category
            })
            return func
        return decorator

    def autodiscover(self, package_path: str):
        """
        Dynamically imports all modules in the given package path.
        Example: registry.autodiscover('amc.commands')
        """
        package = importlib.import_module(package_path)
        for __, name, is_pkg in pkgutil.iter_modules(package.__path__):
            full_name = f"{package_path}.{name}"
            importlib.import_module(full_name)
            if is_pkg:
                self.autodiscover(full_name)

    def _build_regex_from_signature(self, aliases: List[str], func: Callable) -> str:
        # 1. Build the command part: (?:/cmd1|/cmd2)
        escaped_aliases = [re.escape(a) for a in aliases]
        cmd_part = f"(?:{'|'.join(escaped_aliases)})"
        
        # 2. Build the args part
        sig = inspect.signature(func)
        params = list(sig.parameters.values())
        regex_parts = [cmd_part]
        
        # Filter out 'ctx' or 'self'
        args = [p for p in params if p.name not in ('ctx', 'self')]
        
        for i, param in enumerate(args):
            is_last = (i == len(args) - 1)
            annotation = param.annotation
            default = param.default
            is_optional = default != inspect.Parameter.empty
            
            # Determine regex for type
            if annotation is int:
                # Matches digits, optional negative sign
                type_regex = r"[-]?\d+" 
            elif annotation is float:
                # Matches float or int
                type_regex = r"[-]?\d+(?:\.\d+)?"
            else:
                # String handling
                if is_last:
                    # Last string arg takes the rest of the line
                    type_regex = r".+" 
                else:
                    # Intermediate string arg takes until next whitespace
                    type_regex = r"\S+"

            # Build the group: (?P<name>regex)
            group = f"(?P<{param.name}>{type_regex})"
            
            if is_optional:
                # Optional arg: non-capturing group with space and the arg, whole thing optional
                # e.g. (?:\s+(?P<arg>\d+))?
                regex_parts.append(r"(?:\s+" + group + r")?")
            else:
                # Required arg: must have preceding space
                regex_parts.append(r"\s+" + group)

        regex_parts.append(r"$") # End of string anchor
        return "".join(regex_parts)

    async def execute(self, message: str, ctx: CommandContext) -> bool:
        """
        Iterates through registered commands, checks matches, casts types, and executes.
        Returns True if a command was matched and executed.
        """
        for cmd_data in self.commands:
            match = cmd_data['pattern'].match(message)
            if match:
                kwargs = match.groupdict()
                func = cmd_data['func']
                hints = cmd_data['hints']
                
                # Type casting
                processed_kwargs = {}
                for k, v in kwargs.items():
                    if v is None: 
                        continue 
                    
                    target_type = hints.get(k, str)
                    try:
                        if target_type is int:
                            # Handle "1,000" -> 1000
                            processed_kwargs[k] = int(v.replace(',', ''))
                        elif target_type is float:
                            processed_kwargs[k] = float(v)
                        else:
                            processed_kwargs[k] = v
                    except ValueError:
                        # If casting fails, we assume this isn't the right command match 
                        # (though regex should mostly prevent this) or bad input
                        continue 

                # Execute
                try:
                    lang = 'en-gb'
                    if ctx.player and hasattr(ctx.player, 'language') and isinstance(ctx.player.language, str):
                        lang = ctx.player.language

                    with translation.override(lang):
                        await func(ctx, **processed_kwargs)
                    return True
                except Exception as e:
                    logger.exception(f"Error executing command {cmd_data['name']}")
                    lang = 'en-gb'
                    if ctx.player and hasattr(ctx.player, 'language') and isinstance(ctx.player.language, str):
                        lang = ctx.player.language

                    with translation.override(lang):
                        await ctx.reply(_("<Title>Error</>\n{error}").format(error=str(e)))
                    return True
        return False

# Global instance
registry = CommandRegistry()
