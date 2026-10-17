"""Terrain height lookup from the Motor Town Jeju_World heightmap.

Source data is the full-resolution heightmap dump that also feeds the
amc-web 3D map (see mt-map-extract's ``Jeju_World.json``):

- 11000 x 11000 unsigned 16-bit LE samples, row-major, one value per
  world quad (200 cm x 200 cm).
- World mapping (game units are cm):
      col = (X - (-1_280_000)) / 200
      row = (Y - (-320_000))   / 200
      Z_cm = (raw - 32768) / 128 * 100

Two file formats are supported, picked by sniffing the first bytes:

- Raw uint16 (242 MB): mmap'd, O(1) sampling.
- ``JHM1`` (30 MB): the same data split into 128x128-sample blocks,
  delta-coded and lzma-compressed, with an in-memory block index and a
  small LRU of decoded blocks.  Lossless and fully random-access; a
  lookup on an uncached block costs ~0.4 ms, cached lookups are ~1 us.
  Convert once, offline, with :func:`write_jhm`.

The mmap/index is opened lazily on first lookup and kept for the
process lifetime.  Deploy the heightmap file to the host and point
``HEIGHTMAP_PATH`` at it (defaults to
/var/lib/amc-backend/jeju_heights_11000.jhm).  All functions return
None when the file is missing or the coordinate falls outside the
mapped area, so callers can fall back to their previous behaviour.
"""

from __future__ import annotations

import logging
import lzma
import mmap
import os
import struct
import zipfile
from functools import lru_cache
from itertools import accumulate
from typing import Optional

logger = logging.getLogger("amc.heightmap")

# Jeju_World layout constants (cm).
ORIGIN_X_CM = -1_280_000.0
ORIGIN_Y_CM = -320_000.0
QUAD_CM = 200.0
WIDTH = 11_000
HEIGHT = 11_000

DEFAULT_HEIGHTMAP_PATH = "/var/lib/amc-backend/jeju_heights_11000.jhm"

_JHM_MAGIC = b"JHM1"
_JHM_HEADER = struct.Struct("<4sHHHIQ")  # magic, w, h, block, n_blocks, index_offset


class Heightmap:
    """Bilinear terrain sampler over a raw uint16 or JHM1 heightmap file."""

    def __init__(self, path: str, width: int = WIDTH, height: int = HEIGHT):
        self.path = path
        self._f = open(path, "rb")
        magic = self._f.read(4)
        self._f.seek(0)
        if magic == _JHM_MAGIC:
            head = _JHM_HEADER.unpack(self._f.read(_JHM_HEADER.size))
            _, w, h, block, count, index_offset = head
            self.width, self.height, self._block = w, h, block
            self._fd = self._f.fileno()
            blob = os.pread(self._fd, count * 12, index_offset)
            self._index = list(struct.iter_unpack("<QI", blob))
            self._nb = (w + block - 1) // block
            self._get_block = lru_cache(maxsize=64)(self._load_block)
        else:
            self.width, self.height = width, height
            size = os.fstat(self._f.fileno()).st_size
            expected = width * height * 2
            if size != expected:
                self._f.close()
                raise ValueError(
                    f"heightmap {path} is {size} bytes, expected {expected}"
                )
            self._mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
            self._unpack = struct.Struct("<H").unpack_from

    def close(self) -> None:
        self._f.close()

    @staticmethod
    def raw_to_z_cm(raw: float) -> float:
        return (raw - 32768.0) / 128.0 * 100.0

    def _load_block(self, br: int, bc: int) -> list:
        off, clen = self._index[br * self._nb + bc]
        payload = os.pread(self._fd, clen, off)
        n = self._block * self._block
        deltas = struct.unpack(f"<{n}H", lzma.decompress(payload))
        # Deltas are (v[i] - v[i-1]) mod 65536, first sample absolute, so
        # prefix sums recover the values (masked back into uint16 range).
        return list(accumulate(deltas))

    def _raw_at(self, row: int, col: int) -> int:
        if hasattr(self, "_index"):
            block = self._get_block(row // self._block, col // self._block)
            return block[(row % self._block) * self._block + (col % self._block)] & 0xFFFF
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


def write_jhm(src_path: str, dst_path: str, width: int = WIDTH, height: int = HEIGHT,
              block: int = 128) -> None:
    """Convert a raw uint16 heightmap to the compressed JHM1 format.

    One-time offline tool (the full map takes ~3 minutes in pure
    Python).  Layout: header, block index, then lzma-compressed blocks;
    each block is row-major uint16 samples, delta-coded sequentially
    (first sample absolute, the rest mod 65536).  Edge blocks are
    zero-padded to block x block; z_cm() never samples the padding.
    """
    nb = (width + block - 1) // block
    count = nb * nb
    index_offset = _JHM_HEADER.size
    fd = os.open(src_path, os.O_RDONLY)

    def read_block(br: int, bc: int) -> list:
        r0, c0 = br * block, bc * block
        grid = [0] * (block * block)
        for i, r in enumerate(range(r0, min(r0 + block, height))):
            off = (r * width + c0) * 2
            n = min(block, width - c0) * 2
            vals = struct.unpack(f"<{n // 2}H", os.pread(fd, n, off))
            grid[i * block: i * block + len(vals)] = vals
        return grid

    def delta_encode(samples: list) -> bytes:
        out = [samples[0]]
        prev = samples[0]
        for v in samples[1:]:
            out.append((v - prev) & 0xFFFF)
            prev = v
        return struct.pack(f"<{len(out)}H", *out)

    entries: list[tuple[int, int]] = []
    payloads: list[bytes] = []
    pos = index_offset + count * 12
    for br in range(nb):
        for bc in range(nb):
            payload = lzma.compress(delta_encode(read_block(br, bc)), preset=6)
            entries.append((pos, len(payload)))
            payloads.append(payload)
            pos += len(payload)

    with open(dst_path, "wb") as f:
        f.write(_JHM_HEADER.pack(_JHM_MAGIC, width, height, block, count, index_offset))
        f.write(b"".join(struct.pack("<QI", off, ln) for off, ln in entries))
        for payload in payloads:
            f.write(payload)
    os.close(fd)


_instance: Optional[Heightmap] = None
_state: dict = {"key": None}


def _latest_heightmap_file(directory: str) -> Optional[str]:
    """Newest heightmap-looking file directly inside `directory` (drop-in updates)."""
    best: Optional[tuple[float, str]] = None
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        if not name.endswith((".jhm", ".bin", ".bin.gz", ".jhm.gz", ".zip")):
            continue
        mtime = os.path.getmtime(path)
        if best is None or mtime > best[0]:
            best = (mtime, path)
    return best[1] if best else None


def _extract_heightmap(source: str) -> str:
    """Decompress a gzipped/zipped heightmap into a cache dir; return the openable file.

    The extracted file is keyed by the source's mtime, so replacing the source (e.g.
    dropping a new heightmap zip into the amc-web map_tiles directory) invalidates the
    cache automatically.  A zip must contain exactly one non-directory member.
    """
    from django.conf import settings

    cache_dir = getattr(settings, "HEIGHTMAP_CACHE_DIR", None)
    if not cache_dir:
        cache_dir = os.path.join(os.path.dirname(source), ".heightmap-cache")
    os.makedirs(cache_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(source))[0]
    key = f"{stem}-{int(os.path.getmtime(source))}"
    if source.endswith(".gz"):
        import gzip

        inner = stem[: -len(".bin")] if stem.endswith(".bin") else stem
        out = os.path.join(cache_dir, f"{key}-{inner}")
        if not os.path.exists(out):
            tmp = f"{out}.tmp"
            with gzip.open(source, "rb") as src, open(tmp, "wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
            os.replace(tmp, out)
        return out
    if source.endswith(".zip"):
        with zipfile.ZipFile(source) as zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            if len(members) != 1:
                raise ValueError(f"heightmap zip {source} must contain exactly one file")
            member = members[0]
            out = os.path.join(cache_dir, f"{key}-{os.path.basename(member.filename)}")
            if not os.path.exists(out):
                tmp = f"{out}.tmp"
                with zf.open(member) as src, open(tmp, "wb") as dst:
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
                os.replace(tmp, out)
        return out
    return source


def _resolve_heightmap_path(path: str) -> Optional[str]:
    """Resolve a configured heightmap location to an openable file, or None.

    `path` may be a heightmap file (raw uint16, JHM1, or either one gzipped/zipped)
    or a directory to scan for the newest one — dropping a new zip into the directory
    (e.g. amc-web's static/map_tiles/<map>/) is enough to update the map data.
    """
    if os.path.isdir(path):
        path = _latest_heightmap_file(path)
        if path is None:
            return None
    try:
        return _extract_heightmap(path)
    except (OSError, ValueError, zipfile.BadZipFile):
        logger.exception("heightmap source %s could not be used", path)
        return None


def _source_key(path: str) -> Optional[tuple]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def get_heightmap() -> Optional[Heightmap]:
    """Return the process-wide heightmap, or None if unavailable.

    The configured source is re-checked (cheap stat) on every call and the sampler is
    rebuilt when the file changes, so heightmaps can be swapped without a restart.
    """
    global _instance
    from django.conf import settings

    path = getattr(settings, "HEIGHTMAP_PATH", DEFAULT_HEIGHTMAP_PATH)
    resolved = _resolve_heightmap_path(path)
    if resolved is None:
        return None
    key = (resolved, _source_key(resolved))
    if _instance is not None and _state["key"] == key:
        return _instance
    if _instance is not None:
        try:
            _instance.close()
        except OSError:
            logger.exception("heightmap %s could not be closed", _instance.path)
        _instance = None
    # Tests plug in synthetic maps via HEIGHTMAP_WIDTH/HEIGHT settings; production
    # leaves them unset and gets the real 11000x11000 layout.
    width = getattr(settings, "HEIGHTMAP_WIDTH", None)
    height = getattr(settings, "HEIGHTMAP_HEIGHT", None)
    try:
        _instance = (
            Heightmap(resolved)
            if width is None or height is None
            else Heightmap(resolved, width=width, height=height)
        )
        _state["key"] = key
    except FileNotFoundError:
        logger.warning("heightmap not found at %s — Z lookups disabled", resolved)
    except OSError:
        logger.exception("heightmap %s could not be opened", resolved)
    except ValueError:
        logger.exception("heightmap %s has an unexpected size", resolved)
    return _instance


def terrain_z_cm(x: float, y: float) -> Optional[float]:
    """Terrain Z in game cm at world X/Y, or None if unavailable/out of bounds."""
    hm = get_heightmap()
    if hm is None:
        return None
    return hm.z_cm(x, y)
