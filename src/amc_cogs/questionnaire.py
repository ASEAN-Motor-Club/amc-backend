"""Discord questionnaire system.

``/questionnaire`` group (create / results / export / close) restricted to the
Kimaki and Admin roles. Anyone may answer the public embed posted by create.

Questions JSON schema (accepted inline or as a .json attachment):

    {
      "title": str,
      "description": str (optional),
      "response_mode": "single"|"multiple" (optional, default "single"),
      "questions": [
        {"text": str, "type": "single"|"multi", "options": [str, ...]},
        {"text": str, "type": "text", "style": "short"|"paragraph" (optional,
         default "short"), "placeholder": str (optional), "required": bool
         (optional, default true)}
      ]
    }

``text`` questions are answered in a Discord Modal (popup text inputs)
opened via the "Answer" button on the embed; ``single``/``multi`` questions
use dropdowns on the embed itself. Both kinds may be mixed freely; the
button is only rendered when the questionnaire has at least one text
question.
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
    "\"questions\": [{\"text\": str, \"type\": \"single\"|\"multi\", "
    "\"options\": [str, ...]}]} — max 25 options per question, max 10 questions."
)


# ---------------------------------------------------------------- pure helpers


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
    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("\"questions\" must be a non-empty list.")
    if len(questions) > 10:
        raise ValueError("Maximum 10 questions per questionnaire.")
    normalized: list[dict] = []
    for i, q in enumerate(questions, 1):
        if not isinstance(q, dict):
            raise ValueError(f"Question {i} must be an object.")  # noqa: TRY004
        text = q.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Question {i}: \"text\" must be a non-empty string.")
        qtype = q.get("type", "single")
        if qtype not in ("single", "multi", "text"):
            raise ValueError(
                f"Question {i}: \"type\" must be \"single\", \"multi\" or \"text\"."
            )
        if qtype == "text":
            style = q.get("style", "short")
            if style not in ("short", "paragraph"):
                raise ValueError(
                    f"Question {i}: \"style\" must be \"short\" or \"paragraph\"."
                )
            placeholder = q.get("placeholder", "")
            if not isinstance(placeholder, str):
                raise ValueError(f"Question {i}: \"placeholder\" must be a string.")
            required = q.get("required", True)
            if not isinstance(required, bool):
                raise ValueError(f"Question {i}: \"required\" must be a boolean.")
            normalized.append(
                {
                    "text": text.strip(),
                    "type": "text",
                    "style": style,
                    "placeholder": placeholder.strip()[:100],
                    "required": required,
                }
            )
            continue
        options = q.get("options")
        if not isinstance(options, list) or not (1 <= len(options) <= 25):
            raise ValueError(
                f"Question {i}: \"options\" must be a list of 1-25 strings."
            )
        if any(not isinstance(o, str) or not o.strip() for o in options):
            raise ValueError(f"Question {i}: every option must be a non-empty string.")
        normalized.append(
            {"text": text.strip(), "type": qtype, "options": [o.strip() for o in options]}
        )
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
                counts.update(value)
            elif value is not None:
                counts[value] += 1
        lines = [f"`{counts.get(o, 0):>3}` {o}" for o in q["options"]]
        embed.add_field(
            name=f"Q{i + 1}. {q['text']} ({q['type']})",
            value="\n".join(lines)[:1024] or "No options",
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
            row.append(", ".join(value) if isinstance(value, list) else (value or ""))
        writer.writerow(row)
    buf.seek(0)
    return buf


# --------------------------------------------------------------- answer UI


class QuestionnaireAnswerView(discord.ui.View):
    """Dropdowns (one per question) + Submit. Attached to the public embed.

    Questionnaires containing ``text``-type questions get an additional
    "Answer" button that opens a :class:`QuestionnaireTextModal` with one
    text input per text question (Discord caps modals at 5 inputs).
    """

    def __init__(self, questionnaire_id: int, questions: list[dict]):
        super().__init__(timeout=None)  # persistent across restarts
        self.questionnaire_id = questionnaire_id
        self.questions = questions
        self.selections: dict[int, str | list[str]] = {}
        for i, q in enumerate(questions):
            if q["type"] == "text":
                continue
            self.add_item(_QuestionSelect(self, i, q))
        if any(q["type"] == "text" for q in questions):
            self.add_item(_OpenTextModalButton(self))

    @discord.ui.button(label="Submit", style=discord.ButtonStyle.success)
    async def submit(self, interaction: discord.Interaction, _button: discord.ui.Button):
        missing = [
            i + 1
            for i, q in enumerate(self.questions)
            if q["type"] != "text" and i not in self.selections
        ]
        if missing:
            await interaction.response.send_message(
                f"Please answer question(s): {', '.join(map(str, missing))}.",
                ephemeral=True,
            )
            return
        await _save_response(
            interaction, self.questionnaire_id, self.selections, self.text_answers
        )
    # Text answers collected by the modal live here between the modal submit
    # and the Submit press. Populated via `store_text_answers` on the view.
    @property
    def text_answers(self) -> dict[int, str]:
        return getattr(self, "_text_answers", {})

    def store_text_answers(self, answers: dict[int, str]) -> None:
        self._text_answers = {**self.text_answers, **answers}


class QuestionnaireTextModal(discord.ui.Modal):
    """One TextInput per text question (Discord allows max 5 per modal)."""

    def __init__(self, parent_view: QuestionnaireAnswerView, text_qs: list[tuple[int, dict]]):
        super().__init__(title="Text questions")
        self.parent_view = parent_view
        self.text_qs = text_qs
        self.inputs: list[discord.ui.TextInput] = []
        for i, q in text_qs[:5]:
            inp = discord.ui.TextInput(
                label=f"Q{i + 1}: {q['text'][:40]}",
                style=(
                    discord.TextStyle.paragraph
                    if q.get("style") == "paragraph"
                    else discord.TextStyle.short
                ),
                required=q.get("required", True),
                max_length=1000 if q.get("style") == "paragraph" else 200,
                placeholder=(q.get("placeholder") or None),
            )
            self.inputs.append(inp)
            self.add_item(inp)

    async def on_submit(self, interaction: discord.Interaction):
        answers = {
            self.text_qs[i][0]: child.value.strip()
            for i, child in enumerate(self.inputs)
        }
        self.parent_view.store_text_answers(answers)
        await interaction.response.send_message(
            "Text answers saved — now press **Submit** on the embed to send "
            "your full response.",
            ephemeral=True,
        )


class _OpenTextModalButton(discord.ui.Button):
    """Opens the text-questions Modal. Only added when text questions exist."""

    def __init__(self, parent_view: "QuestionnaireAnswerView"):
        super().__init__(
            label="Answer text questions", style=discord.ButtonStyle.primary
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        text_qs = [
            (i, q)
            for i, q in self.parent_view.questions
            if q["type"] == "text"
        ]
        await interaction.response.send_modal(
            QuestionnaireTextModal(self.parent_view, text_qs)
        )


class _QuestionSelect(discord.ui.Select):
    def __init__(self, view: "QuestionnaireAnswerView", index: int, question: dict):
        self.parent_view: QuestionnaireAnswerView = view
        self._index = index
        options = [
            discord.SelectOption(label=o[:100], value=o[:100])
            for o in question["options"][:25]
        ]
        self._multi = question["type"] == "multi"
        super().__init__(
            placeholder=f"Q{index + 1}: {question['text'][:90]}",
            options=options,
            min_values=1,
            max_values=1 if not self._multi else min(len(options), 25),
        )

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selections[self._index] = (
            list(self.values) if self._multi else self.values[0]
        )
        await interaction.response.defer(ephemeral=True)


async def _save_response(
    interaction: discord.Interaction,
    questionnaire_id: int,
    selections: dict,
    text_answers: dict | None = None,
) -> None:
    text_answers = text_answers or {}

    def _db() -> tuple[str, bool]:
        try:
            q = Questionnaire.objects.get(pk=questionnaire_id)
        except Questionnaire.DoesNotExist:
            return "This questionnaire no longer exists.", False
        if q.closed:
            return "This questionnaire is closed.", False
        answers: list = []
        for i in range(len(q.questions)):
            if i in selections:
                answers.append(selections[i])
            else:
                answers.append(text_answers.get(i, ""))
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

    message, ok = await asyncio.to_thread(_db)
    await interaction.response.send_message(message, ephemeral=True)
    if not ok:
        return


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
    )
    async def create(
        self,
        interaction: discord.Interaction,
        json_string: str | None = None,
        questions_file: discord.Attachment | None = None,
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
        view = QuestionnaireAnswerView(questionnaire.id, data["questions"])
        message = await interaction.channel.send(embed=embed, view=view)
        await asyncio.to_thread(
            Questionnaire.objects.filter(pk=questionnaire.id).update,
            channel_id=str(interaction.channel_id),
            message_id=str(message.id),
        )
        await interaction.followup.send(
            f"Questionnaire #{questionnaire.id} posted. "
            f"Use `/questionnaire results id:{questionnaire.id}` for tallies.",
            ephemeral=True,
        )

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
        raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(QuestionnaireCog(bot))
