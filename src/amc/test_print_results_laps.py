"""print_results lap columns: best (BL) + last (LL) lap in the results popup.

Legacy behavior must be preserved byte-for-byte when no participant recorded
a lap (sprints / time trials) — the lap columns only appear for multi-lap
events where at least one participant has lap data.
"""

from types import SimpleNamespace

from amc.events import print_results


def participant(
    name="Player",
    net_time: float | None = 651.4,
    finished=True,
    best_lap_time: float | None = 0.0,
    lap_times: list[float] | None = None,
):
    return SimpleNamespace(
        character=SimpleNamespace(name=name),
        net_time=net_time,
        finished=finished,
        wrong_engine=False,
        wrong_vehicle=False,
        best_lap_time=best_lap_time,
        lap_times=lap_times if lap_times is not None else [],
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
