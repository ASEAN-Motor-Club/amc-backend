"""Tests for the exam cog (pure helpers + wiring; no Discord I/O).

The grade-and-save DB tests run the PRODUCTION path (asyncio.run + real
``asyncio.to_thread``): the ORM calls execute on a worker thread with no
running event loop, so they use ``@pytest.mark.django_db(transaction=True)``
(real commits; pytest-django flushes between tests). The worker thread's
connection cannot see uncommitted main-thread rows, which is why plain
``django_db`` transactions would fail with FK errors here.
"""

import asyncio
import csv
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from amc.models import Exam, ExamAttempt
from amc_cogs.exam import (
    ExamAnswerView,
    ExamCog,
    _paginate_rows,
    build_exam_embed,
    build_results_csv,
    build_results_embed,
    grade_and_save_db,
    grade_exam,
    score_exam,
    validate_exam_payload,
)

VALID_EXAM = json.dumps(
    {
        "title": "Police Academy Exam",
        "description": "Entrance exam",
        "questions": [
            {"text": "Q1 Who yields?", "type": "single",
             "options": ["The officer", "The other driver", "Both"],
             "answer": "The other driver"},
            {"text": "Q2 Required rules", "type": "check",
             "options": ["Notify dispatch", "Two units", "Break off"],
             "answer": ["Notify dispatch", "Two units"]},
            {"text": "Q3 Rate risk", "type": "radio",
             "options": ["Low", "High"],
             "answer": "High"},
            {"text": "Q4 Anything to add?", "type": "text", "required": False},
        ],
    }
)


def _interaction(user_id=42, name="Tester"):
    interaction = AsyncMock()
    interaction.user = SimpleNamespace(id=user_id, display_name=name)
    return interaction


# ------------------------------------------------------------- validation


def test_validate_accepts_valid_exam():
    data = validate_exam_payload(VALID_EXAM)
    assert data["title"] == "Police Academy Exam"
    assert data["pass_mark"] == 80
    assert data["max_attempts"] == 1
    assert data["questions"][0]["answer"] == "The other driver"
    assert data["questions"][1]["answer"] == ["Notify dispatch", "Two units"]


def test_validate_missing_answer_rejected():
    payload = json.dumps({
        "title": "T",
        "questions": [{"text": "Q1", "type": "single", "options": ["a", "b"]}],
    })
    with pytest.raises(ValueError, match="missing \"answer\""):
        validate_exam_payload(payload)


def test_validate_answer_not_in_options():
    payload = json.dumps({
        "title": "T",
        "questions": [{"text": "Q1", "type": "single", "options": ["a", "b"],
                       "answer": "c"}],
    })
    with pytest.raises(ValueError, match="not one of"):
        validate_exam_payload(payload)


def test_validate_multi_answer_needs_list():
    payload = json.dumps({
        "title": "T",
        "questions": [{"text": "Q1", "type": "multi", "options": ["a", "b"],
                       "answer": "a"}],
    })
    with pytest.raises(ValueError, match="list of option strings"):
        validate_exam_payload(payload)


def test_validate_multi_answer_unknown_option():
    payload = json.dumps({
        "title": "T",
        "questions": [{"text": "Q1", "type": "check", "options": ["a", "b"],
                       "answer": ["a", "zz"]}],
    })
    with pytest.raises(ValueError, match="not in the question's options"):
        validate_exam_payload(payload)


def test_validate_multi_answer_duplicates():
    payload = json.dumps({
        "title": "T",
        "questions": [{"text": "Q1", "type": "multi", "options": ["a", "b"],
                       "answer": ["a", "a"]}],
    })
    with pytest.raises(ValueError, match="duplicates"):
        validate_exam_payload(payload)


def test_validate_radio_answer_as_list_rejected():
    payload = json.dumps({
        "title": "T",
        "questions": [{"text": "Q1", "type": "radio", "options": ["a", "b"],
                       "answer": ["a", "b"]}],
    })
    with pytest.raises(ValueError, match="one option string"):
        validate_exam_payload(payload)


def test_validate_file_type_rejected():
    payload = json.dumps({
        "title": "T",
        "questions": [{"text": "Q1", "type": "file", "answer": "x"}],
    })
    with pytest.raises(ValueError, match="file.*cannot be graded"):
        validate_exam_payload(payload)


def test_validate_no_graded_questions():
    payload = json.dumps({
        "title": "T",
        "questions": [{"text": "Q1", "type": "text"}],
    })
    with pytest.raises(ValueError, match="no graded questions"):
        validate_exam_payload(payload)


def test_validate_pass_mark_bounds():
    base = {"title": "T", "questions": [
        {"text": "Q1", "type": "single", "options": ["a", "b"], "answer": "a"},
    ]}
    for bad in (0, 101, True):
        payload = json.dumps({**base, "pass_mark": bad})
        with pytest.raises(ValueError, match="pass_mark"):
            validate_exam_payload(payload)


def test_validate_max_attempts_bounds():
    base = {"title": "T", "questions": [
        {"text": "Q1", "type": "single", "options": ["a", "b"], "answer": "a"},
    ]}
    for bad in (0, 11):
        payload = json.dumps({**base, "max_attempts": bad})
        with pytest.raises(ValueError, match="max_attempts"):
            validate_exam_payload(payload)


def test_validate_label_length_still_enforced():
    payload = json.dumps({
        "title": "T",
        "questions": [{"text": "x" * 46, "type": "single",
                       "options": ["a", "b"], "answer": "a"}],
    })
    with pytest.raises(ValueError, match="max is 45"):
        validate_exam_payload(payload)


# ---------------------------------------------------------------- grading


def _exam_questions():
    return json.loads(VALID_EXAM)["questions"]


def test_grade_single_correct_and_wrong():
    # the sample exam grades 3 questions (single, check, radio); text is ungraded
    correct, graded = grade_exam(_exam_questions(), ["The other driver", None, None, None])
    assert (correct, graded) == (1, 3)
    correct, graded = grade_exam(_exam_questions(), ["The officer", None, None, None])
    assert (correct, graded) == (0, 3)


def test_grade_multi_order_insensitive():
    qs = _exam_questions()
    correct, _ = grade_exam(qs, [None, ["Two units", "Notify dispatch"], None, None])
    assert correct == 1


def test_grade_multi_partial_selection_wrong():
    correct, _ = grade_exam(_exam_questions(), [None, ["Notify dispatch"], None, None])
    assert correct == 0


def test_grade_radio():
    correct, _ = grade_exam(_exam_questions(), [None, None, "High", None])
    assert correct == 1


def test_grade_text_ungraded():
    correct, graded = grade_exam(_exam_questions(), ["a", "b", "c", "some text"])
    assert graded == 3  # text question never counts
    assert correct == 0


def test_grade_short_answers_list():
    correct, graded = grade_exam(_exam_questions(), ["The other driver"])
    assert (correct, graded) == (1, 3)


def test_score_pass_boundary():
    exam = SimpleNamespace(pass_mark=80)
    assert score_exam(exam, 8, 10) == (80, True)
    assert score_exam(exam, 79, 100) == (79, False)
    assert score_exam(exam, 0, 0) == (0, False)


# ------------------------------------------------------------------- view


def test_exam_view_has_start_button_only():
    async def run():
        return ExamAnswerView(1, _exam_questions(), form_title="T")

    view = asyncio.run(run())
    assert not [c for c in view.children if isinstance(c, discord.ui.Select)]
    assert [b.label for b in view.children] == ["Start exam"]
    assert view.children[0].custom_id == "exam_open:1"


def test_open_exam_builds_first_page():
    async def run():
        questions = [
            {"text": f"q{i}", "type": "single", "options": ["a", "b"],
             "answer": "a"}
            for i in range(6)
        ]
        view = ExamAnswerView(1, questions, form_title="Test Exam")
        interaction = _interaction()
        open_btn = next(b for b in view.children if b.label == "Start exam")
        await open_btn.callback(interaction)
        return interaction.response.send_modal.call_args.args[0]

    modal = asyncio.run(run())
    assert len(modal.children) == 5
    assert modal.total_pages == 2


def test_pagination_never_exceeds_five_rows():
    questions = [
        {"text": f"q{i}", "type": "single", "options": ["a", "b"], "answer": "a"}
        for i in range(13)
    ]
    pages = _paginate_rows(list(enumerate(questions)))
    assert [len(p) for p in pages] == [5, 5, 3]


def test_do_submit_requires_required_questions():
    async def run():
        view = ExamAnswerView(1, _exam_questions(), form_title="T")
        interaction = _interaction()
        await view.do_submit(interaction)
        return interaction

    interaction = asyncio.run(run())
    msg = interaction.response.send_message.call_args.args[0]
    assert "Please answer question(s)" in msg


# ----------------------------------------------------------------- embeds


@pytest.mark.django_db
def test_exam_embed_footer_and_closed():
    exam = Exam.objects.create(
        title="Academy Exam",
        questions=json.loads(VALID_EXAM)["questions"],
        created_by_discord_id="1",
    )
    embed = build_exam_embed(exam)
    assert "[CLOSED]" not in embed.title
    assert f"Exam #{exam.id}" in embed.footer.text
    assert "pass ≥80%" in embed.footer.text
    assert "1 attempt(s)" in embed.footer.text
    Exam.objects.filter(pk=exam.id).update(closed=True)
    exam.closed = True
    embed = build_exam_embed(exam)
    assert embed.title.startswith("[CLOSED] ")


@pytest.mark.django_db
def test_results_embed_lists_takers():
    exam = Exam.objects.create(
        title="Academy Exam",
        questions=json.loads(VALID_EXAM)["questions"],
        created_by_discord_id="1",
    )
    ExamAttempt.objects.create(
        exam=exam, discord_user_id="10", discord_username="Passer",
        answers=[], correct=3, graded=3, score=100, passed=True,
    )
    ExamAttempt.objects.create(
        exam=exam, discord_user_id="11", discord_username="Failer",
        answers=[], correct=1, graded=3, score=33, passed=False,
    )
    embed = build_results_embed(exam)
    assert "2 taker(s)" in embed.description
    assert "1 passed" in embed.description
    lines = embed.description.splitlines()
    assert lines[2].startswith("✅ Passer")
    assert lines[3].startswith("❌ Failer")


@pytest.mark.django_db
def test_results_embed_empty():
    exam = Exam.objects.create(
        title="Academy Exam",
        questions=json.loads(VALID_EXAM)["questions"],
        created_by_discord_id="1",
    )
    embed = build_results_embed(exam)
    assert "0 taker(s)" in embed.description
    assert "No attempts yet" in embed.description


@pytest.mark.django_db
def test_results_csv_rows_and_columns():
    exam = Exam.objects.create(
        title="Academy Exam",
        questions=json.loads(VALID_EXAM)["questions"],
        created_by_discord_id="1",
    )
    ExamAttempt.objects.create(
        exam=exam, discord_user_id="10", discord_username="Passer",
        answers=["The other driver", ["Notify dispatch", "Two units"], "High", "hi"],
        correct=3, graded=3, score=100, passed=True,
    )
    ExamAttempt.objects.create(
        exam=exam, discord_user_id="10", discord_username="Passer",
        answers=[], correct=0, graded=3, score=0, passed=False,
    )
    buf = build_results_csv(exam)
    rows = list(csv.reader(io.StringIO(buf.getvalue())))
    header, attempt1, attempt2 = rows[0], rows[1], rows[2]
    assert header[4:7] == ["submitted_at", "score", "passed"]
    assert header[-1] == "Q4: Q4 Anything to add?"
    assert attempt1[3] == "1" and attempt1[6] == "True"
    assert attempt2[3] == "2" and attempt2[6] == "False"
    assert attempt1[-1] == "hi"


# ------------------------------------------------------------- grade+save
# grade_and_save_db is the synchronous DB body (production wraps it in
# asyncio.to_thread); tests call it directly under plain django_db so the ORM
# stays on pytest-django's main thread. The async glue gets its own test.


@pytest.mark.django_db
def test_first_attempt_passes():
    exam = Exam.objects.create(
        title="Academy Exam",
        questions=json.loads(VALID_EXAM)["questions"],
        created_by_discord_id="1",
    )
    selections = {
        0: "The other driver",
        1: ["Notify dispatch", "Two units"],
        2: "High",
        3: "",
    }
    message, ok, attempt_no, score = grade_and_save_db(
        exam.id, "42", "Tester", dict(selections)
    )
    assert ok and attempt_no == 1 and score == 100
    assert "Passed" in message
    attempt = ExamAttempt.objects.get(exam=exam, discord_user_id="42")
    assert (attempt.correct, attempt.graded) == (3, 3)
    assert (attempt.score, attempt.passed) == (100, True)
    assert len(attempt.answers) == 4


@pytest.mark.django_db
def test_failed_attempt_then_retry():
    exam = Exam.objects.create(
        title="Academy Exam",
        questions=json.loads(VALID_EXAM)["questions"],
        max_attempts=2,
        created_by_discord_id="1",
    )
    message, ok, attempt_no, score = grade_and_save_db(
        exam.id, "42", "Tester", {0: "The officer", 2: "Low"}
    )
    assert ok and attempt_no == 1 and score == 0
    assert "Failed" in message
    full_correct = {0: "The other driver", 1: ["Notify dispatch", "Two units"], 2: "High"}
    message, ok, attempt_no, score = grade_and_save_db(
        exam.id, "42", "Tester", dict(full_correct)
    )
    assert ok and attempt_no == 2 and score == 100
    attempts = ExamAttempt.objects.filter(exam=exam).order_by("created_at")
    assert attempts.count() == 2
    assert attempts[0].passed is False and attempts[0].score == 0
    assert attempts[1].passed is True


@pytest.mark.django_db
def test_attempts_exhausted_blocks():
    exam = Exam.objects.create(
        title="Academy Exam",
        questions=json.loads(VALID_EXAM)["questions"],
        max_attempts=1,
        created_by_discord_id="1",
    )
    message, ok, attempt_no, _score = grade_and_save_db(
        exam.id, "42", "Tester", {0: "The officer"}
    )
    assert ok and attempt_no == 1
    message, ok, attempt_no, _score = grade_and_save_db(
        exam.id, "42", "Tester", {0: "The other driver"}
    )
    assert not ok and attempt_no == 1
    assert "No attempts left" in message
    assert ExamAttempt.objects.filter(exam=exam).count() == 1


@pytest.mark.django_db
def test_already_passed_blocks():
    exam = Exam.objects.create(
        title="Academy Exam",
        questions=json.loads(VALID_EXAM)["questions"],
        max_attempts=2,
        created_by_discord_id="1",
    )
    full_correct = {0: "The other driver", 1: ["Notify dispatch", "Two units"], 2: "High"}
    message, ok, _attempt_no, _score = grade_and_save_db(
        exam.id, "42", "Tester", dict(full_correct)
    )
    assert ok and "Passed" in message
    message, ok, attempt_no, _score = grade_and_save_db(
        exam.id, "42", "Tester", {0: "The other driver"}
    )
    assert not ok and attempt_no == 1
    assert "already passed" in message
    assert ExamAttempt.objects.filter(exam=exam).count() == 1


@pytest.mark.django_db
def test_closed_exam_blocks():
    exam = Exam.objects.create(
        title="Academy Exam",
        questions=json.loads(VALID_EXAM)["questions"],
        closed=True,
        created_by_discord_id="1",
    )
    message, ok, attempt_no, _score = grade_and_save_db(
        exam.id, "42", "Tester", {0: "The other driver"}
    )
    assert not ok and attempt_no == 0
    assert "closed" in message
    assert ExamAttempt.objects.filter(exam=exam).count() == 0


def test_grade_and_save_async_glue(monkeypatch):
    """_grade_and_save forwards (exam_id, str(user.id), display_name, selections)."""
    import amc_cogs.exam as exam_module

    captured = {}

    def fake_to_thread(fn, *args):
        captured["fn"] = fn
        captured["args"] = args

        async def _run():
            return ("✅ **Passed** — 3/3 (100%).", True, 1, 100)

        return _run()

    monkeypatch.setattr(exam_module.asyncio, "to_thread", fake_to_thread)

    async def run():
        interaction = _interaction(user_id=42, name="Tester")
        await exam_module._grade_and_save(interaction, 7, {0: "The other driver"})
        return interaction

    interaction = asyncio.run(run())
    assert captured["fn"] is exam_module.grade_and_save_db
    assert captured["args"] == (7, "42", "Tester", {0: "The other driver"})
    interaction.response.send_message.assert_awaited_once()


# -------------------------------------------------------------------- cog


def test_cog_group_has_six_commands():
    ExamCog(SimpleNamespace())  # cog init only stores the bot handle
    assert ExamCog.exam_group.name == "exam"
    commands = {c.name for c in ExamCog.exam_group.walk_commands()}
    assert commands == {"create", "schema", "results", "export", "close", "repost"}
