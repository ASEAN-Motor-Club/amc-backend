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
            {"text": "Anything else?", "type": "text", "required": False},
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


def test_validate_radio_rejects_zero_options():
    with pytest.raises(ValueError, match="options"):
        validate_questions_payload(json.dumps({
            "title": "x", "questions": [{"text": "q", "type": "radio", "options": []}],
        }))


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
        "questions": [{"text": "x" * 100, "type": "text"}],
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
        answers=["Red", ["Apple"], "because", "Good", ["Ham"], "extra text"],
    )
    QuestionnaireResponse.objects.create(
        questionnaire=questionnaire,
        discord_user_id="2",
        discord_username="bob",
        answers=["Blue", ["Apple", "Banana"], None, "Ok", [], None],
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
    assert "> extra text" in q6.value
    assert "No answers" not in q6.value


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
    assert {b.label for b in view.children} == {"Open form"}


def test_open_form_builds_first_page():
    questions = json.loads(VALID_JSON)["questions"]

    async def run():
        view = QuestionnaireAnswerView(1, questions, form_title="Test Survey")
        interaction = AsyncMock()
        open_btn = next(b for b in view.children if b.label == "Open form")
        await open_btn.callback(interaction)
        modal = interaction.response.send_modal.call_args.args[0]
        return modal

    modal = asyncio.run(run())
    # 6 single-row questions -> page 1 holds 5, page 2 the last
    assert len(modal.children) == 5
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
    assert ti.placeholder
    # radio -> RadioGroup
    assert isinstance(labels[3].component, discord.ui.RadioGroup)
    # check -> CheckboxGroup in ONE Label row with the question text
    cg = labels[4].component
    assert isinstance(cg, discord.ui.CheckboxGroup)
    assert [o.label for o in cg.options] == ["Ham", "Corn"]
    # radio -> RadioGroup
    assert isinstance(labels[3].component, discord.ui.RadioGroup)
    assert len(labels[3].component.options) == 3
    # check questions expand to per-option checkboxes on the next page
    assert labels[3].component.options[0].label == "Bad"



def test_modal_title_has_page_prefix():
    """Modal title is "[page/total] survey title", truncated to 45 chars."""

    async def run(form_title):
        questions = [
            {"text": f"q{i}", "type": "single", "options": ["a", "b"]}
            for i in range(12)
        ]
        view = QuestionnaireAnswerView(1, questions, form_title=form_title)
        interaction = AsyncMock()
        open_btn = next(b for b in view.children if b.label == "Open form")
        await open_btn.callback(interaction)
        return interaction.response.send_modal.call_args.args[0]

    modal = asyncio.run(run("Test Survey"))
    # 12 questions -> 3 pages
    assert modal.total_pages == 3
    assert modal.title == "[1/3] Test Survey"

    # long titles truncate but keep the prefix
    long_modal = asyncio.run(run("X" * 60))
    assert long_modal.title.startswith("[1/3] ")
    assert len(long_modal.title) <= 45

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
        page1.find_item("q4")._values = ["Ham"]
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
        page2.find_item("q5")._value = "  nah  "
        interaction2 = AsyncMock()
        await page2.on_submit(interaction2)
        return interaction2

    interaction2 = asyncio.run(run2())
    assert view.selections[4] == ["Ham"]
    assert view.selections[5] == "nah"
    assert "Submit" in interaction2.response.send_message.call_args.args[0]


def test_submit_requires_required_questions():
    questions = json.loads(VALID_JSON)["questions"]

    async def run():
        view = QuestionnaireAnswerView(1, questions, form_title="T")
        # only the optional text (index 2) answered
        view.selections = {2: "why"}
        interaction = AsyncMock()
        await view.do_submit(interaction)
        return interaction

    interaction = asyncio.run(run())
    msg = interaction.response.send_message.call_args.args[0]
    assert all(str(n) in msg for n in (1, 2, 4, 5))
    assert " 3 " not in msg or "3." not in msg


# ------------------------------------------------------------------ wiring


def test_cog_group_has_six_commands():
    bot = MagicMock()
    cog = QuestionnaireCog(bot)
    commands_list = cog.questionnaire_group.commands
    assert {c.name for c in commands_list} == {
        "create", "schema", "results", "export", "close", "repost"
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

# ---------- repost ----------

def test_repost_posts_identical_embed_and_updates_location():
    from unittest.mock import patch

    saved = {}

    class FakeQ:
        id = 7
        title = "Yuuka Survey"
        description = "d"
        questions = json.loads(VALID_JSON)["questions"]
        response_mode = "multi"
        channel_id = "999"
        message_id = "888"
        closed = False

    q = FakeQ()

    def fake_update(**kw):
        saved.update(kw)

    bot = MagicMock()
    cog = QuestionnaireCog(bot)
    interaction = AsyncMock(spec=discord.Interaction)
    interaction.user.id = 555
    interaction.response = AsyncMock()
    interaction.followup.send = AsyncMock()
    new_post = AsyncMock()
    new_post.id = 555001

    async def fake_send(embed=None, view=None):
        new_post.embed = embed
        new_post.view = view
        return new_post

    target_channel = MagicMock()
    target_channel.id = 42
    target_channel.send = fake_send
    interaction.channel = target_channel

    with patch(
        "amc_cogs.questionnaire.build_questionnaire_embed", return_value="EMBED"
    ), patch(
        "amc_cogs.questionnaire.Questionnaire.objects.get", return_value=q
    ), patch(
        "amc_cogs.questionnaire.Questionnaire.objects.filter"
    ) as fake_filter:
        fake_filter.return_value.update = fake_update
        asyncio.run(cog.repost.callback(cog, interaction, q.id))

    # embed identical to what the original post builder produced
    assert new_post.embed == "EMBED"
    # view is an answer view for the same questionnaire with same questions
    view = new_post.view
    assert isinstance(view, QuestionnaireAnswerView)
    assert view.questionnaire_id == q.id
    assert view.questions == q.questions
    # DB location updated to the new post
    assert saved == {"channel_id": "42", "message_id": str(new_post.id)}
    interaction.followup.send.assert_called_once()


@pytest.mark.django_db
def test_repost_target_channel_option():
    from unittest.mock import patch

    class FakeQ:
        id = 7
        title = "s"
        questions = json.loads(VALID_JSON)["questions"]
        response_mode = "multi"
        channel_id = ""
        message_id = ""
        closed = False

    q = FakeQ()

    bot = MagicMock()
    cog = QuestionnaireCog(bot)
    interaction = AsyncMock(spec=discord.Interaction)
    interaction.user.id = 555
    interaction.response = AsyncMock()
    interaction.followup.send = AsyncMock()

    other = AsyncMock()
    other.id = 777
    other.send = AsyncMock(return_value=MagicMock(id=555001))
    interaction.channel = MagicMock()
    interaction.channel.id = 42

    with patch(
        "amc_cogs.questionnaire.build_questionnaire_embed", return_value="E"
    ), patch(
        "amc_cogs.questionnaire.Questionnaire.objects.get", return_value=q
    ), patch(
        "amc_cogs.questionnaire.Questionnaire.objects.filter"
    ) as fake_filter:
        fake_filter.return_value.update.return_value = None
        asyncio.run(cog.repost.callback(cog, interaction, q.id, channel=other))

    other.send.assert_awaited_once()
    assert other.send.await_args.kwargs["view"].questionnaire_id == q.id


@pytest.mark.django_db

# ---------- check (Checkbox Group) + pagination invariants ----------

def test_check_question_is_one_label_row_with_question_text():
    raw = json.dumps({
        "title": "t",
        "questions": [{
            "text": "Which merch would you buy?",
            "type": "check",
            "options": ["Acrylic stand", "Voice pack"],
        }],
    })
    data = validate_questions_payload(raw)
    pages = _paginate_rows(list(enumerate(data["questions"])))
    rows = [lbl for _i, _q, rs in pages[0] for lbl in rs]
    assert len(rows) == 1
    label = rows[0]
    assert label.text == "Which merch would you buy?"
    cg = label.component
    assert isinstance(cg, discord.ui.CheckboxGroup)
    assert [o.label for o in cg.options] == ["Acrylic stand", "Voice pack"]


def test_check_group_respects_min_max_values():
    raw = json.dumps({
        "title": "t",
        "questions": [{
            "text": "Pick snacks", "type": "check", "min_values": 1, "max_values": 2,
            "options": ["A", "B", "C"],
        }],
    })
    data = validate_questions_payload(raw)
    pages = _paginate_rows(list(enumerate(data["questions"])))
    cg = pages[0][0][2][0].component
    assert cg.min_values == 1
    assert cg.max_values == 2


def test_pagination_never_exceeds_five_rows_per_page():
    raw = json.dumps({
        "title": "t",
        "questions": [
            {"text": f"Q{i} text", "type": "check",
             "options": ["opt a", "opt b", "opt c"]}
            for i in range(9)
        ],
    })
    data = validate_questions_payload(raw)
    pages = _paginate_rows(list(enumerate(data["questions"])))
    for page in pages:
        assert sum(len(rows) for _i, _q, rows in page) <= 5


def test_validator_accepts_more_than_ten_questions():
    raw = json.dumps({
        "title": "t",
        "questions": [
            {"text": f"Q{i}", "type": "text"} for i in range(15)
        ],
    })
    data = validate_questions_payload(raw)
    assert len(data["questions"]) == 15


def test_boolean_type_is_rejected():
    raw = json.dumps({
        "title": "t",
        "questions": [{"text": "OK?", "type": "boolean"}],
    })
    with pytest.raises(ValueError, match="type"):
        validate_questions_payload(raw)


def test_yuuka_survey_fits_two_pages():
    """check is now a single row — 7 questions = 7 rows = 2 pages (5+2)."""
    raw = json.dumps({
        "title": "Yuuka Fan Survey",
        "questions": [
            {"text": "Message for Yuuka", "type": "text", "style": "paragraph"},
            {"text": "Favorite stream type?", "type": "single",
             "options": ["Gaming", "Karaoke"]},
            {"text": "Which snacks?", "type": "multi", "max_values": 3,
             "options": ["Bubble tea", "Instant noodles", "Chips"]},
            {"text": "Rate last stream", "type": "radio",
             "options": ["Decent", "PEAK FICTION"]},
            {"text": "Which merch would you buy?", "type": "check",
             "options": ["Acrylic stand", "Voice pack", "Oshi mark hoodie",
                          "Signed photo"]},
            {"text": "Fan art submission", "type": "file"},
        ],
    })
    data = validate_questions_payload(raw)
    pages = _paginate_rows(list(enumerate(data["questions"])))
    row_counts = [sum(len(rows) for _i, _q, rows in page) for page in pages]
    assert len(pages) == 2
    assert row_counts == [5, 1]
