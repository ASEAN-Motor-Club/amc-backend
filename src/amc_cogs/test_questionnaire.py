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
    QuestionnaireFormModal,
    _paginate_rows,
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
            {"text": "Why?", "type": "text", "style": "paragraph",
             "placeholder": "tell us", "required": False},
            {"text": "Rank it", "type": "radio", "options": ["Bad", "Ok", "Good"]},
            {"text": "Toppings", "type": "check", "options": ["Ham", "Corn"]},
            {"text": "Subscribe?", "type": "boolean", "default": False},
        ],
    }
)


# ------------------------------------------------------------- validation


def test_validate_accepts_valid_payload():
    data = validate_questions_payload(VALID_JSON)
    assert data["title"] == "Test Survey"
    assert data["response_mode"] == "single"
    assert len(data["questions"]) == 6
    assert data["questions"][0]["type"] == "single"


def test_validate_rejects_bad_json():
    with pytest.raises(ValueError, match="Invalid JSON"):
        validate_questions_payload("{not json")


def test_validate_rejects_empty_questions():
    with pytest.raises(ValueError, match="non-empty list"):
        validate_questions_payload('{"title": "x", "questions": []}')


def test_validate_rejects_bad_type():
    with pytest.raises(ValueError, match="type"):
        validate_questions_payload(
            '{"title": "x", "questions": [{"text": "q", "type": "radio2", "options": ["a"]}]}'
        )


def test_validate_rejects_too_many_options():
    options = list("abcdefghijklmnopqrstuvwxyz1")
    payload = json.dumps(
        {"title": "x", "questions": [{"text": "q", "options": options}]}
    )
    with pytest.raises(ValueError, match="1-25"):
        validate_questions_payload(payload)


def test_validate_text_question_fields():
    data = validate_questions_payload(json.dumps({
        "title": "x",
        "questions": [{"text": "Say", "type": "text", "style": "paragraph",
                       "placeholder": "hi", "required": False,
                       "min_length": 5, "max_length": 100}],
    }))
    q = data["questions"][0]
    assert q["style"] == "paragraph"
    assert q["placeholder"] == "hi"
    assert q["required"] is False
    assert q["min_length"] == 5
    assert q["max_length"] == 100


def test_validate_rejects_bad_length_bounds():
    with pytest.raises(ValueError, match="min_length"):
        validate_questions_payload(json.dumps({
            "title": "x",
            "questions": [{"text": "q", "type": "text", "min_length": 9999}],
        }))


def test_validate_radio_needs_two_options():
    with pytest.raises(ValueError, match="2-10"):
        validate_questions_payload(json.dumps({
            "title": "x", "questions": [{"text": "q", "type": "radio", "options": ["a"]}],
        }))


def test_validate_boolean_default():
    data = validate_questions_payload(json.dumps({
        "title": "x", "questions": [{"text": "q", "type": "boolean", "default": True}],
    }))
    assert data["questions"][0]["default"] is True


def test_validate_file_question():
    data = validate_questions_payload(json.dumps({
        "title": "x", "questions": [{"text": "q", "type": "file",
                                     "min_values": 2, "max_values": 3}],
    }))
    q = data["questions"][0]
    assert q["min_values"] == 2
    assert q["max_values"] == 3


def test_validate_label_text_truncated():
    data = validate_questions_payload(json.dumps({
        "title": "x",
        "questions": [{"text": "x" * 100, "type": "boolean"}],
    }))
    assert len(data["questions"][0]["text"]) == 45


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
        answers=["Red", ["Apple"], "because", "Good", ["Ham"], True],
    )
    QuestionnaireResponse.objects.create(
        questionnaire=questionnaire,
        discord_user_id="2",
        discord_username="bob",
        answers=["Blue", ["Apple", "Banana"], None, "Ok", [], False],
    )
    embed = build_results_embed(questionnaire)
    assert "2 response(s)" in embed.description
    q1 = embed.fields[0]
    assert "`  1` Red" in q1.value
    assert "`  1` Blue" in q1.value
    q2 = embed.fields[1]
    assert "`  2` Apple" in q2.value
    assert "`  1` Banana" in q2.value
    q4 = embed.fields[3]
    assert "radio" in q4.name
    assert "`  1` Good" in q4.value
    q5 = embed.fields[4]
    assert "`  1` Ham" in q5.value
    q6 = embed.fields[5]
    assert "`  1` yes" in q6.value
    assert "`  1` no" in q6.value


def test_build_results_csv(questionnaire):
    QuestionnaireResponse.objects.create(
        questionnaire=questionnaire,
        discord_user_id="1",
        discord_username="alice",
        answers=["Red", ["Apple", "Banana"], "why not", "Good", ["Corn"], False],
    )
    buf = build_results_csv(questionnaire)
    rows = list(csv.reader(io.StringIO(buf.getvalue())))
    assert rows[0][:4] == [
        "questionnaire_id", "respondent_id", "respondent_name", "submitted_at"
    ]
    assert "Favourite colour?" in rows[0][4]
    assert rows[1][4] == "Red"
    assert rows[1][5] == "Apple, Banana"
    assert rows[1][6] == "why not"
    assert rows[1][8] == "Corn"
    assert rows[1][9] == "no"


# -------------------------------------------------------------------- view


def test_answer_view_is_form_button_only():
    questions = json.loads(VALID_JSON)["questions"]

    async def run():
        return QuestionnaireAnswerView(1, questions, form_title="T")

    view = asyncio.run(run())
    # No selects ever — everything lives in modals
    assert not [c for c in view.children if isinstance(c, discord.ui.Select)]
    assert {b.label for b in view.children} == {"Open form", "Submit"}


def test_open_form_builds_first_page():
    questions = json.loads(VALID_JSON)["questions"]

    async def run():
        view = QuestionnaireAnswerView(1, questions, form_title="Test Survey")
        interaction = AsyncMock()
        await view.open_form.callback(interaction)
        modal = interaction.response.send_modal.call_args.args[0]
        return modal

    modal = asyncio.run(run())
    # rows: q1(1)+q2(1)+q3(1)+q4(1)=4; q5 expands to 2 rows -> would overflow, so page 2
    assert len(modal.children) == 4
    assert modal.total_pages == 2
    labels = [c for c in modal.children]
    assert all(isinstance(c, discord.ui.Label) for c in labels)
    # single -> Select inside Label
    assert isinstance(labels[0].component, discord.ui.Select)
    assert labels[0].component.options[0].label == "Red"
    # multi -> Select with max_values
    assert labels[1].component.max_values == 2
    # text -> TextInput paragraph, optional
    ti = labels[2].component
    assert isinstance(ti, discord.ui.TextInput)
    assert ti.style == discord.TextStyle.paragraph
    assert ti.required is False
    assert ti.placeholder == "tell us"
    # radio -> RadioGroup
    assert isinstance(labels[3].component, discord.ui.RadioGroup)
    assert len(labels[3].component.options) == 3
    # check questions expand to per-option checkboxes on the next page
    assert labels[3].component.options[0].label == "Bad"


def test_modal_collect_and_chain():
    questions = json.loads(VALID_JSON)["questions"]

    async def run():
        view = QuestionnaireAnswerView(1, questions, form_title="T")
        pages = _paginate_rows(list(enumerate(questions)))
        page1 = QuestionnaireFormModal(view, pages[0], 1, 2, next_pages=pages[1:])
        # fill answers
        page1.find_item("q0")._values = ["Green"]
        page1.find_item("q1")._values = ["Apple", "Banana"]
        page1.find_item("q2")._value = "  because reasons  "
        page1.find_item("q3")._value = "Good"
        interaction = AsyncMock()
        await page1.on_submit(interaction)
        return view, interaction

    view, interaction = asyncio.run(run())
    assert view.selections[0] == "Green"
    assert view.selections[1] == ["Apple", "Banana"]
    assert view.selections[2] == "because reasons"
    assert view.selections[3] == "Good"
    # chained next-page button
    kwargs = interaction.response.send_message.call_args.kwargs
    btn = kwargs["view"].children[0]
    assert btn.label == "Open page 2 of 2"

    async def run2():
        page2 = QuestionnaireFormModal(
            view, btn.next_pages[0], 2, 2, next_pages=btn.next_pages[1:]
        )
        page2.find_item("q4o0")._value = True
        page2.find_item("q4o1")._value = False
        page2.find_item("q5")._value = False
        interaction2 = AsyncMock()
        await page2.on_submit(interaction2)
        return interaction2

    interaction2 = asyncio.run(run2())
    assert view.selections[4] == ["Ham"]
    assert view.selections[5] is False
    assert "Submit" in interaction2.response.send_message.call_args.args[0]


def test_submit_requires_required_questions():
    questions = json.loads(VALID_JSON)["questions"]

    async def run():
        view = QuestionnaireAnswerView(1, questions, form_title="T")
        # Q3 (radio) unanswered but required
        view.selections = {2: "why"}  # only text (not required) answered
        interaction = AsyncMock()
        await view.submit.callback(interaction)
        return interaction

    interaction = asyncio.run(run())
    msg = interaction.response.send_message.call_args.args[0]
    assert all(str(n) in msg for n in (1, 2, 4, 5, 6))
    assert "3" not in msg.replace("question(s):", "")


# ------------------------------------------------------------------ wiring


def test_cog_group_has_five_commands():
    bot = MagicMock()
    cog = QuestionnaireCog(bot)
    commands_list = cog.questionnaire_group.commands
    assert {c.name for c in commands_list} == {
        "create", "schema", "results", "export", "close"
    }


def test_role_check_rejects_missing_role():
    member = MagicMock(spec=discord.Member)
    member.roles = []
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = member

    import amc_cogs.questionnaire as qmod

    async def dummy(interaction):
        return True

    qmod._require_role()(dummy)
    (predicate,) = dummy.__discord_app_commands_checks__

    async def run():
        await predicate(interaction)

    with pytest.raises(app_commands.CheckFailure):
        asyncio.run(run())
