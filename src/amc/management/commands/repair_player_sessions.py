"""Rebuild play sessions from the authoritative server log.

``PlayerStatusLog`` ranges are written by the login/logout handlers. Whenever a
line was lost — a restart that killed the logout, a RELP outage, an exception
in the login handler — the range either stayed open forever or was stretched
by the next logout that happened to land on it. Play-time totals are the plain
sum of those ranges, so the damage shows up as thousands of hours on a profile.

This command ignores the existing ranges and re-derives them from
``ServerLog`` (which retains every login/logout/restart line since the log
listener started), pairing:

* ``login`` → opens a session,
* ``logout`` → closes it,
* ``login`` while a session is open → the previous session ended there,
* ``DedicatedServer is started`` on a host → closes that host's sessions.

A span longer than ``MAX_SESSION`` is not trusted (a login and a logout weeks
apart only prove the player *was* online at both ends). Such a span is cut back
to the character's last recorded in-game activity inside it, or to a
zero-length marker when there is none — never to an invented end.

Usage::

    manage.py repair_player_sessions                # dry run, full report
    manage.py repair_player_sessions --character Goji
    manage.py repair_player_sessions --apply        # destructive
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.models import Q
from psycopg.types.range import Range

from amc import sessions as session_integrity
from amc.models import Character, PlayerStatusLog, ServerLog
from amc.player_tags import strip_all_tags

LOGIN_RE = re.compile(r"^Player Login: (?P<name>.+) \((?P<pid>\d+)\)\s*$")
LOGOUT_RE = re.compile(r"^Player Logout: (?P<name>.+) \((?P<pid>\d+)\)\s*$")
RESTART_RE = re.compile(r"^DedicatedServer is started")

ORDER = {"restart": 0, "logout": 1, "login": 2}


def _fmt_hours(total: timedelta) -> str:
    return f"{total.total_seconds() / 3600:,.0f}h"


class Command(BaseCommand):
    help = "Rebuild PlayerStatusLog session ranges from ServerLog"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the rebuilt ranges (default is a dry run)",
        )
        parser.add_argument(
            "--character",
            action="append",
            default=[],
            help="Limit to a character name (repeatable)",
        )

    def handle(self, *args, **options):
        apply_changes: bool = options["apply"]
        names: list[str] = options["character"]

        character_ids = None
        if names:
            character_ids = set(
                Character.objects.filter(name__in=names).values_list("id", flat=True)
            )
            if not character_ids:
                self.stderr.write(f"No characters named {names}")
                return

        self.stdout.write("Scanning ServerLog for login/logout/restart lines…")
        events, unresolved = self._load_events(character_ids)

        self.stdout.write(
            f"  {len(events):,} events, {unresolved:,} unattributable login/logout lines"
        )

        ranges = session_integrity.build_session_ranges(events)
        ranges = self._resolve_oversized(ranges)
        restart_violations = self._enforce_restart_bounds(ranges, events)
        if restart_violations:
            self.stdout.write(
                self.style.WARNING(
                    f"  clamped {restart_violations:,} range(s) that spanned a "
                    f"restart of their own host (should be 0 — the pairing step "
                    f"already splits on restarts)"
                )
            )

        targets = {cid for cid, *_ in ranges}
        if character_ids is not None:
            targets &= character_ids

        before = self._existing_stats(targets)
        after = self._range_stats(ranges)

        self.stdout.write("")
        self.stdout.write(f"Characters to rebuild: {len(targets):,}")
        self.stdout.write(f"  rows   {before['rows']:,} → {len(ranges):,}")
        self.stdout.write(
            f"  hours  {_fmt_hours(before['total'])} → {_fmt_hours(after['total'])}"
            f"   (counting only sessions ≤ {session_integrity.MAX_SESSION})"
        )
        self.stdout.write(
            f"  implausible rows before: {before['oversized']:,} "
            f"({_fmt_hours(before['oversized_total'])})"
        )
        if after["oversized"]:
            self.stdout.write(
                self.style.WARNING(
                    f"  {after['oversized']:,} rebuilt spans are still longer than "
                    f"MAX_SESSION — inspect them before trusting the totals"
                )
            )

        top = sorted(
            (
                (
                    cid,
                    before["per_character"].get(cid, timedelta(0)),
                    after["per_character"].get(cid, timedelta(0)),
                )
                for cid in targets
            ),
            key=lambda row: row[1] - row[2],
            reverse=True,
        )[:10]
        if top:
            names_by_id = dict(
                Character.objects.filter(id__in=[cid for cid, *_ in top]).values_list(
                    "id", "name"
                )
            )
            self.stdout.write("")
            self.stdout.write("Largest corrections:")
            for cid, old, new in top:
                self.stdout.write(
                    f"  {names_by_id.get(cid, cid)}: {_fmt_hours(old)} → {_fmt_hours(new)}"
                )

        if not apply_changes:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING("Dry run — nothing written. Re-run with --apply.")
            )
            return

        deleted, created = self._apply(targets, ranges)
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(f"Deleted {deleted:,} rows, wrote {created:,} rows.")
        )
        self._verify(ranges)

    # -- loading -----------------------------------------------------------

    def _load_events(self, character_ids):
        events: list[session_integrity.SessionEvent] = []
        unresolved = 0

        # char_key = (player unique_id, cleaned name) -> character id
        characters = Character.objects.select_related("player").values_list(
            "id", "name", "player_id"
        )
        by_name_pid = {(name, pid): cid for cid, name, pid in characters}
        by_pid = defaultdict(list)
        for cid, name, pid in characters:
            by_pid[pid].append(cid)

        qs = (
            ServerLog.objects.filter(
                Q(text__startswith=session_integrity.LOGIN_PREFIX)
                | Q(text__startswith=session_integrity.LOGOUT_PREFIX)
                | Q(text__startswith=session_integrity.RESTART_PREFIX)
            )
            .order_by("timestamp")
            .values_list("timestamp", "text", "hostname")
        )

        for timestamp, text, hostname in qs.iterator(chunk_size=5000):
            host = hostname or ""
            if RESTART_RE.match(text):
                events.append(
                    session_integrity.SessionEvent(timestamp, "restart", None, host)
                )
                continue
            match = LOGIN_RE.match(text) or LOGOUT_RE.match(text)
            if not match:
                continue
            pid = int(match.group("pid"))
            name = strip_all_tags(match.group("name")).strip()
            cid = by_name_pid.get((name, pid))
            if cid is None:
                candidates = by_pid.get(pid) or []
                cid = candidates[0] if len(candidates) == 1 else None
            if cid is None:
                unresolved += 1
                continue
            if character_ids is not None and cid not in character_ids:
                continue
            kind = "login" if text.startswith("Player Login: ") else "logout"
            events.append(session_integrity.SessionEvent(timestamp, kind, cid, host))

        return events, unresolved

    # -- post-processing ---------------------------------------------------

    def _resolve_oversized(self, ranges):
        """Never let rebuilding invent a multi-week session."""
        resolved = []
        for cid, start, end, host in ranges:
            if end is not None and end - start > session_integrity.MAX_SESSION:
                # The player was demonstrably in the world at the last
                # recorded activity inside the span; keep that, or nothing.
                from asgiref.sync import async_to_sync

                evidence = async_to_sync(session_integrity.activity_boundary)(
                    cid, start, end
                )
                end = evidence if evidence and evidence > start else start
            resolved.append((cid, start, end, host))
        return resolved

    def _enforce_restart_bounds(self, ranges, events):
        """No rebuilt range may span a restart of its own host.

        The pairing step already closes a session at the restart that
        interrupted it, so this is a self-check: a non-zero count means the
        builder and this clamp disagree and the spans are untrustworthy.
        Ranges with no host (legacy) are clamped by the earliest restart of
        any host — that can only shorten them, never lengthen.
        """
        restarts: dict[str, list] = defaultdict(list)
        for event in events:
            if event.kind == "restart":
                restarts[event.host].append(event.timestamp)
        for host in restarts:
            restarts[host].sort()

        clamped = 0
        for index, (cid, start, end, host) in enumerate(ranges):
            if end is None:
                continue
            if host:
                candidates = restarts.get(host, [])
            else:
                candidates = sorted(
                    ts for timestamps in restarts.values() for ts in timestamps
                )
            if not candidates:
                continue
            position = bisect_right(candidates, start)
            if position < len(candidates) and candidates[position] < end:
                ranges[index] = (cid, start, candidates[position], host)
                clamped += 1
        return clamped

    def _existing_stats(self, targets):
        qs = PlayerStatusLog.objects.filter(character_id__in=targets)
        total = timedelta(0)
        oversized = 0
        oversized_total = timedelta(0)
        per_character = defaultdict(timedelta)
        rows = 0
        for cid, duration in qs.values_list("character_id", "duration").iterator():
            rows += 1
            if duration is None:
                continue
            per_character[cid] += duration
            total += duration
            if duration > session_integrity.MAX_SESSION:
                oversized += 1
                oversized_total += duration
        return {
            "rows": rows,
            "total": total,
            "oversized": oversized,
            "oversized_total": oversized_total,
            "per_character": per_character,
        }

    @staticmethod
    def _range_stats(ranges):
        total = timedelta(0)
        oversized = 0
        per_character = defaultdict(timedelta)
        for cid, start, end, _host in ranges:
            if end is None:
                continue
            span = end - start
            if span <= timedelta(0):
                continue
            per_character[cid] += span
            if span <= session_integrity.MAX_SESSION:
                total += span
            else:
                oversized += 1
        return {
            "total": total,
            "oversized": oversized,
            "per_character": per_character,
        }

    # -- writing -----------------------------------------------------------

    def _apply(self, targets, ranges):
        to_create = [
            PlayerStatusLog(
                character_id=cid,
                host=host or "",
                timespan=Range(start, end, bounds="[)"),
            )
            for cid, start, end, host in ranges
        ]
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '600s'")
            deleted, _ = PlayerStatusLog.objects.filter(
                character_id__in=targets
            ).delete()
            for batch_start in range(0, len(to_create), 1000):
                PlayerStatusLog.objects.bulk_create(
                    to_create[batch_start : batch_start + 1000]
                )
        return deleted, len(to_create)

    def _verify(self, ranges):
        ids = {cid for cid, *_ in ranges}
        total = timedelta(0)
        for duration in PlayerStatusLog.objects.filter(
            character_id__in=ids
        ).values_list("duration", flat=True):
            if duration is not None:
                total += duration
        self.stdout.write(
            f"Verification: {len(ranges):,} rows, {_fmt_hours(total)} total recorded."
        )
