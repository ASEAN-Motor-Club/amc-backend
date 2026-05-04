import logging

from django.conf import settings

from amc.handlers import register
from amc.mod_server import clear_suspect, make_suspect, show_popup
from amc.models import CriminalRecord, PoliceSession, Wanted
from amc.player_tags import refresh_player_name
from amc.police import deactivate_police

logger = logging.getLogger("amc.webhook.handlers.customization")

COSTUME_SLOT = 4
CRIMINAL_SUSPECT_DURATION = 70  # seconds — mod clamps to 60s; refresh_suspect_tags reapplies every 30s for overlap


@register("ServerSetEquipmentInventory")
async def handle_set_equipment_inventory(event, player, character, ctx):
    data = event.get("data", {}) or {}
    equipped = data.get("Equipped", []) or []
    unequipped = data.get("Unequipped", []) or []

    costume_equipped = next(
        (e for e in equipped if e.get("Slot") == COSTUME_SLOT), None
    )
    costume_unequipped = next(
        (e for e in unequipped if e.get("Slot") == COSTUME_SLOT), None
    )

    if not costume_equipped and not costume_unequipped:
        return 0, 0, 0, 0  # nothing to do for hat/glasses/beard

    was_wearing_costume = character.wearing_costume
    old_costume_key = character.costume_item_key

    if costume_equipped:
        new_key = costume_equipped.get("ItemKey") or None
        character.wearing_costume = new_key in settings.SUSPECT_COSTUMES
        character.costume_item_key = new_key
    else:
        character.wearing_costume = False
        character.costume_item_key = None

    await character.asave(update_fields=["wearing_costume", "costume_item_key"])

    # Immediate suspect poke so the wearer lights up on cops' HUD without
    # waiting for the 10 s refresh tick. Gated on an active CriminalRecord.
    if character.wearing_costume and ctx.http_client_mod and character.guid:
        has_record = await CriminalRecord.objects.filter(
            character=character, cleared_at__isnull=True
        ).aexists()
        if not has_record:
            await CriminalRecord.objects.acreate(
                character=character,
                reason="Wearing criminal costume",
            )
            try:
                await refresh_player_name(character, ctx.http_client_mod)
            except Exception:
                logger.warning(
                    "refresh_player_name (costume-equip) failed for %s",
                    character.name,
                    exc_info=True,
                )
        try:
            await make_suspect(
                ctx.http_client_mod,
                character.guid,
                duration_seconds=CRIMINAL_SUSPECT_DURATION,
            )
        except Exception:
            logger.warning(
                "make_suspect (costume-equip) failed for %s",
                character.name,
                exc_info=True,
            )

    # Immediate suspect GE removal when the player stops wearing a suspect
    # costume — clears the blue overlay / Net_Suspects entry without waiting
    # up to 10 s for the refresh_suspect_tags transition-out pass.
    if (
        was_wearing_costume
        and not character.wearing_costume
        and ctx.http_client_mod
        and character.guid
    ):
        is_wanted = await Wanted.objects.filter(
            character=character, expired_at__isnull=True, wanted_remaining__gt=0
        ).aexists()
        if not is_wanted:
            try:
                await clear_suspect(ctx.http_client_mod, character.guid)
            except Exception:
                logger.warning(
                    "clear_suspect (costume-change) failed for %s",
                    character.name,
                    exc_info=True,
                )

    # Auto-deactivate police if officer removes/swaps away from police costume
    was_police_costume = old_costume_key in settings.POLICE_COSTUMES
    is_police_costume = character.costume_item_key in settings.POLICE_COSTUMES
    if was_police_costume and not is_police_costume:
        is_active = await PoliceSession.objects.filter(
            character=character, ended_at__isnull=True
        ).aexists()
        if is_active:
            await deactivate_police(character, ctx.http_client_mod)
            try:
                await show_popup(
                    ctx.http_client_mod,
                    "Your police session has been ended because you removed the police uniform.",
                    character_guid=character.guid,
                )
            except Exception:
                logger.warning(
                    "show_popup (police costume removal) failed for %s",
                    character.name,
                    exc_info=True,
                )

    return 0, 0, 0, 0
