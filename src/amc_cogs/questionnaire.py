"""Discord questionnaire system.

``/questionnaire`` group (create / results / export / close) restricted to the
Kimaki and Admin roles. Anyone may answer the public embed posted by create.

Questions JSON schema (accepted inline or as a .json attachment):

    {
      "title": str,
      "description": str (optional),
      "response_mode": "single"|"multiple" (optional, default "single"),
      "questions": [
        {"text": str,                      # Label text (max 45 chars)
         "type": "text"|"single"|"multi"|"radio"|"check"|"file",
         "description": str (optional, Label description, max 100 chars),
         # text:    style "short"|"paragraph", placeholder, required,
         #          min_length (0-4000), max_length (1-4000)
         # single:  options [str, ...] (1-25) -> String Select
         # multi:   options [str, ...], min_values (default 0), max_values
         # radio:   options [str, ...] (2-10) -> Radio Group
         # check:   options [str, ...] (2-10) -> Checkbox Group (multi)
         # file:    required, min_values, max_values -> File Upload
         "required": bool (optional, default true)
        }
      ]
    }

Answering is FULLY modal-based: the embed carries a single "Open form"
button; the modal shows 5 questions per page (Discord hard cap) and pages
chain via ephemeral "Next page" buttons when needed.
"""

import asyncio
import csv
import io
import json
from collections import Counter

import discord
from discord import app_commands
from discord.ext import commands
from django.conf import settings

from amc.models import Questionnaire, QuestionnaireResponse

QUESTIONS_JSON_DOC = (
    "Schema: {\"title\": str, \"description\": str (optional), "
    "\"response_mode\": \"single\"|\"multiple\" (optional, default single), "
    "\"questions\": [{\"text\": str (≤45 chars, the field label), "
    "\"type\": \"single\"|\"multi\"|\"text\"|\"radio\"|\"check\"|\"file\", "
    "\"description\": str (optional, ≤100 chars, shown under the label), "
    "\"required\": bool (optional, default true), "
    "\"options\": [str, ...] (single/multi: 1-25; radio/check: 2-10), "
    "// text only: \"style\": \"short\"|\"paragraph\", \"placeholder\": str, "
    "\"min_length\": 0-4000, \"max_length\": 1-4000; "
    "// multi: \"min_values\": 0-25, \"max_values\": 1-25; "
    "// check: \"min_values\"/\"max_values\": 0-10; "
    "file: \"min_values\"/\"max_values\": 1-10"
    "]}]} Use /questionnaire schema for examples."
)


SCHEMA_EXAMPLES = """\
**Questionnaire JSON — question types & customization**

Top level: `title` (str), `description` (str, optional),
`response_mode` (`single` = one per user, `multiple` = many; default single),
`questions` (1-10).

Common per-question fields:
• `text` — the question label (≤45 chars, required)
• `description` — helper text under the label (≤100 chars, optional)
• `required` — whether it must be answered (default true)

**`text`** — free text box
```json
{"text": "Feedback?", "type": "text", "style": "paragraph",
 "placeholder": "Tell us everything", "min_length": 0, "max_length": 4000,
 "required": false}
```
`style`: `short` (200 chars) or `paragraph` (4000).

**`single`** — pick one from a dropdown
```json
{"text": "Favourite colour?", "type": "single",
 "options": ["Red", "Green", "Blue"]}
```

**`multi`** — pick several
```json
{"text": "Toppings?", "type": "multi", "options": ["Ham", "Corn", "Pineapple"],
 "min_values": 1, "max_values": 3}
```

**`radio`** — pick one, big buttons
```json
{"text": "Rate the event", "type": "radio",
 "options": ["Bad", "Ok", "Good", "Great"]}
```

**`check`** — checkbox group, tick any of the options
```json
{"text": "Which days can you attend?", "type": "check",
 "options": ["Fri", "Sat", "Sun"], "min_values": 1, "max_values": 3}
```


**`file`** — upload files
```json
{"text": "Attach your screenshot", "type": "file",
 "min_values": 1, "max_values": 3, "required": true}
```

Tip: ask me (Yumemi) in chat to generate this JSON from a plain-English
description — I validate it against the bot's own parser before handing
it over."""


# ---------------------------------------------------------------- pure helpers


def _validate_common(q: dict, i: int) -> tuple[str, str, bool]:
    text = q.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"Question {i}: \"text\" must be a non-empty string.")
    if len(text) > 45:
        raise ValueError(
            f"Question {i}: \"text\" is {len(text)} chars; max is 45. "
            "Move long question text into the question's \"description\" "
            "(max 100 chars)."
        )
    description = q.get("description", "")
    if not isinstance(description, str):
        raise ValueError(f"Question {i}: \"description\" must be a string.")  # noqa: TRY004
    if len(description.strip()) > 100:
        raise ValueError(
            f"Question {i}: \"description\" is {len(description.strip())} chars; "
            "max is 100."
        )
    required = q.get("required", True)
    if not isinstance(required, bool):
        raise ValueError(f"Question {i}: \"required\" must be a boolean.")  # noqa: TRY004
    return text.strip(), description.strip(), required


def _validate_options(q: dict, i: int, lo: int, hi: int) -> list[str]:
    options = q.get("options")
    if not isinstance(options, list) or not (lo <= len(options) <= hi):
        raise ValueError(
            f"Question {i}: \"options\" must be a list of {lo}-{hi} strings."
        )
    if any(not isinstance(o, str) or not o.strip() for o in options):
        raise ValueError(f"Question {i}: every option must be a non-empty string.")
    stripped = [o.strip() for o in options]
    too_long = [o for o in stripped if len(o) > 100]
    if too_long:
        raise ValueError(
            f"Question {i}: option(s) over 100 chars: {too_long[0]!r} "
            f"({len(too_long[0])} chars). Shorten the option."
        )
    return stripped


def _int_field(q: dict, i: int, key: str, default, lo: int, hi: int):
    value = q.get(key, default)
    if value is None:
        return None
    if not isinstance(value, int) or not (lo <= value <= hi):
        raise ValueError(
            f"Question {i}: \"{key}\" must be an integer between {lo} and {hi}."
        )
    return value


def validate_questions_payload(raw: str) -> dict:
    """Parse and validate the questionnaire JSON. Raises ValueError."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Top level must be a JSON object.")  # noqa: TRY004
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("\"title\" must be a non-empty string.")
    if len(title.strip()) > 200:
        raise ValueError(f"\"title\" is {len(title.strip())} chars; max is 200.")
    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("\"questions\" must be a non-empty list.")
    normalized: list[dict] = []
    for i, q in enumerate(questions, 1):
        if not isinstance(q, dict):
            raise ValueError(f"Question {i} must be an object.")  # noqa: TRY004
        text, description, required = _validate_common(q, i)
        qtype = q.get("type", "single")
        entry: dict = {"text": text, "type": qtype, "required": required}
        if description:
            entry["description"] = description
        if qtype == "text":
            entry["style"] = q.get("style", "short")
            if entry["style"] not in ("short", "paragraph"):
                raise ValueError(
                    f"Question {i}: \"style\" must be \"short\" or \"paragraph\"."
                )
            entry["placeholder"] = str(q.get("placeholder", "")).strip()[:100]
            default_max = 1000 if entry["style"] == "paragraph" else 200
            entry["min_length"] = _int_field(q, i, "min_length", 0, 0, 4000)
            entry["max_length"] = _int_field(q, i, "max_length", default_max, 1, 4000)
        elif qtype in ("single", "multi"):
            entry["options"] = _validate_options(q, i, 1, 25)
            if qtype == "multi":
                entry["min_values"] = _int_field(q, i, "min_values", 0, 0, 25)
                entry["max_values"] = _int_field(q, i, "max_values", 25, 1, 25)
        elif qtype in ("radio", "check"):
            entry["options"] = _validate_options(q, i, 1, 10)
            if qtype == "check":
                if "min_values" in q and q["min_values"] is not None:
                    entry["min_values"] = _int_field(q, i, "min_values", 0, 0, 10)
                if "max_values" in q and q["max_values"] is not None:
                    entry["max_values"] = _int_field(q, i, "max_values", 1, 1, 10)
        elif qtype == "file":
            entry["min_values"] = _int_field(q, i, "min_values", 1, 1, 10)
            entry["max_values"] = _int_field(q, i, "max_values", 1, 1, 10)
        else:
            raise ValueError(
                f"Question {i}: \"type\" must be one of text, single, multi, "
                "radio, check, file."
            )
        normalized.append(entry)
    response_mode = data.get("response_mode", "single")
    if response_mode not in ("single", "multiple"):
        raise ValueError("\"response_mode\" must be \"single\" or \"multiple\".")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ValueError("\"description\" must be a string.")  # noqa: TRY004
    return {
        "title": title.strip()[:200],
        "description": description.strip(),
        "response_mode": response_mode,
        "questions": normalized,
    }


def build_questionnaire_embed(questionnaire: Questionnaire) -> discord.Embed:
    """Public embed shown for a (still-open or closed) questionnaire."""
    closed = questionnaire.closed
    embed = discord.Embed(
        title=f"{'[CLOSED] ' if closed else ''}{questionnaire.title}",
        description=questionnaire.description or None,
        color=discord.Color.red() if closed else discord.Color.blurple(),
    )
    mode = "multiple responses allowed" if (
        questionnaire.response_mode == "multiple"
    ) else "one response per user"
    embed.set_footer(text=f"Questionnaire #{questionnaire.id} • {mode}")
    return embed


def _format_value(value) -> str:
    """Human-readable single answer for results/CSV."""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if value is None or value == "":
        return ""
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return str(value)


def build_results_embed(questionnaire: Questionnaire) -> discord.Embed:
    """Quick-view embed: per-question tallies + response count."""
    responses = list(questionnaire.responses.all().order_by("created_at"))
    embed = discord.Embed(
        title=f"Results — {questionnaire.title}",
        description=(
            f"{len(responses)} response(s)"
            + (" • CLOSED" if questionnaire.closed else "")
        ),
        color=discord.Color.gold(),
    )
    for i, q in enumerate(questionnaire.questions):
        if q["type"] == "text":
            given = [
                str(r.answers[i])
                for r in responses
                if i < len(r.answers) and r.answers[i]
            ]
            lines = [f"> {a[:200]}" for a in given] or ["No answers"]
            embed.add_field(
                name=f"Q{i + 1}. {q['text']} (text)",
                value="\n".join(lines)[:1024],
                inline=False,
            )
            continue
        counts: Counter = Counter()
        for r in responses:
            answers = r.answers
            value = answers[i] if i < len(answers) else None
            if isinstance(value, list):
                counts.update(_format_value(v) for v in value)
            elif value is not None and value != "":
                counts[_format_value(value)] += 1
        if q["type"] in ("radio", "check"):
            lines = [f"`{counts.get(o, 0):>3}` {o}" for o in q["options"]]
        else:
            lines = [f"`{c:>3}` {v}" for v, c in counts.most_common()] or [
                "No answers"
            ]
        embed.add_field(
            name=f"Q{i + 1}. {q['text']} ({q['type']})",
            value="\n".join(lines)[:1024],
            inline=False,
        )
    embed.set_footer(text=f"Questionnaire #{questionnaire.id}")
    return embed


def build_results_csv(questionnaire: Questionnaire) -> io.StringIO:
    """CSV: one row per response — respondent, timestamp, answers."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["questionnaire_id", "respondent_id", "respondent_name", "submitted_at"]
        + [f"Q{i + 1}: {q['text']}" for i, q in enumerate(questionnaire.questions)]
    )
    for r in questionnaire.responses.all().order_by("created_at"):
        row = [questionnaire.id, r.discord_user_id, r.discord_username,
               r.created_at.isoformat()]
        for i, _q in enumerate(questionnaire.questions):
            value = r.answers[i] if i < len(r.answers) else None
            row.append(_format_value(value))
        writer.writerow(row)
    buf.seek(0)
    return buf


# --------------------------------------------------------------- answer UI

MODAL_PAGE_SIZE = 5  # Discord hard cap: 5 Label rows per modal


def _build_form_item(index: int, q: dict) -> discord.ui.Item:
    """Build the interactive component for one question (inside a Label)."""
    cid = f"q{index}"
    qtype = q["type"]
    if qtype == "text":
        return discord.ui.TextInput(
            custom_id=cid,
            style=(
                discord.TextStyle.paragraph
                if q.get("style") == "paragraph"
                else discord.TextStyle.short
            ),
            required=q.get("required", True),
            min_length=q.get("min_length") or 0,
            max_length=q.get("max_length") or 4000,
            placeholder=q.get("placeholder") or None,
        )
    if qtype == "single":
        return discord.ui.Select(
            custom_id=cid,
            options=[discord.SelectOption(label=o, value=o) for o in q["options"]],
            required=q.get("required", True),
            placeholder=q.get("placeholder") or None,
        )
    if qtype == "multi":
        return discord.ui.Select(
            custom_id=cid,
            options=[discord.SelectOption(label=o, value=o) for o in q["options"]],
            min_values=q.get("min_values") or 0,
            max_values=q.get("max_values") or len(q["options"]),
            required=q.get("required", True),
            placeholder=q.get("placeholder") or None,
        )
    if qtype == "radio":
        rg = discord.ui.RadioGroup(custom_id=cid, required=q.get("required", True))
        for j, o in enumerate(q["options"]):
            rg.add_option(label=o, value=o, default=False)
        return rg
    if qtype == "file":
        return discord.ui.FileUpload(
            custom_id=cid,
            required=q.get("required", True),
            min_values=q.get("min_values") or 1,
            max_values=q.get("max_values") or 1,
        )
    raise ValueError(f"unknown question type {qtype!r}")


def _build_form_row(index: int, q: dict) -> list[discord.ui.Label]:
    """Return the Label row(s) for one question."""
    cid = f"q{index}"
    if q["type"] == "check":
        required = q.get("required", True)
        min_vals = q.get("min_values")
        if min_vals is None:
            # Discord requires min_values >= 1 when the group is required.
            min_vals = 1 if required else 0
        n_opts = len(q["options"])
        max_vals = q.get("max_values")
        if max_vals is None or max_vals > n_opts:
            max_vals = n_opts
        if required and (min_vals is None or min_vals < 1):
            min_vals = 1
        if min_vals is None:
            min_vals = 0
        min_vals = min(min_vals, max_vals)
        cg = discord.ui.CheckboxGroup(
            custom_id=cid,
            required=required,
            min_values=min_vals,
            max_values=max_vals,
        )
        for j, o in enumerate(q["options"]):
            cg.add_option(label=o[:100], value=o[:100])
        return [
            discord.ui.Label(
                text=q["text"],
                component=cg,
                description=q.get("description") or None,
            )
        ]
    item = _build_form_item(index, q)
    return [
        discord.ui.Label(
            text=q["text"],
            component=item,
            description=q.get("description") or None,
        )
    ]


def _paginate_rows(
    questions: list[tuple[int, dict]],
) -> list[list[tuple[int, dict, list[discord.ui.Label]]]]:
    """Chunk questions into modal pages of at most MODAL_PAGE_SIZE label rows.

    Each page holds whole questions only; a question's rows never split
    across pages.
    """
    pages: list[list[tuple[int, dict, list[discord.ui.Label]]]] = []
    current: list[tuple[int, dict, list[discord.ui.Label]]] = []
    count = 0
    for index, q in questions:
        rows = _build_form_row(index, q)
        if count + len(rows) > MODAL_PAGE_SIZE and current:
            pages.append(current)
            current = []
            count = 0
        current.append((index, q, rows))
        count += len(rows)
    if current:
        pages.append(current)
    return pages


class QuestionnaireFormModal(discord.ui.Modal):
    """One page of the questionnaire form (max 5 Label rows per modal).

    ``page_questions`` is a list of ``(index, question, rows)`` triples for
    this page (rows pre-built by :func:`_paginate_rows`). Answers accumulate
    on the shared parent view; later pages chain via an ephemeral
    "Next page" button.
    """

    def __init__(
        self,
        parent_view: "QuestionnaireAnswerView",
        page_questions: list,
        page: int = 1,
        total_pages: int = 1,
        next_pages: list | None = None,
    ):
        super().__init__(title=f"[{page}/{total_pages}] {parent_view.form_title}"[:45])
        self.parent_view = parent_view
        self.next_pages = next_pages or []
        self.page_questions = page_questions
        self.page = page
        self.total_pages = total_pages
        for _index, _q, rows in page_questions:
            for label in rows:
                self.add_item(label)

    async def on_submit(self, interaction: discord.Interaction):
        collected = self._collect_answers()
        self.parent_view.store_answers(collected)
        if self.next_pages:
            next_page = self.page + 1
            await interaction.response.send_message(
                f"Page {self.page}/{self.total_pages} saved.",
                view=_NextPageView(
                    self.parent_view, self.next_pages, next_page, self.total_pages
                ),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "All questions answered — press **Submit** to send your response.",
                view=_SubmitFollowupView(self.parent_view),
                ephemeral=True,
            )

    def find_item(self, custom_id: str) -> discord.ui.Item | None:
        """Locate an item by custom_id, unwrapping Label components."""
        for child in self.walk_children():
            if isinstance(child, discord.ui.Label):
                comp = child.component
                if getattr(comp, "custom_id", None) == custom_id:
                    return comp
            elif getattr(child, "custom_id", None) == custom_id:
                return child
        return None

    def _collect_answers(self) -> dict[int, object]:
        answers: dict[int, object] = {}
        for index, q, _rows in self.page_questions:
            item = self.find_item(f"q{index}")
            answers[index] = self._read_value(item, q)
        return answers

    @staticmethod
    def _read_value(item: discord.ui.Item, q: dict) -> object:
        if isinstance(item, discord.ui.Label):
            item = item.component
        if isinstance(item, discord.ui.TextInput):
            return (item.value or "").strip()
        if isinstance(item, discord.ui.CheckboxGroup):
            return list(item.values or [])
        if isinstance(item, discord.ui.Select):
            values = list(item.values or [])
            if q["type"] == "single":
                return values[0] if values else None
            return values
        if isinstance(item, discord.ui.RadioGroup):
            return item.value
        if isinstance(item, discord.ui.FileUpload):
            return [str(a.id) for a in (item.values or [])]
        return None


class _NextPageView(discord.ui.View):
    """Ephemeral button that opens the next modal page (modals can't nest)."""

    def __init__(
        self,
        parent_view: "QuestionnaireAnswerView",
        next_pages: list,
        page: int,
        total_pages: int,
    ):
        super().__init__(timeout=300)
        self.add_item(_NextPageButton(parent_view, next_pages, page, total_pages))


class _NextPageButton(discord.ui.Button):
    def __init__(
        self,
        parent_view: "QuestionnaireAnswerView",
        next_pages: list,
        page: int,
        total_pages: int,
    ):
        super().__init__(
            label=f"Open page {page} of {total_pages}",
            style=discord.ButtonStyle.primary,
        )
        self.parent_view = parent_view
        self.next_pages = next_pages
        self.page = page
        self.total_pages = total_pages

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            QuestionnaireFormModal(
                self.parent_view,
                self.next_pages[0],
                self.page,
                self.total_pages,
                next_pages=self.next_pages[1:],
            )
        )


class QuestionnaireAnswerView(discord.ui.View):
    """The public embed view: one "Open form" button (class-based)."""

    def __init__(self, questionnaire_id: int, questions: list[dict], form_title: str = "Form"):
        super().__init__(timeout=None)  # persistent across restarts
        self.questionnaire_id = questionnaire_id
        self.questions = questions
        self.form_title = form_title
        self.selections: dict[int, object] = {}
        self.add_item(_OpenFormButton(self))

    @property
    def text_answers(self) -> dict[int, object]:
        return self.selections

    def store_answers(self, answers: dict[int, object]) -> None:
        self.selections = {**self.selections, **answers}

    # backwards-compatible alias used by tests
    def store_text_answers(self, answers: dict[int, str]) -> None:
        self.store_answers(answers)

    async def do_submit(self, interaction: discord.Interaction) -> None:
        missing = [
            i + 1
            for i, q in enumerate(self.questions)
            if q.get("required", True) and i not in self.selections
        ]
        if missing:
            await interaction.response.send_message(
                f"Please answer question(s): {', '.join(map(str, missing))}.",
                ephemeral=True,
            )
            return
        await _save_response(
            interaction, self.questionnaire_id, self.selections
        )


class _SubmitButton(discord.ui.Button):
    def __init__(self, parent_view: "QuestionnaireAnswerView"):
        super().__init__(label="Submit", style=discord.ButtonStyle.success)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        await self.parent_view.do_submit(interaction)


class _SubmitFollowupView(discord.ui.View):
    """Ephemeral view shown after the last modal page: the Submit button."""

    def __init__(self, parent_view: "QuestionnaireAnswerView"):
        super().__init__(timeout=600)
        self.add_item(_SubmitButton(parent_view))


class _OpenFormButton(discord.ui.Button):
    def __init__(self, parent_view: "QuestionnaireAnswerView"):
        super().__init__(label="Open form", style=discord.ButtonStyle.primary)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        pages = _paginate_rows(list(enumerate(self.parent_view.questions)))
        await interaction.response.send_modal(
            QuestionnaireFormModal(
                self.parent_view, pages[0], 1, len(pages), next_pages=pages[1:]
            )
        )


async def _save_response(
    interaction: discord.Interaction, questionnaire_id: int, selections: dict
) -> None:
    def _db() -> tuple[str, bool]:
        try:
            q = Questionnaire.objects.get(pk=questionnaire_id)
        except Questionnaire.DoesNotExist:
            return "This questionnaire no longer exists.", False
        if q.closed:
            return "This questionnaire is closed.", False
        answers = [selections.get(i) for i in range(len(q.questions))]
        if q.response_mode == "single":
            QuestionnaireResponse.objects.update_or_create(
                questionnaire=q,
                discord_user_id=str(interaction.user.id),
                defaults={
                    "discord_username": interaction.user.display_name,
                    "answers": answers,
                },
            )
        else:
            QuestionnaireResponse.objects.create(
                questionnaire=q,
                discord_user_id=str(interaction.user.id),
                discord_username=interaction.user.display_name,
                answers=answers,
            )
        return "Your response has been recorded. Thank you!", True

    message, _ok = await asyncio.to_thread(_db)
    await interaction.response.send_message(message, ephemeral=True)


# -------------------------------------------------------------------- the cog


def _require_role():
    """Gate the management commands to Kimaki + Admin roles."""
    allowed = {
        settings.DISCORD_KIMAKI_ROLE_ID,
        settings.DISCORD_ADMIN_ROLE_ID,
    }

    async def predicate(interaction: discord.Interaction) -> bool:
        user = interaction.user
        if isinstance(user, discord.Member) and any(
            role.id in allowed for role in user.roles
        ):
            return True
        raise app_commands.CheckFailure("questionnaire_role")

    return app_commands.check(predicate)


class QuestionnaireCog(commands.Cog):
    questionnaire_group = app_commands.Group(
        name="questionnaire",
        description="Create and manage questionnaires",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @questionnaire_group.command(
        name="create", description="Create a questionnaire from questions JSON"
    )
    @_require_role()
    @app_commands.describe(
        json_string="Questions JSON inline (small surveys), OR attach a .json file",
        questions_file="A .json file with the questions (overrides json_string)",
        channel="Channel to post in (defaults to the current channel)",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        json_string: str | None = None,
        questions_file: discord.Attachment | None = None,
        channel: discord.TextChannel | None = None,
    ):
        if questions_file is not None:
            raw = (await questions_file.read()).decode("utf-8")
        elif json_string:
            raw = json_string
        else:
            await interaction.response.send_message(
                f"Provide `json_string` or attach `questions_file`.\n{QUESTIONS_JSON_DOC}",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            data = await asyncio.to_thread(validate_questions_payload, raw)
        except ValueError as exc:
            await interaction.followup.send(
                f"Invalid questions JSON:\n> {exc}\n{QUESTIONS_JSON_DOC}", ephemeral=True
            )
            return

        def _db() -> Questionnaire:
            return Questionnaire.objects.create(
                title=data["title"],
                description=data["description"],
                questions=data["questions"],
                response_mode=data["response_mode"],
                created_by_discord_id=str(interaction.user.id),
            )

        questionnaire = await asyncio.to_thread(_db)

        embed = build_questionnaire_embed(questionnaire)
        view = QuestionnaireAnswerView(
            questionnaire.id, data["questions"], form_title=data["title"]
        )
        target = channel or interaction.channel
        try:
            message = await target.send(embed=embed, view=view)
        except discord.Forbidden:
            await interaction.followup.send(
                f"I lack **Send Messages / Embed Links** permission in "
                f"{target.mention} — the questionnaire was created but NOT posted. "
                f"Fix the channel permissions or re-run `/questionnaire create` "
                f"with a `channel` I can post in.",
                ephemeral=True,
            )
            return
        await asyncio.to_thread(
            Questionnaire.objects.filter(pk=questionnaire.id).update,
            channel_id=str(target.id),
            message_id=str(message.id),
        )
        await interaction.followup.send(
            f"Questionnaire #{questionnaire.id} posted in {target.mention}. "
            f"Use `/questionnaire results id:{questionnaire.id}` for tallies.",
            ephemeral=True,
        )

    @questionnaire_group.command(
        name="schema",
        description="How to customize questionnaire questions (JSON schema + examples)",
    )
    async def schema(self, interaction: discord.Interaction):
        await interaction.response.send_message(SCHEMA_EXAMPLES, ephemeral=True)

    @questionnaire_group.command(
        name="results", description="Quick view of response tallies"
    )
    @_require_role()
    @app_commands.describe(id="Questionnaire id (from the posted embed footer)")
    async def results(self, interaction: discord.Interaction, id: int):
        await interaction.response.defer(ephemeral=True)

        def _db() -> tuple[Questionnaire | None, str | None]:
            try:
                return Questionnaire.objects.get(pk=id), None
            except Questionnaire.DoesNotExist:
                return None, f"No questionnaire with id {id}."

        questionnaire, err = await asyncio.to_thread(_db)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return
        embed = await asyncio.to_thread(build_results_embed, questionnaire)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @questionnaire_group.command(
        name="export", description="Export responses to CSV"
    )
    @_require_role()
    @app_commands.describe(id="Questionnaire id (from the posted embed footer)")
    async def export(self, interaction: discord.Interaction, id: int):
        await interaction.response.defer(ephemeral=True)

        def _db() -> tuple[Questionnaire | None, str | None]:
            try:
                return Questionnaire.objects.get(pk=id), None
            except Questionnaire.DoesNotExist:
                return None, f"No questionnaire with id {id}."

        questionnaire, err = await asyncio.to_thread(_db)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return
        buf = await asyncio.to_thread(build_results_csv, questionnaire)
        filename = f"questionnaire_{id}_results.csv"
        await interaction.followup.send(
            file=discord.File(io.BytesIO(buf.getvalue().encode("utf-8")), filename=filename),
            ephemeral=True,
        )

    @questionnaire_group.command(
        name="close", description="Close a questionnaire (stops new responses)"
    )
    @_require_role()
    @app_commands.describe(id="Questionnaire id (from the posted embed footer)")
    async def close(self, interaction: discord.Interaction, id: int):
        await interaction.response.defer(ephemeral=True)

        def _db() -> tuple[Questionnaire | None, str | None]:
            try:
                return Questionnaire.objects.get(pk=id), None
            except Questionnaire.DoesNotExist:
                return None, f"No questionnaire with id {id}."

        questionnaire, err = await asyncio.to_thread(_db)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return
        await asyncio.to_thread(
            Questionnaire.objects.filter(pk=id).update, closed=True
        )
        questionnaire.closed = True
        try:
            channel = interaction.client.get_channel(int(questionnaire.channel_id))
            if channel:
                message = await channel.fetch_message(int(questionnaire.message_id))
                await message.edit(
                    embed=build_questionnaire_embed(questionnaire), view=None
                )
        except (ValueError, discord.HTTPException):
            pass
        await interaction.followup.send(
            f"Questionnaire #{id} closed.", ephemeral=True
        )

    @questionnaire_group.command(
        name="repost",
        description="Repost a questionnaire post (same embed + Open form button)",
    )
    @_require_role()
    @app_commands.describe(
        id="Questionnaire id (from the posted embed footer)",
        channel="Channel to repost in (defaults to this channel)",
    )
    async def repost(
        self,
        interaction: discord.Interaction,
        id: int,
        channel: discord.TextChannel | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        def _db() -> tuple[Questionnaire | None, str | None]:
            try:
                return Questionnaire.objects.get(pk=id), None
            except Questionnaire.DoesNotExist:
                return None, f"No questionnaire with id {id}."

        questionnaire, err = await asyncio.to_thread(_db)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        embed = build_questionnaire_embed(questionnaire)
        view = QuestionnaireAnswerView(
            questionnaire.id, questionnaire.questions, form_title=questionnaire.title
        )
        target = channel or interaction.channel
        try:
            message = await target.send(embed=embed, view=view)
        except discord.Forbidden:
            await interaction.followup.send(
                f"I lack **Send Messages / Embed Links** permission in "
                f"{target.mention} — nothing was posted.",
                ephemeral=True,
            )
            return
        await asyncio.to_thread(
            Questionnaire.objects.filter(pk=id).update,
            channel_id=str(target.id),
            message_id=str(message.id),
        )
        await interaction.followup.send(
            f"Questionnaire #{id} reposted in {target.mention} — the old post's "
            f"Open form button still works, but new results now track this post.",
            ephemeral=True,
        )

    # ------------------------------------------------------------- errors

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                "You need the Kimaki or Admin role to use questionnaire commands.",
                ephemeral=True,
            )
            return
        if isinstance(error, app_commands.CommandInvokeError) and isinstance(
            error.original, discord.Forbidden
        ):
            await interaction.followup.send(
                "Discord said **403 Missing Permissions** — I can't post there. "
                "Give the bot Send Messages + Embed Links in this channel, or "
                "re-run `/questionnaire create` with a `channel` I can post in.",
                ephemeral=True,
            )
            return
        raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(QuestionnaireCog(bot))
