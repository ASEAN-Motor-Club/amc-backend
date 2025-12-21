from amc.command_framework import registry, CommandContext
from django.utils.translation import gettext as _, gettext_lazy

@registry.register("/language", description=gettext_lazy("Set your language preference"), category="General")
async def cmd_language(ctx: CommandContext, lang: str = ""):
    languages = {
        'en': 'en-gb',
        'en-gb': 'en-gb',
        'id': 'id',
        'indonesia': 'id',
        'indonesian': 'id',
        'zh': 'zh-hans',
        'zh-hans': 'zh-hans',
        'cn': 'zh-hans',
        'chinese': 'zh-hans',
    }
    
    if not lang:
        await ctx.reply(_("<Title>Language Settings</>\nAvailable languages:\n- <Highlight>en</> (English)\n- <Highlight>id</> (Indonesian)\n- <Highlight>zh</> (Simplified Chinese)\n\nUsage: /language [en|id|zh]"))
        return
    
    lang_code = languages.get(lang.lower())
    if not lang_code:
        await ctx.reply(_("Invalid language code. Use 'en', 'id', or 'zh'."))
        return
    
    ctx.player.language = lang_code
    await ctx.player.asave(update_fields=['language'])
    
    from django.utils import translation
    with translation.override(lang_code):
        await ctx.reply(_("Language set to {lang}").format(lang=lang_code))
