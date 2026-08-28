"""Terrain height lookup from the Motor Town Jeju_World heightmap.

Source data is the full-resolution heightmap dump that also feeds the
amc-web 3D map (see mt-map-extract's ``Jeju_World.json``):

- 11000 x 11000 unsigned 16-bit LE samples, row-major, one value per
  world quad (200 cm x 200 cm).
- World mapping (game units are cm):
      col = (X - (-1_280_000)) / 200
      row = (Y - (-320_000))   / 200
      Z_cm = (raw - 32768) / 128 * 100

The mmap is opened lazily on first lookup and kept for the process
lifetime; sampling is O(1) via file offsets, so no RAM is required
beyond the OS page cache.

Deploy the heightmap file to the host and point ``HEIGHTMAP_PATH`` at
it (defaults to /var/lib/amc-backend/jeju_heights_11000.bin).  All
functions return None when the file is missing or the coordinate falls
outside the mapped area, so callers can fall back to their previous
behaviour.
"""

from __future__ import annotations

import logging
import mmap
import os
import struct
from typing import Optional

logger = logging.getLogger("amc.heightmap")

# Jeju_World layout constants (cm).
ORIGIN_X_CM = -1_280_000.0
ORIGIN_Y_CM = -320_000.0
QUAD_CM = 200.0
WIDTH = 11_000
HEIGHT = 11_000

DEFAULT_HEIGHTMAP_PATH = "/var/lib/amc-backend/jeju_heights_11000.bin"


class Heightmap:
    """Bilinear terrain sampler over a raw uint16 heightmap file."""

    def __init__(self, path: str, width: int = WIDTH, height: int = HEIGHT):
        self.path = path
        self.width = width
        self.height = height
        self._f = open(path, "rb")
        size = os.fstat(self._f.fileno()).st_size
        expected = width * height * 2
        if size != expected:
            self._f.close()
            raise ValueError(f"heightmap {path} is {size} bytes, expected {expected}")
        self._mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        self._unpack = struct.Struct("<H").unpack_from

    def close(self) -> None:
        self._mm.close()
        self._f.close()

    @staticmethod
    def raw_to_z_cm(raw: float) -> float:
        return (raw - 32768.0) / 128.0 * 100.0

    def _raw_at(self, row: int, col: int) -> int:
        return self._unpack(self._mm, (row * self.width + col) * 2)[0]

    def z_cm(self, x: float, y: float) -> Optional[float]:
        """Terrain Z (cm) at world coordinates X/Y, or None out of bounds."""
        col_f = (x - ORIGIN_X_CM) / QUAD_CM
        row_f = (y - ORIGIN_Y_CM) / QUAD_CM
        if not (0.0 <= col_f <= self.width - 1 and 0.0 <= row_f <= self.height - 1):
            return None

        col = min(int(col_f), self.width - 2)
        row = min(int(row_f), self.height - 2)
        fx = col_f - col
        fy = row_f - row

        v = (
            self._raw_at(row, col) * (1.0 - fx) * (1.0 - fy)
            + self._raw_at(row, col + 1) * fx * (1.0 - fy)
            + self._raw_at(row + 1, col) * (1.0 - fx) * fy
            + self._raw_at(row + 1, col + 1) * fx * fy
        )
        return self.raw_to_z_cm(v)


_instance: Optional[Heightmap] = None


def get_heightmap() -> Optional[Heightmap]:
    """Return the process-wide heightmap, or None if unavailable."""
    global _instance
    if _instance is not None:
        return _instance

    from django.conf import settings

    path = getattr(settings, "HEIGHTMAP_PATH", DEFAULT_HEIGHTMAP_PATH)
    try:
        _instance = Heightmap(path)
    except FileNotFoundError:
        logger.warning("heightmap not found at %s — Z lookups disabled", path)
    except OSError:
        logger.exception("heightmap %s could not be opened", path)
    except ValueError:
        logger.exception("heightmap %s has an unexpected size", path)
    return _instance


def terrain_z_cm(x: float, y: float) -> Optional[float]:
    """Terrain Z in game cm at world X/Y, or None if unavailable/out of bounds."""
    hm = get_heightmap()
    if hm is None:
        return None
    return hm.z_cm(x, y)
