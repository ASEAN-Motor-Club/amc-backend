"""Tests for the questionnaire cog (pure helpers + wiring; no Discord I/O)."""

import asyncio
import csv
import io
import json
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from amc.models import Questionnaire, QuestionnaireResponse
from amc_cogs.questionnaire import (
    QuestionnaireAnswerView,
    QuestionnaireCog,
    QuestionnaireTextModal,
    build_questionnaire_embed,
    build_results_csv,
    build_results_embed,
    validate_questions_payload,
)

VALID_JSON = json.dumps(
    {
        "title": "Test Survey",
        "description": "A test",
        "questions": [
            {"text": "Favourite colour?", "type": "single",
             "options": ["Red", "Green", "Blue"]},
            {"text": "Pick fruits", "type": "multi",
             "options": ["Apple", "Banana"]},
        ],
    }
)


# ------------------------------------------------------------- validation


def test_validate_accepts_valid_payload():
    data = validate_questions_payload(VALID_JSON)
    assert data["title"] == "Test Survey"
    assert data["response_mode"] == "single"
    assert len(data["questions"]) == 2
    assert data["questions"][0]["type"] == "single"


def test_validate_rejects_bad_json():
    with pytest.raises(ValueError, match="Invalid JSON"):
        validate_questions_payload("{not json")


def test_validate_rejects_empty_questions():
    with pytest.raises(ValueError, match="non-empty list"):
        validate_questions_payload('{"title": "x", "questions": []}')


def test_validate_rejects_bad_type():
    with pytest.raises(ValueError, match='"type"'):
        validate_questions_payload(
            '{"title": "x", "questions": [{"text": "q", "type": "radio", "options": ["a"]}]}'
        )


def test_validate_rejects_too_many_options():
    options = list("abcdefghijklmnopqrstuvwxyz1")
    payload = json.dumps(
        {"title": "x", "questions": [{"text": "q", "options": options}]}
    )
    with pytest.raises(ValueError, match="1-25"):
        validate_questions_payload(payload)


def test_validate_defaults_response_mode():
    data = validate_questions_payload(VALID_JSON)
    assert data["response_mode"] == "single"
    data = validate_questions_payload(
        json.dumps({**json.loads(VALID_JSON), "response_mode": "multiple"})
    )
    assert data["response_mode"] == "multiple"


# ------------------------------------------------------------------ embeds


@pytest.fixture
def questionnaire(db):
    return Questionnaire.objects.create(
        title="Test Survey",
        description="A test",
        questions=json.loads(VALID_JSON)["questions"],
        created_by_discord_id="123",
    )


def test_build_questionnaire_embed_open(questionnaire):
    embed = build_questionnaire_embed(questionnaire)
    assert embed.title == "Test Survey"
    assert embed.color == discord.Color.blurple()
    assert f"#{questionnaire.id}" in embed.footer.text


def test_build_questionnaire_embed_closed(questionnaire):
    questionnaire.closed = True
    embed = build_questionnaire_embed(questionnaire)
    assert embed.title.startswith("[CLOSED]")
    assert embed.color == discord.Color.red()


def test_build_results_embed_tallies(questionnaire):
    QuestionnaireResponse.objects.create(
        questionnaire=questionnaire,
        discord_user_id="1",
        discord_username="alice",
        answers=["Red", ["Apple"]],
    )
    QuestionnaireResponse.objects.create(
        questionnaire=questionnaire,
        discord_user_id="2",
        discord_username="bob",
        answers=["Blue", ["Apple", "Banana"]],
    )
    embed = build_results_embed(questionnaire)
    assert "2 response(s)" in embed.description
    q1 = embed.fields[0]
    assert "`  1` Red" in q1.value
    assert "`  1` Blue" in q1.value
    q2 = embed.fields[1]
    assert "`  2` Apple" in q2.value
    assert "`  1` Banana" in q2.value


def test_build_results_csv(questionnaire):
    QuestionnaireResponse.objects.create(
        questionnaire=questionnaire,
        discord_user_id="1",
        discord_username="alice",
        answers=["Red", ["Apple", "Banana"]],
    )
    buf = build_results_csv(questionnaire)
    rows = list(csv.reader(io.StringIO(buf.getvalue())))
    assert rows[0][:4] == [
        "questionnaire_id", "respondent_id", "respondent_name", "submitted_at"
    ]
    assert "Favourite colour?" in rows[0][4]
    assert rows[1][3 + 1] == "Red"
    assert rows[1][3 + 2] == "Apple, Banana"


# -------------------------------------------------------------------- view


def test_answer_view_has_one_select_per_question(questionnaire):
    async def run():
        return QuestionnaireAnswerView(questionnaire.id, questionnaire.questions)

    view = asyncio.run(run())
    selects = [c for c in view.children if isinstance(c, discord.ui.Select)]
    buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
    assert len(selects) == 2
    # No text questions in this fixture → only the Submit button renders.
    assert {b.label for b in buttons} == {"Submit"}
    assert selects[0].max_values == 1  # single
    assert selects[1].max_values == 2  # multi


def test_answer_view_text_questions_render_modal_button():
    questions = [
        {"text": "Pick one", "type": "single", "options": ["A", "B"]},
        {"text": "Tell us more", "type": "text", "style": "paragraph"},
    ]

    async def run():
        return QuestionnaireAnswerView(1, questions)

    view = asyncio.run(run())
    selects = [c for c in view.children if isinstance(c, discord.ui.Select)]
    buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
    assert len(selects) == 1  # dropdown questions only
    assert {b.label for b in buttons} == {"Answer text questions", "Submit"}


def test_validate_text_question():
    data = validate_questions_payload(
        json.dumps(
            {
                "title": "x",
                "questions": [
                    {"text": "Say something", "type": "text",
                     "style": "paragraph", "placeholder": "hi", "required": False}
                ],
            }
        )
    )
    q = data["questions"][0]
    assert q["type"] == "text"
    assert q["style"] == "paragraph"
    assert q["placeholder"] == "hi"
    assert q["required"] is False


def test_validate_text_question_defaults():
    data = validate_questions_payload(
        json.dumps({"title": "x", "questions": [{"text": "q", "type": "text"}]})
    )
    q = data["questions"][0]
    assert q["style"] == "short"
    assert q["placeholder"] == ""
    assert q["required"] is True


def test_validate_rejects_bad_text_style():
    with pytest.raises(ValueError, match="style"):
        validate_questions_payload(
            json.dumps(
                {"title": "x", "questions": [{"text": "q", "type": "text", "style": "huge"}]}
            )
        )


def test_modal_collects_text_answers():
    questions = [{"text": "Tell us", "type": "text"}]

    async def run():
        view = QuestionnaireAnswerView(1, questions)
        text_qs = [(i, q) for i, q in enumerate(questions) if q["type"] == "text"]
        modal = QuestionnaireTextModal(view, text_qs)
        # simulate a filled input (value is backed by _value)
        modal.inputs[0]._value = "  hello world  "
        interaction = AsyncMock()
        await modal.on_submit(interaction)
        return view, interaction

    view, interaction = asyncio.run(run())
    assert view.text_answers == {0: "hello world"}
    assert interaction.response.send_message.called


def test_answer_view_submit_missing_answers(questionnaire):
    async def run():
        view = QuestionnaireAnswerView(questionnaire.id, questionnaire.questions)
        interaction = AsyncMock()
        await view.submit.callback(interaction)
        return interaction

    interaction = asyncio.run(run())
    msg = interaction.response.send_message.call_args.args[0]
    assert "question(s): 1, 2" in msg


# ------------------------------------------------------------------ wiring


def test_cog_group_has_four_commands():
    bot = MagicMock()
    cog = QuestionnaireCog(bot)
    commands_list = cog.questionnaire_group.commands
    assert {c.name for c in commands_list} == {"create", "results", "export", "close"}


def test_role_check_rejects_missing_role():
    member = MagicMock(spec=discord.Member)
    member.roles = []
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = member

    # A member holding NEITHER allowed role must be rejected.
    # app_commands.check() ATTACHES the predicate to the decorated function
    # (via __discord_app_commands_checks__) and returns it unchanged — so
    # grab the attached predicate and await it.
    import amc_cogs.questionnaire as qmod

    async def dummy(interaction):
        return True

    qmod._require_role()(dummy)
    (predicate,) = dummy.__discord_app_commands_checks__

    async def run():
        await predicate(interaction)

    with pytest.raises(app_commands.CheckFailure):
        asyncio.run(run())


