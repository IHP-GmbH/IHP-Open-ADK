"""ADK test harness fixtures.

Synthesizes the minimal GDS layouts that exercise each ASM rule on demand,
plus the boundary-manifest sidecar each one is checked against, so the suite
does not depend on committed binary fixtures or any real PDK.
"""

import json
import sys
from pathlib import Path

import klayout.db as kdb
import pytest

ADK_ROOT = Path(__file__).resolve().parents[1]
RUNNER_DIR = ADK_ROOT / "klayout" / "drc"
TEST_DIR = Path(__file__).resolve().parent
TEST_ADAPTER = TEST_DIR / "fixtures" / "test_interposer_adapter.drc"

# Layer numbers used by the synthetic fixtures. exchange0 only models a
# pre-migration ("legacy") GDS; the manifest-native path ignores it. The
# attachment layer must match the test adapter.
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


def manifest_path_for(gds_path) -> Path:
    """The <stem>.boundaries.json sidecar the runner auto-discovers."""
    p = Path(gds_path)
    return p.with_name(p.stem + ".boundaries.json")


def _write_manifest(gds_path, boundary_boxes):
    """Write a boundary-manifest sidecar from a list of (x1,y1,x2,y2) um boxes.
    These are the authoritative chiplet boundaries the manifest-native DRC
    checks (independent of any GDS layer)."""
    boundaries = []
    for i, (x1, y1, x2, y2) in enumerate(boundary_boxes):
        boundaries.append({
            "instance": f"U{i + 1}",
            "source_die": "TEST",
            "class": "chiplet",
            "polygon_dbu": [
                [_um(x1), _um(y1)], [_um(x2), _um(y1)],
                [_um(x2), _um(y2)], [_um(x1), _um(y2)],
            ],
        })
    manifest = {
        "schema": "adk-boundary-manifest",
        "version": "1.0.0",
        "generator": "conftest",
        "assembly_gds": Path(gds_path).name,
        "dbu_um": 0.001,
        "top_cell": "TOP",
        "boundaries": boundaries,
    }
    manifest_path_for(gds_path).write_text(json.dumps(manifest, indent=2))


def _emit(out: Path, boundary_boxes, pad_boxes, manifest_boxes=None):
    """Write a fixture GDS (exchange0 boundaries + attachment pads) and its
    boundary-manifest sidecar.

    manifest_boxes defaults to boundary_boxes (manifest agrees with the GDS
    exchange0 geometry). Pass a different list to model a GDS whose exchange0
    geometry disagrees with the authoritative manifest -- used to prove the
    manifest-native deck ignores chiplet-internal/legacy exchange0 shapes.
    """
    ly = _new_layout()
    top = ly.create_cell("TOP")
    ex0 = ly.layer(*EXCHANGE0_LAYER)
    pad = ly.layer(*ATTACHMENT_LAYER)
    for box in boundary_boxes:
        _add_box(top, ex0, *box)
    for box in pad_boxes:
        _add_box(top, pad, *box)
    ly.write(str(out))
    _write_manifest(out, boundary_boxes if manifest_boxes is None else manifest_boxes)


def _build_assembly_ok(out: Path):
    """Two 200x200 um chiplets, 100 um apart, each with an attachment pad
    fully inside the boundary. Expected: no violations."""
    _emit(out,
          boundary_boxes=[(0, 0, 200, 200), (300, 0, 500, 200)],
          pad_boxes=[(50, 50, 150, 150), (350, 50, 450, 150)])


def _build_assembly_overlap(out: Path):
    """Two 200x200 um chiplets overlapping by 50 um. Expected: ASM.a."""
    _emit(out,
          boundary_boxes=[(0, 0, 200, 200), (150, 0, 350, 200)],
          pad_boxes=[(50, 50, 150, 150), (200, 50, 300, 150)])


def _build_assembly_too_close(out: Path):
    """Two 200x200 um chiplets separated by 30 um (< ASM.b 50 um spacing).
    Expected: ASM.b only."""
    _emit(out,
          boundary_boxes=[(0, 0, 200, 200), (230, 0, 430, 200)],
          pad_boxes=[(50, 50, 150, 150), (280, 50, 380, 150)])


def _build_assembly_too_small(out: Path):
    """One 50x50 um chiplet (2500 um^2 < ASM.e 10000 um^2) with an attachment
    pad fully inside. Expected: ASM.e only."""
    _emit(out,
          boundary_boxes=[(0, 0, 50, 50)],
          pad_boxes=[(10, 10, 40, 40)])


def _build_assembly_pad_outside(out: Path):
    """One 200x200 um chiplet with an attachment pad straddling the boundary.
    Expected: ASM.f only."""
    _emit(out,
          boundary_boxes=[(0, 0, 200, 200)],
          pad_boxes=[(150, 50, 250, 150)])


def _build_collision_internal_exchange0(out: Path):
    """GDS exchange0 carries TWO OVERLAPPING boxes (which would trigger ASM.a
    if the deck read the fab layer), but the authoritative manifest declares
    two well-separated boundaries. Proves the manifest-native deck ignores
    chiplet-internal / legacy exchange0 geometry.

    Manifest-native mode: clean. Legacy mode: ASM.a."""
    _emit(out,
          boundary_boxes=[(0, 0, 200, 200), (150, 0, 350, 200)],   # overlap in GDS
          pad_boxes=[(50, 50, 150, 150), (550, 50, 650, 150)],
          manifest_boxes=[(0, 0, 200, 200), (500, 0, 700, 200)])   # clean in manifest


_BUILDERS = {
    "assembly_ok":                  _build_assembly_ok,
    "assembly_overlap":             _build_assembly_overlap,
    "assembly_too_close":           _build_assembly_too_close,
    "assembly_too_small":           _build_assembly_too_small,
    "assembly_pad_outside":         _build_assembly_pad_outside,
    "collision_internal_exchange0": _build_collision_internal_exchange0,
}


@pytest.fixture(scope="session")
def fixture_layouts(tmp_path_factory) -> dict:
    """Build all assembly DRC fixtures once per session in a tmp dir. Each GDS
    has a sibling <stem>.boundaries.json the runner auto-discovers."""
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
