"""print_results lap columns: best (BL) + last (LL) lap in the results popup.

Legacy behavior must be preserved byte-for-byte when no participant recorded
a lap (sprints / time trials) — the lap columns only appear for multi-lap
events where at least one participant has lap data.
"""

from types import SimpleNamespace

from amc.events import (
    create_event_embed,
    participant_lap_segment,
    print_results,
)


def participant(
    name="Player",
    net_time: float | None = 651.4,
    finished=True,
    best_lap_time: float | None = 0.0,
    lap_times: list[float] | None = None,
    laps=1,
    section_index=-1,
):
    return SimpleNamespace(
        character=SimpleNamespace(name=name),
        net_time=net_time,
        finished=finished,
        wrong_engine=False,
        wrong_vehicle=False,
        best_lap_time=best_lap_time,
        lap_times=lap_times if lap_times is not None else [],
        laps=laps,
        section_index=section_index,
    )


def test_no_laps_keeps_legacy_format():
    """No participant has lap data -> no BL/LL columns, legacy layout only."""
    participants = [
        participant(name="Alpha"),
        participant(name="Beta", net_time=None, finished=False),
    ]
    out = print_results(participants)
    lines = out.split("\n")

    assert "BL" not in out
    assert "LL" not in out
    assert lines[0].startswith("#01: <Bold>Alpha           </> 10:51.400")
    assert lines[1].startswith("#02: <Bold>Beta            </> -")
    assert "<Warning>DNF</>" in lines[1]


def test_lap_event_shows_best_and_last_lap():
    """Best lap = min lap recorded, last lap = final entry of lap_times."""
    fast = participant(
        name="Fast",
        best_lap_time=96.2,
        lap_times=[96.2, 103.9],  # best was lap 1, last lap was slower
    )
    slow = participant(name="Slow", net_time=None, finished=False)
    out = print_results([fast, slow])
    lines = out.split("\n")

    assert "BL 01:36.200" in lines[0]
    assert "LL 01:43.900" in lines[0]
    # Participant without laps renders dashes in both lap columns.
    assert "BL -" in lines[1]
    assert "LL -" in lines[1]
    # Legacy rank/name/time prefix is untouched.
    assert lines[0].startswith("#01: <Bold>Fast            </> 10:51.400")
    assert lines[1].startswith("#02: <Bold>Slow            </> -")


def test_best_lap_only_in_lap_times():
    """best_lap_time=0 but lap_times recorded -> lap columns appear from the array."""
    p = participant(best_lap_time=0.0, lap_times=[95.0])
    out = print_results([p])

    assert "BL -" in out
    assert "LL 01:35.000" in out


def test_none_and_empty_lap_data_are_safe():
    """None best_lap_time / None lap_times must not crash — treated as a
    no-lap event, so the legacy (no lap columns) format applies."""
    p = participant(best_lap_time=None, lap_times=None)
    out = print_results([p])

    assert "BL" not in out
    assert "LL" not in out


def test_long_best_lap_alignment():
    """A 10-minute lap (9-char time) still renders without truncation."""
    p = participant(best_lap_time=620.5, lap_times=[620.5])
    out = print_results([p])

    assert "BL 10:20.500" in out
    assert "LL 10:20.500" in out


def test_lap_segment_with_laps():
    """Discord suffix carries both best and last lap."""
    p = participant(best_lap_time=96.2, lap_times=[96.2, 103.9])
    assert participant_lap_segment(p) == " BL 01:36.200 LL 01:43.900"


def test_lap_segment_empty_without_laps():
    """No lap data -> empty suffix, so sprint/TT lines stay byte-identical."""
    assert participant_lap_segment(participant()) == ""
    assert participant_lap_segment(participant(best_lap_time=None, lap_times=None)) == ""


def test_lap_segment_best_only_in_lap_times():
    p = participant(best_lap_time=0.0, lap_times=[95.0])
    assert participant_lap_segment(p) == " BL - LL 01:35.000"


def _embed_participants_field(participants):
    game_event = SimpleNamespace(
        name="Qualifying",
        state=1,
        race_setup=SimpleNamespace(
            hash="abc123",
            num_laps=2,
            num_sections=10,
            vehicles=[],
            engines=[],
        ),
        scheduled_event=None,
        participants=SimpleNamespace(all=lambda: participants),
    )
    embed = create_event_embed(game_event)
    field = next(f for f in embed.fields if f.name == "👥 Participants")
    assert field.value is not None
    return field.value


def test_event_embed_shows_laps():
    """Discord event embed participant lines carry BL/LL for lap events."""
    value = _embed_participants_field(
        [
            participant(name="Fast", best_lap_time=96.2, lap_times=[96.2, 103.9]),
            participant(name="Slow", net_time=None, finished=False),
        ]
    )
    fast_line = next(line for line in value.split("\n") if "Fast" in line)
    slow_line = next(line for line in value.split("\n") if "Slow" in line)

    assert "BL 01:36.200" in fast_line
    assert "LL 01:43.900" in fast_line
    assert "BL" not in slow_line
    assert "LL" not in slow_line
