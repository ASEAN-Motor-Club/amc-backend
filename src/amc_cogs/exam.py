"""Discord exam system — scored exams built on the questionnaire machinery.

``/exam`` group (create / schema / results / export / close / repost)
restricted to the Kimaki and Admin roles (same gate as /questionnaire).
Anyone may take a posted exam; grading is automatic against the per-question
answer key.

Exam JSON = questionnaire JSON plus a top-level ``pass_mark``/``max_attempts``
and a per-question ``answer``:

    {
      "title": str,
      "description": str (optional),
      "pass_mark": int 1-100 (optional, default 80),
      "max_attempts": int 1-10 (optional, default 1),
      "questions": [
        {"text": str,                      # Label text (max 45 chars)
         "type": "single"|"radio"|"multi"|"check"|"text",
         "description": str (optional, max 100 chars),
         "options": [str, ...],            # choice types (same caps as surveys)
         "answer": str | [str, ...],       # REQUIRED for choice types
         "required": bool (optional, default true)
        }
      ]
    }

Grading: single/radio compare the chosen string to ``answer``; multi/check
compare the chosen set to ``answer`` (order-insensitive, all-or-nothing).
``text`` questions are recorded but never scored. Score = correct/graded*100;
pass iff score >= pass_mark. Failed takers are NOT told which questions were
wrong — the key would leak across attempts.
"""

import asyncio
import csv
import io
import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

from amc.models import Exam, ExamAttempt
from amc_cogs.questionnaire import (
    QuestionnaireFormModal,
    _format_value,
    _int_field,
    _paginate_rows,
    _require_role,
    _validate_common,
    _validate_options,
)

log = logging.getLogger("amc.exam")

EXAM_OPEN_CUSTOM_ID_PREFIX = "exam_open:"


def exam_open_custom_id(exam_id: int) -> str:
    """Deterministic component custom_id so views survive bot restarts."""
    return f"{EXAM_OPEN_CUSTOM_ID_PREFIX}{exam_id}"


# ---------------------------------------------------------------- validation


def _validate_answer(
    q: dict, i: int, qtype: str, options: list[str]
) -> str | list[str]:
    answer = q.get("answer")
    if answer is None:
        raise ValueError(
            f'Question {i}: missing "answer" — exams grade against it. '
            "Set it to the exact correct option string (or a list of strings "
            "for multi/check)."
        )
    if qtype in ("single", "radio"):
        if not isinstance(answer, str):
            raise ValueError(
                f'Question {i}: "answer" for {qtype} must be one option '
                "string, not a list."
            )
        if answer.strip() not in options:
            raise ValueError(
                f'Question {i}: "answer" {answer.strip()!r} is not one of '
                "the question's options."
            )
        return answer.strip()
    if (
        not isinstance(answer, list)
        or not answer
        or not all(isinstance(a, str) for a in answer)
    ):
        raise ValueError(
            f'Question {i}: "answer" for {qtype} must be a non-empty list '
            "of option strings."
        )
    stripped = [a.strip() for a in answer]
    bad = [a for a in stripped if a not in options]
    if bad:
        raise ValueError(
            f'Question {i}: "answer" option(s) not in the question\'s '
            f"options: {bad[0]!r}"
        )
    if len(set(stripped)) != len(stripped):
        raise ValueError(f'Question {i}: "answer" contains duplicates.')
    return stripped


def validate_exam_payload(raw: str) -> dict:
    """Parse and validate the exam JSON (questions + answer key). ValueError."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Top level must be a JSON object.")  # noqa: TRY004
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError('"title" must be a non-empty string.')
    if len(title.strip()) > 200:
        raise ValueError(f'"title" is {len(title.strip())} chars; max is 200.')
    pass_mark = data.get("pass_mark", 80)
    if not isinstance(pass_mark, int) or isinstance(pass_mark, bool) or not (
        1 <= pass_mark <= 100
    ):
        raise ValueError('"pass_mark" must be an integer between 1 and 100.')
    max_attempts = data.get("max_attempts", 1)
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not (
        1 <= max_attempts <= 10
    ):
        raise ValueError('"max_attempts" must be an integer between 1 and 10.')
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ValueError('"description" must be a string.')  # noqa: TRY004
    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError('"questions" must be a non-empty list.')
    normalized: list[dict] = []
    graded = 0
    for i, q in enumerate(questions, 1):
        if not isinstance(q, dict):
            raise ValueError(f"Question {i} must be an object.")  # noqa: TRY004
        text, q_description, required = _validate_common(q, i)
        qtype = q.get("type", "single")
        entry: dict = {"text": text, "type": qtype, "required": required}
        if q_description:
            entry["description"] = q_description
        if qtype in ("single", "radio"):
            lo, hi = (1, 10) if qtype == "radio" else (1, 25)
            entry["options"] = _validate_options(q, i, lo, hi)
            entry["answer"] = _validate_answer(q, i, qtype, entry["options"])
            graded += 1
        elif qtype in ("multi", "check"):
            lo, hi = (1, 25) if qtype == "multi" else (1, 10)
            entry["options"] = _validate_options(q, i, lo, hi)
            if qtype == "multi":
                entry["min_values"] = _int_field(q, i, "min_values", 0, 0, 25)
                entry["max_values"] = _int_field(q, i, "max_values", 25, 1, 25)
            entry["answer"] = _validate_answer(q, i, qtype, entry["options"])
            graded += 1
        elif qtype == "text":
            entry["style"] = q.get("style", "short")
            if entry["style"] not in ("short", "paragraph"):
                raise ValueError(
                    f'Question {i}: "style" must be "short" or "paragraph".'
                )
            entry["placeholder"] = str(q.get("placeholder", "")).strip()[:100]
            default_max = 1000 if entry["style"] == "paragraph" else 200
            entry["min_length"] = _int_field(q, i, "min_length", 0, 0, 4000)
            entry["max_length"] = _int_field(q, i, "max_length", default_max, 1, 4000)
        else:
            raise ValueError(
                f"Question {i}: exam questions must be single, radio, multi, "
                f'check or text (got {qtype!r}; "file" cannot be graded).'
            )
        normalized.append(entry)
    if graded == 0:
        raise ValueError(
            'The exam has no graded questions — at least one choice question '
            'with an "answer" is required.'
        )
    return {
        "title": title.strip()[:200],
        "description": description.strip(),
        "pass_mark": pass_mark,
        "max_attempts": max_attempts,
        "questions": normalized,
    }


# ------------------------------------------------------------------- grading


def grade_exam(questions: list[dict], answers: list) -> tuple[int, int]:
    """Grade one attempt against the key. Returns (correct, graded)."""
    correct = graded = 0
    for i, q in enumerate(questions):
        key = q.get("answer")
        if key is None:
            continue  # ungraded (text)
        graded += 1
        given = answers[i] if i < len(answers) else None
        if isinstance(key, list):
            if isinstance(given, list) and set(given) == set(key):
                correct += 1
        elif given == key:
            correct += 1
    return correct, graded


def score_exam(exam: Exam, correct: int, graded: int) -> tuple[int, bool]:
    """Percent score + pass verdict for one attempt."""
    score = round(100 * correct / graded) if graded else 0
    return score, score >= exam.pass_mark


# -------------------------------------------------------------------- embeds


def build_exam_embed(exam: Exam) -> discord.Embed:
    """Public embed shown for a (still-open or closed) exam."""
    closed = exam.closed
    embed = discord.Embed(
        title=f"{'[CLOSED] ' if closed else ''}{exam.title}",
        description=exam.description or None,
        color=discord.Color.red() if closed else discord.Color.green(),
    )
    embed.set_footer(
        text=(
            f"Exam #{exam.id} • pass ≥{exam.pass_mark}% • "
            f"{exam.max_attempts} attempt(s)"
        )
    )
    return embed


def build_results_embed(exam: Exam) -> discord.Embed:
    """Quick-view embed: per-taker best score + pass verdict."""
    attempts = list(exam.attempts.order_by("created_at"))
    by_user: dict[str, dict] = {}
    for a in attempts:
        row = by_user.setdefault(
            a.discord_user_id,
            {"name": a.discord_username, "n": 0, "best": 0, "passed": False},
        )
        row["n"] += 1
        row["best"] = max(row["best"], a.score)
        row["passed"] = row["passed"] or a.passed
    passed_count = sum(1 for r in by_user.values() if r["passed"])
    rows = sorted(
        by_user.values(), key=lambda r: (-int(r["passed"]), -r["best"])
    )
    lines = [
        f"{'✅' if r['passed'] else '❌'} {r['name']} — best {r['best']}% "
        f"({r['n']} attempt(s))"
        for r in rows
    ] or ["No attempts yet"]
    embed = discord.Embed(
        title=f"Results — {exam.title}",
        color=discord.Color.gold(),
    )
    embed.description = (
        f"{len(by_user)} taker(s) • {passed_count} passed • "
        f"{len(attempts)} attempt(s) • CLOSED" if exam.closed else
        f"{len(by_user)} taker(s) • {passed_count} passed • "
        f"{len(attempts)} attempt(s)"
    )
    embed.description += "\n\n" + "\n".join(lines)
    embed.description = embed.description[:4000]
    embed.set_footer(text=f"Exam #{exam.id}")
    return embed


def build_results_csv(exam: Exam) -> io.StringIO:
    """CSV: one row per attempt — respondent, attempt no, score, answers."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["exam_id", "respondent_id", "respondent_name", "attempt",
         "submitted_at", "score", "passed"]
        + [f"Q{i + 1}: {q['text']}" for i, q in enumerate(exam.questions)]
    )
    attempt_no: dict[str, int] = {}
    for a in exam.attempts.order_by("created_at"):
        attempt_no[a.discord_user_id] = attempt_no.get(a.discord_user_id, 0) + 1
        row = [
            exam.id,
            a.discord_user_id,
            a.discord_username,
            attempt_no[a.discord_user_id],
            a.created_at.isoformat(),
            a.score,
            a.passed,
        ]
        for i, _q in enumerate(exam.questions):
            value = a.answers[i] if i < len(a.answers) else None
            row.append(_format_value(value))
        writer.writerow(row)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------- answer UI

EXAM_DOC = (
    'Schema: {"title": str, "description": str (optional), '
    '"pass_mark": int 1-100 (default 80), "max_attempts": int 1-10 '
    '(default 1), "questions": [{"text": str (≤45 chars), '
    '"type": "single"|"radio"|"multi"|"check"|"text", '
    '"description": str (optional, ≤100 chars), '
    '"options": [str, ...] (single/multi: 1-25; radio/check: 1-10), '
    '"answer": str (single/radio) or [str, ...] (multi/check), '
    '"required": bool (optional, default true); text only: "style", '
    '"placeholder", "min_length", "max_length"}]} '
    "Use /exam schema for examples."
)


class ExamAnswerView(discord.ui.View):
    """The public exam embed view: one "Start exam" button (persistent).

    Carries the attribute contract shared with the questionnaire modal
    machinery (questionnaire_id/questions/form_title/selections plus
    store_answers/do_submit); ``questionnaire_id`` holds the exam id.
    """

    def __init__(self, exam_id: int, questions: list[dict], form_title: str = "Exam"):
        super().__init__(timeout=None)  # persistent across restarts
        self.questionnaire_id = exam_id  # machinery contract — NOT a questionnaire
        self.exam_id = exam_id
        self.questions = questions
        self.form_title = form_title
        self.selections: dict[int, object] = {}
        self.add_item(_ExamOpenFormButton(self))

    def store_answers(self, answers: dict[int, object]) -> None:
        self.selections = {**self.selections, **answers}

    async def do_submit(self, interaction: discord.Interaction) -> None:
        missing = [
            i + 1
            for i, q in enumerate(self.questions)
            if q.get("required", True) and i not in self.selections
        ]
        if missing:
            log.info(
                "exam submit incomplete: exam=%s user=%s missing=%s",
                self.exam_id,
                interaction.user.id,
                missing,
            )
            await interaction.response.send_message(
                f"Please answer question(s): {', '.join(map(str, missing))}.",
                ephemeral=True,
            )
            return
        log.info(
            "exam submit: exam=%s user=%s",
            self.exam_id,
            interaction.user.id,
        )
        await _grade_and_save(interaction, self.exam_id, dict(self.selections))

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        log.exception(
            "exam view error: exam=%s user=%s item=%s",
            self.exam_id,
            interaction.user.id,
            type(item).__name__,
        )


class _ExamOpenFormButton(discord.ui.Button):
    def __init__(self, parent_view: ExamAnswerView):
        super().__init__(
            label="Start exam",
            style=discord.ButtonStyle.primary,
            custom_id=exam_open_custom_id(parent_view.exam_id),
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        exam_id = self.parent_view.exam_id
        user = getattr(interaction, "user", None)
        log.info(
            "Start exam clicked: exam=%s user=%s message=%s",
            exam_id,
            getattr(user, "id", None),
            interaction.message.id if interaction.message else None,
        )
        try:
            pages = _paginate_rows(list(enumerate(self.parent_view.questions)))
            log.info(
                "Start exam: exam=%s built %d modal page(s) (rows/page=%s)",
                exam_id,
                len(pages),
                [len(p) for p in pages],
            )
            await interaction.response.send_modal(
                QuestionnaireFormModal(
                    self.parent_view, pages[0], 1, len(pages), next_pages=pages[1:]
                )
            )
            log.info("Start exam: exam=%s modal sent OK", exam_id)
        except Exception:
            log.exception(
                "Start exam FAILED: exam=%s user=%s",
                exam_id,
                getattr(user, "id", None),
            )
            raise


def grade_and_save_db(
    exam_id: int,
    discord_user_id: str,
    discord_username: str,
    selections: dict,
) -> tuple[str, bool, int, int]:
    """Grade one attempt and persist it. Returns (message, ok, attempt_no, score).

    Synchronous — production calls it via ``asyncio.to_thread`` inside
    :func:`_grade_and_save`; tests call it directly (no event loop).
    """
    try:
        exam = Exam.objects.get(pk=exam_id)
    except Exam.DoesNotExist:
        return "This exam no longer exists.", False, 0, 0
    if exam.closed:
        return "This exam is closed.", False, 0, 0
    answers = [selections.get(i) for i in range(len(exam.questions))]
    prior = ExamAttempt.objects.filter(exam=exam, discord_user_id=discord_user_id)
    used = prior.count()
    if used and prior.filter(passed=True).exists():
        return "You already passed this exam.", False, used, 0
    if used >= exam.max_attempts:
        return (
            f"No attempts left ({exam.max_attempts} used).",
            False,
            used,
            0,
        )
    correct, graded = grade_exam(exam.questions, answers)
    score, passed = score_exam(exam, correct, graded)
    ExamAttempt.objects.create(
        exam=exam,
        discord_user_id=discord_user_id,
        discord_username=discord_username,
        answers=answers,
        correct=correct,
        graded=graded,
        score=score,
        passed=passed,
    )
    if passed:
        message = f"✅ **Passed** — {correct}/{graded} ({score}%)."
    else:
        message = (
            f"❌ **Failed** — {correct}/{graded} ({score}%; "
            f"pass ≥{exam.pass_mark}%). Attempt {used + 1}"
            f"/{exam.max_attempts} used."
        )
    return message, True, used + 1, score


async def _grade_and_save(
    interaction: discord.Interaction, exam_id: int, selections: dict
) -> None:
    message, ok, attempt_no, score = await asyncio.to_thread(
        grade_and_save_db,
        exam_id,
        str(interaction.user.id),
        interaction.user.display_name,
        selections,
    )
    log.info(
        "Exam grade save: exam=%s user=%s ok=%s attempt=%s score=%s msg=%r",
        exam_id,
        interaction.user.id,
        ok,
        attempt_no,
        score,
        message,
    )
    await interaction.response.send_message(message, ephemeral=True)


# -------------------------------------------------------------------- the cog


class ExamCog(commands.Cog):
    exam_group = app_commands.Group(
        name="exam",
        description="Create and manage scored exams",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        """Schedule the persistent-view restore when the cog is added."""

        async def _restore_when_ready() -> None:
            await self.bot.wait_until_ready()
            try:
                await self.restore_persistent_views()
            except Exception:
                log.exception("Exam persistent-view restore crashed")

        self.bot.loop.create_task(_restore_when_ready())

    async def restore_persistent_views(self) -> None:
        """Re-register Start-exam views for every open exam."""

        def _open_rows() -> list[Exam]:
            return list(Exam.objects.filter(closed=False))

        rows = await asyncio.to_thread(_open_rows)
        log.info("Exam view restore: %d open exam(s)", len(rows))
        for exam in rows:
            view = ExamAnswerView(exam.id, exam.questions, form_title=exam.title)
            self.bot.add_view(view)
            log.info(
                "Exam view restore: registered view exam=%s custom_id=%s",
                exam.id,
                exam_open_custom_id(exam.id),
            )
            # Fix posts made before deterministic ids: rebuild the button.
            try:
                channel = self.bot.get_channel(int(exam.channel_id))
                if channel is None:
                    channel = await self.bot.fetch_channel(int(exam.channel_id))
                if not hasattr(channel, "fetch_message"):
                    log.warning(
                        "Exam view restore: exam=%s channel=%s is a %s — "
                        "cannot fetch the posted message",
                        exam.id,
                        exam.channel_id,
                        type(channel).__name__,
                    )
                    continue
                message = await channel.fetch_message(int(exam.message_id))
                posted_ids: list[str] = []
                for row in message.components:
                    children = getattr(row, "children", [row])
                    for child in children:
                        cid = getattr(child, "custom_id", None)
                        if isinstance(cid, str):
                            posted_ids.append(cid)
                expected = exam_open_custom_id(exam.id)
                if posted_ids != [expected]:
                    log.warning(
                        "Exam view restore: exam=%s button id mismatch "
                        "(expected %s, found %s) — editing message in place",
                        exam.id,
                        expected,
                        posted_ids,
                    )
                    await message.edit(embed=build_exam_embed(exam), view=view)
            except Exception:
                log.exception(
                    "Exam view restore: FAILED for exam=%s — its Start exam "
                    "button may be dead until manually reposted",
                    exam.id,
                )

    @exam_group.command(
        name="create", description="Create a scored exam from exam JSON"
    )
    @_require_role()
    @app_commands.describe(
        json_string="Exam JSON inline (small exams), OR attach a .json file",
        questions_file="A .json file with the exam (overrides json_string)",
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
                f"Provide `json_string` or attach `questions_file`.\n{EXAM_DOC}",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            data = await asyncio.to_thread(validate_exam_payload, raw)
        except ValueError as exc:
            await interaction.followup.send(
                f"Invalid exam JSON:\n> {exc}\n{EXAM_DOC}", ephemeral=True
            )
            return

        def _db() -> Exam:
            return Exam.objects.create(
                title=data["title"],
                description=data["description"],
                questions=data["questions"],
                pass_mark=data["pass_mark"],
                max_attempts=data["max_attempts"],
                created_by_discord_id=str(interaction.user.id),
            )

        exam = await asyncio.to_thread(_db)

        embed = build_exam_embed(exam)
        view = ExamAnswerView(exam.id, data["questions"], form_title=data["title"])
        target = channel or interaction.channel
        try:
            message = await target.send(embed=embed, view=view)
        except discord.Forbidden:
            await interaction.followup.send(
                f"I lack **Send Messages / Embed Links** permission in "
                f"{target.mention} — the exam was created but NOT posted. "
                f"Fix the channel permissions or re-run `/exam create` with a "
                f"`channel` I can post in.",
                ephemeral=True,
            )
            return
        await asyncio.to_thread(
            Exam.objects.filter(pk=exam.id).update,
            channel_id=str(target.id),
            message_id=str(message.id),
        )
        await interaction.followup.send(
            f"Exam #{exam.id} posted in {target.mention} "
            f"(pass ≥{data['pass_mark']}%, {data['max_attempts']} attempt(s)). "
            f"Use `/exam results id:{exam.id}` for scores.",
            ephemeral=True,
        )

    @exam_group.command(
        name="schema",
        description="Exam JSON format (question types + answer key + example)",
    )
    async def schema(self, interaction: discord.Interaction):
        await interaction.response.send_message(EXAM_SCHEMA_TEXT, ephemeral=True)

    @exam_group.command(
        name="results", description="Per-taker scores (best attempt + verdict)"
    )
    @_require_role()
    @app_commands.describe(id="Exam id (from the posted embed footer)")
    async def results(self, interaction: discord.Interaction, id: int):
        await interaction.response.defer(ephemeral=True)

        def _db() -> tuple[Exam | None, str | None]:
            try:
                return Exam.objects.get(pk=id), None
            except Exam.DoesNotExist:
                return None, f"No exam with id {id}."

        exam, err = await asyncio.to_thread(_db)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return
        embed = await asyncio.to_thread(build_results_embed, exam)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @exam_group.command(name="export", description="Export attempts to CSV")
    @_require_role()
    @app_commands.describe(id="Exam id (from the posted embed footer)")
    async def export(self, interaction: discord.Interaction, id: int):
        await interaction.response.defer(ephemeral=True)

        def _db() -> tuple[Exam | None, str | None]:
            try:
                return Exam.objects.get(pk=id), None
            except Exam.DoesNotExist:
                return None, f"No exam with id {id}."

        exam, err = await asyncio.to_thread(_db)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return
        buf = await asyncio.to_thread(build_results_csv, exam)
        filename = f"exam_{id}_results.csv"
        await interaction.followup.send(
            file=discord.File(
                io.BytesIO(buf.getvalue().encode("utf-8")), filename=filename
            ),
            ephemeral=True,
        )

    @exam_group.command(name="close", description="Close an exam (stops attempts)")
    @_require_role()
    @app_commands.describe(id="Exam id (from the posted embed footer)")
    async def close(self, interaction: discord.Interaction, id: int):
        await interaction.response.defer(ephemeral=True)

        def _db() -> tuple[Exam | None, str | None]:
            try:
                return Exam.objects.get(pk=id), None
            except Exam.DoesNotExist:
                return None, f"No exam with id {id}."

        exam, err = await asyncio.to_thread(_db)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return
        await asyncio.to_thread(Exam.objects.filter(pk=id).update, closed=True)
        exam.closed = True
        try:
            channel = interaction.client.get_channel(int(exam.channel_id))
            if channel:
                message = await channel.fetch_message(int(exam.message_id))
                await message.edit(embed=build_exam_embed(exam), view=None)
        except (ValueError, discord.HTTPException):
            pass
        await interaction.followup.send(f"Exam #{id} closed.", ephemeral=True)

    @exam_group.command(
        name="repost", description="Repost an exam post (same embed + Start exam)"
    )
    @_require_role()
    @app_commands.describe(
        id="Exam id (from the posted embed footer)",
        channel="Channel to repost in (defaults to this channel)",
    )
    async def repost(
        self,
        interaction: discord.Interaction,
        id: int,
        channel: discord.TextChannel | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        def _db() -> tuple[Exam | None, str | None]:
            try:
                return Exam.objects.get(pk=id), None
            except Exam.DoesNotExist:
                return None, f"No exam with id {id}."

        exam, err = await asyncio.to_thread(_db)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        embed = build_exam_embed(exam)
        view = ExamAnswerView(exam.id, exam.questions, form_title=exam.title)
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
            Exam.objects.filter(pk=id).update,
            channel_id=str(target.id),
            message_id=str(message.id),
        )
        await interaction.followup.send(
            f"Exam #{id} reposted in {target.mention} — the old post's "
            f"Start exam button still works, but results now track this post.",
            ephemeral=True,
        )

    # ------------------------------------------------------------- errors

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                "You need the Kimaki or Admin role to use exam commands.",
                ephemeral=True,
            )
            return
        if isinstance(error, app_commands.CommandInvokeError) and isinstance(
            error.original, discord.Forbidden
        ):
            await interaction.followup.send(
                "Discord said **403 Missing Permissions** — I can't post there. "
                "Give the bot Send Messages + Embed Links in this channel, or "
                "re-run `/exam create` with a `channel` I can post in.",
                ephemeral=True,
            )
            return
        raise error


EXAM_SCHEMA_TEXT = """\
**Exam JSON — same question types as surveys, plus an answer key**

Top level: `title` (str), `description` (str, optional),
`pass_mark` (int 1-100, default 80), `max_attempts` (int 1-10, default 1),
`questions` (list; label ≤45 chars, description ≤100, option ≤100).

Choice questions (`single`/`radio`/`multi`/`check`) REQUIRE `answer` —
the exact correct option string (a list of strings for multi/check).
`text` questions are recorded but ungraded. `file` is not allowed.
Score = correct/graded×100; pass at `pass_mark`. Failed takers see their
score but not which questions were wrong.

Example:
```json
{
  "title": "Police Academy Entrance Exam",
  "pass_mark": 80,
  "max_attempts": 1,
  "questions": [
    {"text": "Q1 Radio procedure", "type": "single",
     "description": "First call when stopping a vehicle",
     "options": ["Unit to dispatch", "Dispatch to unit", "All units alert"],
     "answer": "Unit to dispatch"},
    {"text": "Q2 Pursuit rules", "type": "check",
     "description": "Tick every required rule",
     "options": ["Notify dispatch", "Two units minimum",
                 "Break off at city limits"],
     "answer": ["Notify dispatch", "Two units minimum"]},
    {"text": "Anything to add?", "type": "text", "required": false}
  ]
}
```
"""


async def setup(bot: commands.Bot):
    """Extension entry point (load_extension path only).

    The production bot adds cogs directly in setup_hook, so this is never
    called there; the restore lives in ExamCog.cog_load.
    """
    await bot.add_cog(ExamCog(bot))
