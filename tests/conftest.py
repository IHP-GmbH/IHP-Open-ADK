"""ADK test harness fixtures.

Synthesizes the minimal GDS layouts that exercise each ASM rule on demand,
so the suite does not depend on committed binary fixtures or any real PDK.
"""

import sys
from pathlib import Path

import klayout.db as kdb
import pytest

ADK_ROOT = Path(__file__).resolve().parents[1]
RUNNER_DIR = ADK_ROOT / "klayout" / "drc"
TEST_DIR = Path(__file__).resolve().parent
TEST_ADAPTER = TEST_DIR / "fixtures" / "test_interposer_adapter.drc"

# Layer numbers used by the synthetic fixtures. exchange0 must match
# config/layers.json; the attachment layer must match the test adapter.
EXCHANGE0_LAYER = (190, 0)
ATTACHMENT_LAYER = (999, 0)

# Make the runner importable from tests (PDK-agnostic helpers only).
sys.path.insert(0, str(RUNNER_DIR))


def _new_layout():
    ly = kdb.Layout()
    ly.dbu = 0.001  # 1 nm
    return ly


def _um(value):
    return int(round(value * 1000))


def _add_box(cell, layer_idx, x1, y1, x2, y2):
    cell.shapes(layer_idx).insert(
        kdb.Box(_um(x1), _um(y1), _um(x2), _um(y2))
    )


def _build_assembly_ok(out: Path):
    """Two 200x200 um chiplets, 100 um apart, each with an attachment pad
    fully inside the boundary. Expected: no violations."""
    ly = _new_layout()
    top = ly.create_cell("TOP")
    ex0 = ly.layer(*EXCHANGE0_LAYER)
    pad = ly.layer(*ATTACHMENT_LAYER)
    _add_box(top, ex0, 0, 0, 200, 200)
    _add_box(top, pad, 50, 50, 150, 150)
    _add_box(top, ex0, 300, 0, 500, 200)
    _add_box(top, pad, 350, 50, 450, 150)
    ly.write(str(out))


def _build_assembly_overlap(out: Path):
    """Two 200x200 um chiplets overlapping by 50 um. Expected to trigger
    ASM.a (the merged overlap region)."""
    ly = _new_layout()
    top = ly.create_cell("TOP")
    ex0 = ly.layer(*EXCHANGE0_LAYER)
    pad = ly.layer(*ATTACHMENT_LAYER)
    _add_box(top, ex0, 0, 0, 200, 200)
    _add_box(top, pad, 50, 50, 150, 150)
    _add_box(top, ex0, 150, 0, 350, 200)
    _add_box(top, pad, 200, 50, 300, 150)
    ly.write(str(out))


def _build_assembly_too_close(out: Path):
    """Two 200x200 um chiplets separated by 30 um (< ASM.b 50 um spacing).
    Expected: ASM.b only."""
    ly = _new_layout()
    top = ly.create_cell("TOP")
    ex0 = ly.layer(*EXCHANGE0_LAYER)
    pad = ly.layer(*ATTACHMENT_LAYER)
    _add_box(top, ex0, 0, 0, 200, 200)
    _add_box(top, pad, 50, 50, 150, 150)
    _add_box(top, ex0, 230, 0, 430, 200)
    _add_box(top, pad, 280, 50, 380, 150)
    ly.write(str(out))


def _build_assembly_too_small(out: Path):
    """One 50x50 um chiplet (2500 um^2 < ASM.e 10000 um^2) with an
    attachment pad fully inside. Expected: ASM.e only."""
    ly = _new_layout()
    top = ly.create_cell("TOP")
    ex0 = ly.layer(*EXCHANGE0_LAYER)
    pad = ly.layer(*ATTACHMENT_LAYER)
    _add_box(top, ex0, 0, 0, 50, 50)
    _add_box(top, pad, 10, 10, 40, 40)
    ly.write(str(out))


def _build_assembly_pad_outside(out: Path):
    """One 200x200 um chiplet with an attachment pad straddling the
    boundary. Expected: ASM.f only."""
    ly = _new_layout()
    top = ly.create_cell("TOP")
    ex0 = ly.layer(*EXCHANGE0_LAYER)
    pad = ly.layer(*ATTACHMENT_LAYER)
    _add_box(top, ex0, 0, 0, 200, 200)
    _add_box(top, pad, 150, 50, 250, 150)
    ly.write(str(out))


_BUILDERS = {
    "assembly_ok":           _build_assembly_ok,
    "assembly_overlap":      _build_assembly_overlap,
    "assembly_too_close":    _build_assembly_too_close,
    "assembly_too_small":    _build_assembly_too_small,
    "assembly_pad_outside":  _build_assembly_pad_outside,
}


@pytest.fixture(scope="session")
def fixture_layouts(tmp_path_factory) -> dict:
    """Build all assembly DRC fixtures once per session in a tmp dir."""
    out_dir = tmp_path_factory.mktemp("adk_fixtures")
    paths = {}
    for name, builder in _BUILDERS.items():
        target = out_dir / f"{name}.gds"
        builder(target)
        paths[name] = target
    return paths


@pytest.fixture(scope="session")
def test_adapter() -> Path:
    """Path to the synthetic interposer adapter."""
    assert TEST_ADAPTER.is_file(), f"Missing test adapter: {TEST_ADAPTER}"
    return TEST_ADAPTER
