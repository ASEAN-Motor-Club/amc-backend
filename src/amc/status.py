import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import psutil  # type: ignore[import-untyped]
from django.core.cache import cache
from amc.mod_server import get_status, set_config
from amc.game_server import get_players, announce
from amc.models import ServerStatus
from amc.utils import skip_if_running
from amc.pinned_announcement import announce_server_restart

logger = logging.getLogger(__name__)

FD_MONITOR_LOG = Path("/var/log/motortown-fd-monitor.log")
FD_SETSIZE = 1024  # glibc hard limit for select()
FD_WARN_THRESHOLD = 850  # warn when max FD number exceeds this

# Tracks whether the game server was reachable on the previous status tick, so
# we can detect the down→up transition (crash/restart recovery). Defaults to
# True so the backend doesn't announce a restart on its own first observation
# while the server is already up.
_WAS_UP_KEY = "server_status:was_up"

# Tracks the last FD alert level to avoid spamming:
#   None  = never alerted
#   "warn" = warned at FD_WARN_THRESHOLD
#   "crit" = warned at 950+
_last_fd_alert_level: str | None = None


@dataclass
class FDSnapshot:
    total_fds: int
    max_fd_num: int
    wineserver_fds: int | None = None
    wineserver_max: int | None = None
    gamethread_fds: int | None = None
    gamethread_max: int | None = None


def parse_fd_monitor_log() -> FDSnapshot | None:
    """Parse the last line of the FD monitor log written by /tmp/fd-monitor.sh.

    Log format:
        2026-05-02 19:48:44 | total_fds=827 max_fd_num=545 | wineserver(pid=2138206):fds=508,high=545,... GameThread(pid=2138281):fds=319,high=331,...
    """
    if not FD_MONITOR_LOG.exists():
        return None

    try:
        # Read only the last line (the file is appended to every 60s)
        with open(FD_MONITOR_LOG, "rb") as f:
            try:
                f.seek(-4096, 2)  # seek to ~end of file
            except OSError:
                f.seek(0)
            last_line = f.read().splitlines()[-1].decode("utf-8", errors="replace")
    except (OSError, IndexError):
        return None

    # Parse total_fds and max_fd_num from the summary segment
    summary_match = re.search(r"total_fds=(\d+)\s+max_fd_num=(\d+)", last_line)
    if not summary_match:
        return None

    total_fds = int(summary_match.group(1))
    max_fd_num = int(summary_match.group(2))

    snapshot = FDSnapshot(total_fds=total_fds, max_fd_num=max_fd_num)

    # Parse per-process breakdown
    ws_match = re.search(
        r"wineserver\(pid=\d+\):fds=(\d+),high=(\d+)", last_line
    )
    if ws_match:
        snapshot.wineserver_fds = int(ws_match.group(1))
        snapshot.wineserver_max = int(ws_match.group(2))

    gt_match = re.search(
        r"GameThread\(pid=\d+\):fds=(\d+),high=(\d+)", last_line
    )
    if gt_match:
        snapshot.gamethread_fds = int(gt_match.group(1))
        snapshot.gamethread_max = int(gt_match.group(2))

    return snapshot


async def _alert_fd_usage(snapshot: FDSnapshot, http_client) -> None:
    """Send a Discord alert if FD usage is approaching FD_SETSIZE."""
    global _last_fd_alert_level

    if snapshot.max_fd_num < FD_WARN_THRESHOLD:
        if _last_fd_alert_level is not None:
            # FDs dropped back below threshold — reset
            _last_fd_alert_level = None
            logger.info("FD usage back to normal (max_fd=%d)", snapshot.max_fd_num)
        return

    # Determine severity
    if snapshot.max_fd_num >= 950:
        level = "crit"
        color = "FF0000"
        prefix = "FD CRITICAL"
    else:
        level = "warn"
        color = "FFA500"
        prefix = "FD WARNING"

    # Only alert once per severity escalation (don't spam every 60s)
    if _last_fd_alert_level == level:
        return

    _last_fd_alert_level = level

    headroom = FD_SETSIZE - snapshot.max_fd_num
    msg = (
        f"{prefix}: Game server max FD number is {snapshot.max_fd_num}/{FD_SETSIZE} "
        f"({headroom} headroom). Total FDs: {snapshot.total_fds}."
    )
    if snapshot.wineserver_fds and snapshot.gamethread_fds:
        msg += (
            f" wineserver={snapshot.wineserver_fds} fds (max#{snapshot.wineserver_max})"
            f" GameThread={snapshot.gamethread_fds} fds (max#{snapshot.gamethread_max})."
        )
    msg += " The server will crash with 'bit out of range FD_SETSIZE' if max fd exceeds 1024."

    logger.warning(msg)
    try:
        await announce(msg, http_client, color=color)
    except Exception as e:
        logger.error("Failed to send FD alert: %s", e)


@skip_if_running
async def monitor_server_status(ctx):
    status = await get_status(ctx["http_client_mod"])
    try:
        players = await get_players(ctx["http_client"])
    except Exception as e:
        print(f"Failed to get players: {e}")
        players = []

    if status is None:
        status = {}

    # Detect a crash/restart: the mod is unreachable when the game is down.
    up = bool(status)
    was_up = cache.get(_WAS_UP_KEY, True)
    cache.set(_WAS_UP_KEY, up)
    if up and not was_up:
        # Server just came back — announce the restart via the /ap pin, in the
        # background and without awaiting a response from the endpoint.
        asyncio.create_task(announce_server_restart(ctx))

    mem = psutil.virtual_memory()

    # Parse FD monitor log
    fd = parse_fd_monitor_log()

    await ServerStatus.objects.acreate(
        fps=status.get("FPS", 0),
        used_memory=mem.used,
        num_players=len(players) if players is not None else 0,
        fd_total=fd.total_fds if fd else None,
        fd_max_num=fd.max_fd_num if fd else None,
    )

    # Alert on Discord if FDs are approaching the limit
    if fd:
        await _alert_fd_usage(fd, ctx["http_client"])


async def monitor_server_condition(ctx):
    status = await get_status(ctx["http_client_mod"])
    try:
        players = await get_players(ctx["http_client"])
    except Exception as e:
        print(f"Failed to get players: {e}")
        players = []

    if status is None:
        status = {}
    fps = status.get("FPS", 0)
    num_players = len(players) if players is not None else 0
    base_vehicles_per_player = 12
    target_fps = 22
    if num_players == 0:
        max_vehicles_per_player = base_vehicles_per_player
    else:
        max_vehicles_per_player = (
            min(
                base_vehicles_per_player,
                max(
                    int(fps * base_vehicles_per_player * 20 / target_fps / num_players),
                    3,
                ),
            )
            - 1
        )

    await set_config(ctx["http_client_mod"], max_vehicles_per_player)
    if fps < target_fps:
        if max_vehicles_per_player < base_vehicles_per_player:
            await announce(
                f"Max vehicles per player is now {max_vehicles_per_player}.",
                ctx["http_client"],
                color="FF59EE",
            )


async def monitor_rp_mode(ctx):
    # NOTE: Autopilot detection (bIsAIDriving) requires per-player vehicle data
    # that is no longer available via the batch GET /players endpoint. The
    # per-player list_player_vehicles endpoint is disabled. This function is
    # a no-op until a batch vehicle endpoint is added to the mod server.
    pass
