import re
import logging
from amc.mod_server import set_character_name

logger = logging.getLogger(__name__)

# Regexes for stripping tags — covers both new compact and legacy formats
TAG_PATTERNS = [
    # New compact format: must start with C/M/G/P, optionally followed by more letters/digits
    # e.g. [C], [M], [G3], [CM], [MG3], [CMG12], [P], [MP], [MPG3] — but NOT [123] or [ABC]
    re.compile(r"\[(?=[CMGP])[CMGP\d]+\]\s*"),
    # Legacy compact format with Unicode subscript digits (e.g. [G₃], [MG₂₃])
    re.compile(r"\[(?=[CMGP])[CMGP₀₁₂₃₄₅₆₇₈₉]+\]\s*"),
    # Legacy formats (for players who logged in before the refactor)
    re.compile(r"\[CRIM\]\s*", re.IGNORECASE),
    re.compile(r"\[MODS\]\s*", re.IGNORECASE),
    re.compile(r"\[MOD\]\s*", re.IGNORECASE),
    re.compile(r"\[GOV\d*\]\s*", re.IGNORECASE),
    re.compile(r"\[DOT\]\s*", re.IGNORECASE),
]


def strip_all_tags(name: str) -> str:
    """Remove all known tag prefixes from a name."""
    clean_name = name
    for pattern in TAG_PATTERNS:
        clean_name = pattern.sub("", clean_name)
    return clean_name.strip()


def build_display_name(
    base_name: str,
    *,
    criminal_level: int = 0,
    has_custom_parts: bool = False,
    police_level: int = 0,
    gov_level: int = 0,
) -> str:
    """Build the definitive display name with a single compact tag.

    Tag format: [MP1C1G3] BaseName  (order: M, P, C, G)
      M = Modded vehicle parts
      P1 = Police level (active session)
      C1 = Criminal level (suppressed when police is active)
      G3 = Government employee level

    Args:
        base_name: The player's original name (stripped of any existing tags)
        criminal_level: Criminal level (0 = no active criminal record)
        has_custom_parts: Whether the player's current vehicle has custom/modded parts
        police_level: Police level (0 = not on duty)
        gov_level: Government employee level (0 = not a gov employee)
    """
    clean_name = strip_all_tags(base_name)
    tag = ""

    if has_custom_parts:
        tag += "M"

    if police_level > 0:
        tag += f"P{police_level}"

    # C is suppressed when police or gov is active
    if criminal_level > 0 and police_level == 0 and gov_level == 0:
        tag += f"C{criminal_level}"

    if gov_level > 0:
        tag += f"G{gov_level}"

    if tag:
        return f"[{tag}] {clean_name}"
    return clean_name


async def refresh_player_name(
    character, session, *, has_custom_parts: bool | None = None
):
    """Recompute and apply the correct display name for a character.

    This is the ONLY function that should call set_character_name.
    Reads character state (gov_employee, criminal_records, etc.) and computes the definitive name.

    Args:
        character: Character model instance (with player relation loaded)
        session: HTTP client for mod server
        has_custom_parts: If provided, use this value. If None, preserve the
            character's current MOD tag state (from custom_name).
    """
    if not character:
        return

    # Determine MOD state
    if has_custom_parts is None:
        # Preserve existing state — check for both legacy [MODS]/[MOD] and new [M]
        current_name = character.custom_name or character.name
        has_custom_parts = bool(
            re.search(r"\[MODS?\]", current_name, re.IGNORECASE)
            or re.search(r"\[[CG0-9₀-₉]*M[G0-9₀-₉]*\]", current_name)
        )

    # Determine GOV state
    gov_level = 0
    if character.is_gov_employee:
        from amc.gov_employee import calculate_gov_level

        gov_level = calculate_gov_level(character.gov_employee_contributions)

    # Determine CRIM state
    from django.utils import timezone
    from amc.models import CriminalRecord

    has_criminal_record = await CriminalRecord.objects.filter(
        character=character, expires_at__gt=timezone.now()
    ).aexists()

    # Compute criminal level from cumulative laundered total
    criminal_level = 0
    if has_criminal_record:
        from amc.special_cargo import calculate_criminal_level

        criminal_level = calculate_criminal_level(character.criminal_laundered_total)

    # Determine POLICE state
    from amc.police import is_police as check_police, calculate_police_level

    police_level = 0
    if await check_police(character):
        police_level = calculate_police_level(character.police_confiscated_total)

    # Reconstruct name
    new_name = build_display_name(
        character.name,
        criminal_level=criminal_level,
        has_custom_parts=has_custom_parts,
        police_level=police_level,
        gov_level=gov_level,
    )

    # Save to DB if changed
    if new_name != character.custom_name:
        # If it matches the original character name exactly, we set custom_name to None
        if new_name == character.name:
            character.custom_name = None
        else:
            character.custom_name = new_name

        await character.asave(update_fields=["custom_name"])

    # Push to game server if GUID exists
    if session and character.guid:
        try:
            await set_character_name(session, character.guid, new_name)
        except Exception as e:
            logger.exception(f"Failed to set character name for {character.name}: {e}")
