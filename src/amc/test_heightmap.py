"""Unit tests for the heightmap sampler — pure stdlib, no DB required."""

import struct

import pytest

from amc.heightmap import (
    HEIGHT,
    ORIGIN_X_CM,
    ORIGIN_Y_CM,
    QUAD_CM,
    WIDTH,
    Heightmap,
)

# 3x3 synthetic map. Node (row, col) sits at world
#   X = ORIGIN_X_CM + col * QUAD_CM, Y = ORIGIN_Y_CM + row * QUAD_CM.
RAWS = [
    [32768, 33792, 34816],  # Z cm: 0, 800, 1600
    [31744, 32768, 33792],  # Z cm: -800, 0, 800
    [30720, 31744, 32768],  # Z cm: -1600, -800, 0
]


@pytest.fixture
def heightmap(tmp_path):
    path = tmp_path / "heights.bin"
    with open(path, "wb") as f:
        for row in RAWS:
            for raw in row:
                f.write(struct.pack("<H", raw))
    hm = Heightmap(path, width=3, height=3)
    yield hm
    hm.close()


def world_xy(row, col):
    return ORIGIN_X_CM + col * QUAD_CM, ORIGIN_Y_CM + row * QUAD_CM


def test_raw_to_z_cm_formula():
    assert Heightmap.raw_to_z_cm(32768) == 0.0
    assert Heightmap.raw_to_z_cm(0) == -25600.0  # map minZ
    assert Heightmap.raw_to_z_cm(44020) == 8790.625  # map maxZ


def test_exact_node_samples(heightmap):
    for row in range(3):
        for col in range(3):
            x, y = world_xy(row, col)
            expected = Heightmap.raw_to_z_cm(RAWS[row][col])
            assert heightmap.z_cm(x, y) == pytest.approx(expected)


def test_bilinear_midpoint(heightmap):
    # Midpoint between nodes (1,1) and (1,2): average of the two Z values.
    x1, y1 = world_xy(1, 1)
    x2, _ = world_xy(1, 2)
    z1 = Heightmap.raw_to_z_cm(RAWS[1][1])
    z2 = Heightmap.raw_to_z_cm(RAWS[1][2])
    assert heightmap.z_cm((x1 + x2) / 2, y1) == pytest.approx((z1 + z2) / 2)


def test_bilinear_quad_center(heightmap):
    # Centre of the quad spanned by (0,0), (0,1), (1,0), (1,1): mean of all four.
    x0, y0 = world_xy(0, 0)
    x1, y1 = world_xy(1, 1)
    expected = sum(
        Heightmap.raw_to_z_cm(r)
        for r in (RAWS[0][0], RAWS[0][1], RAWS[1][0], RAWS[1][1])
    ) / 4
    assert heightmap.z_cm((x0 + x1) / 2, (y0 + y1) / 2) == pytest.approx(expected)


def test_out_of_bounds_is_none(heightmap):
    assert heightmap.z_cm(ORIGIN_X_CM - 1, ORIGIN_Y_CM) is None
    assert heightmap.z_cm(ORIGIN_X_CM, ORIGIN_Y_CM - 1) is None
    # Far edge of a 3x3 map is ORIGIN + 2 * QUAD_CM.
    assert heightmap.z_cm(ORIGIN_X_CM + 3 * QUAD_CM, ORIGIN_Y_CM) is None
    assert heightmap.z_cm(ORIGIN_X_CM, ORIGIN_Y_CM + 3 * QUAD_CM) is None


def test_far_edge_is_sampleable(heightmap):
    # The last node of the grid must be readable, not clipped away.
    x, y = world_xy(2, 2)
    assert heightmap.z_cm(x, y) == pytest.approx(Heightmap.raw_to_z_cm(RAWS[2][2]))


def test_wrong_size_rejected(tmp_path):
    path = tmp_path / "bad.bin"
    path.write_bytes(b"\x00" * 10)
    with pytest.raises(ValueError):
        Heightmap(path, width=3, height=3)


def test_real_dimensions_constant():
    assert (WIDTH, HEIGHT) == (11_000, 11_000)
