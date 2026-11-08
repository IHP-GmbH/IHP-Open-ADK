# SPDX-License-Identifier: Apache-2.0
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
FIXTURE_DIR = TEST_DIR / "fixtures"
TEST_ADAPTER = FIXTURE_DIR / "test_interposer_adapter.drc"
TEST_WIRE_BOND_ADAPTER = FIXTURE_DIR / "test_wire_bond_adapter.drc"

# Registry ids the fixture registry layer (fixtures/registry.json) declares.
# Adapters are selected by id only, so the suite names these, never a path.
TEST_ADAPTER_ID = "test_interposer"
TEST_INTERCONNECT_ADAPTER_ID = "test_interconnect"
TEST_WIRE_BOND_ADAPTER_ID = "test_wire_bond"

# Layer numbers used by the synthetic fixtures. exchange0 only models a
# pre-migration ("legacy") GDS; the manifest-native path ignores it. The
# attachment layer must match the test adapter.
EXCHANGE0_LAYER = (190, 0)
ATTACHMENT_LAYER = (999, 0)
# The wire-bond pad layer the wire-bond test adapter declares.
WIRE_BOND_LAYER = (998, 0)

# Make the runner importable from tests (PDK-agnostic helpers only).
sys.path.insert(0, str(RUNNER_DIR))
if str(ADK_ROOT) not in sys.path:
    sys.path.insert(0, str(ADK_ROOT))


@pytest.fixture(scope="session", autouse=True)
def registry_layer():
    """Point the registry's env layer at tests/fixtures for the whole session.

    Adapters resolve by registry id only, so the synthetic test adapters need a
    registry entry to be reachable at all. ``ADK_REGISTRY_DIR`` names a
    DIRECTORY holding ``registry.json`` (adk_registry._env_path), and the layer
    merges OVER the built-in one, so every shipped adapter id keeps resolving.
    Set in ``os.environ`` (via monkeypatch) so the subprocess-based tests, which
    inherit the environment, see the same layer.
    """
    import adk_registry

    mp = pytest.MonkeyPatch()
    mp.setenv("ADK_REGISTRY_DIR", str(FIXTURE_DIR))
    # Verify rather than assume: a typo in the env var name or the layer
    # basename would silently leave every test id unresolvable.
    res = adk_registry.resolve_adapter(TEST_ADAPTER_ID)
    assert res.value == TEST_ADAPTER, (
        f"fixture registry did not answer for {TEST_ADAPTER_ID!r}: {res}")
    yield FIXTURE_DIR
    mp.undo()


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


def _write_manifest(gds_path, boundary_boxes, mirror_x=False, cells=None):
    """Write a boundary-manifest sidecar from a list of (x1,y1,x2,y2) um boxes.
    These are the authoritative chiplet boundaries the manifest-native DRC
    checks (independent of any GDS layer). Record i names instance U<i+1> of
    source die TEST, so the exporter would name its die cell U<i+1>_TEST
    (U<i+1>_TEST_flipped when mirror_x). cells, when given, names each
    record's die cell explicitly as its `cell`."""
    boundaries = []
    for i, (x1, y1, x2, y2) in enumerate(boundary_boxes):
        boundaries.append({
            **({"cell": cells[i]} if cells else {}),
            "instance": f"U{i + 1}",
            "source_die": "TEST",
            "class": "chiplet",
            "transform": {"origin_um": [x1, y1], "rotation_deg": 0.0,
                          "mirror_x": mirror_x, "magnification": 1.0},
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


def _emit_wire_bond(out: Path, die_boxes, carrier_pads=(), die_pads=(),
                    subcell_pads=(), die_cell_names=None, mirror_x=False,
                    manifest_cells=None):
    """Write a fixture GDS laid out the way the exporter lays out an assembly:
    one cell per die under TOP, named after its manifest record, holding that
    die's own geometry; the carrier's wire-bond pads in TOP (carrier_pads) or
    in a carrier subcell no record names (subcell_pads). die_pads go in a
    subcell of the first die's cell, as an imported die layout's pads do.
    die_cell_names overrides the cell names (a repeated name is one shared
    cell); manifest_cells writes them into the manifest as each record's
    `cell`."""
    ly = _new_layout()
    top = ly.create_cell("TOP")
    wb = ly.layer(*WIRE_BOND_LAYER)
    suffix = "_flipped" if mirror_x else ""
    names = die_cell_names or [f"U{i + 1}_TEST{suffix}" for i in range(len(die_boxes))]
    dies = {}
    for i, name in enumerate(names):
        if name not in dies:
            dies[name] = ly.create_cell(name)
            if i == 0 and die_pads:
                die_layout = ly.create_cell(name + "_LAYOUT")
                for box in die_pads:
                    _add_box(die_layout, wb, *box)
                dies[name].insert(kdb.CellInstArray(die_layout.cell_index(), kdb.Trans()))
        top.insert(kdb.CellInstArray(dies[name].cell_index(), kdb.Trans()))
    for box in carrier_pads:
        _add_box(top, wb, *box)
    if subcell_pads:
        carrier = ly.create_cell("CARRIER_PADS")
        for box in subcell_pads:
            _add_box(carrier, wb, *box)
        top.insert(kdb.CellInstArray(carrier.cell_index(), kdb.Trans()))
    ly.write(str(out))
    _write_manifest(out, die_boxes, mirror_x=mirror_x, cells=manifest_cells)


# One 200x200 um die; ASM.g is 200 um.
_WB_DIE = [(0, 0, 200, 200)]


def _build_wire_bond_ok(out: Path):
    """A carrier pad 250 um from the die edge. Expected: no violations."""
    _emit_wire_bond(out, _WB_DIE, carrier_pads=[(450, 50, 460, 60)])


def _build_wire_bond_too_close(out: Path):
    """A carrier pad 150 um from the die edge. Expected: ASM.g only."""
    _emit_wire_bond(out, _WB_DIE, carrier_pads=[(350, 50, 360, 60)])


def _build_wire_bond_diagonal(out: Path):
    """A carrier pad diagonal to the die corner, 150 um off in x and in y,
    so 212 um euclidean. Expected: no violations (the rule is euclidean)."""
    _emit_wire_bond(out, _WB_DIE, carrier_pads=[(350, 350, 360, 360)])


def _build_wire_bond_under_die(out: Path):
    """A carrier pad drawn under the die. Expected: ASM.g only."""
    _emit_wire_bond(out, _WB_DIE, carrier_pads=[(50, 50, 60, 60)])


def _build_wire_bond_die_own_pads(out: Path):
    """The die's cell carries its own pads on the wire-bond layer, the
    carrier's pad is 250 um away. Expected: no violations (die cells are
    pruned before the pads are read)."""
    _emit_wire_bond(out, _WB_DIE, carrier_pads=[(450, 50, 460, 60)],
                    die_pads=[(50, 50, 60, 60), (190, 190, 200, 200)])


def _build_wire_bond_flipped(out: Path):
    """As die_own_pads, with a mirrored record and a _flipped die cell.
    Expected: no violations."""
    _emit_wire_bond(out, _WB_DIE, carrier_pads=[(450, 50, 460, 60)],
                    die_pads=[(50, 50, 60, 60)], mirror_x=True)


def _build_wire_bond_subcell(out: Path):
    """A pad 150 um from the die edge inside a carrier subcell no manifest
    record names. Expected: ASM.g only (the subcell is carrier, so read)."""
    _emit_wire_bond(out, _WB_DIE, subcell_pads=[(350, 50, 360, 60)])


def _build_wire_bond_unnamed_die(out: Path):
    """The die cell is named U1_OTHER, not the U1_TEST the record names.
    Expected: the deck refuses."""
    _emit_wire_bond(out, _WB_DIE, carrier_pads=[(450, 50, 460, 60)],
                    die_pads=[(50, 50, 60, 60)], die_cell_names=["U1_OTHER"])


def _build_wire_bond_shared_cell(out: Path):
    """Two dies 300 um apart sharing one die cell whose name needs glob
    escaping, named by each record's `cell` as the resolved-v2 entry writes
    it. The die layout carries its own pads; the carrier pad is 250 um from
    both dies. Expected: no violations."""
    name = "DIE-v1.0*"
    _emit_wire_bond(out, [(0, 0, 200, 200), (500, 0, 700, 200)],
                    carrier_pads=[(345, 450, 355, 460)],
                    die_pads=[(50, 50, 60, 60)],
                    die_cell_names=[name, name], manifest_cells=[name, name])


def _build_assembly_ok(out: Path):
    """Two 200x200 um chiplets, 300 um apart (ASM.b is 200 um), each with an
    attachment pad fully inside the boundary. Expected: no violations."""
    _emit(out,
          boundary_boxes=[(0, 0, 200, 200), (500, 0, 700, 200)],
          pad_boxes=[(50, 50, 150, 150), (550, 50, 650, 150)])


def _build_assembly_overlap(out: Path):
    """Two 200x200 um chiplets overlapping by 50 um. Expected: ASM.a."""
    _emit(out,
          boundary_boxes=[(0, 0, 200, 200), (150, 0, 350, 200)],
          pad_boxes=[(50, 50, 150, 150), (200, 50, 300, 150)])


def _build_assembly_too_close(out: Path):
    """Two 200x200 um chiplets separated by 30 um (< ASM.b 200 um spacing).
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
    "wire_bond_ok":                 _build_wire_bond_ok,
    "wire_bond_too_close":          _build_wire_bond_too_close,
    "wire_bond_diagonal":           _build_wire_bond_diagonal,
    "wire_bond_under_die":          _build_wire_bond_under_die,
    "wire_bond_die_own_pads":       _build_wire_bond_die_own_pads,
    "wire_bond_flipped":            _build_wire_bond_flipped,
    "wire_bond_subcell":            _build_wire_bond_subcell,
    "wire_bond_unnamed_die":        _build_wire_bond_unnamed_die,
    "wire_bond_shared_cell":        _build_wire_bond_shared_cell,
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
def test_adapter(registry_layer) -> str:
    """Registry id of the synthetic interposer adapter.

    An id, not a path: the runner accepts only registered ids, so a test that
    held a path here could not reach the runner at all.
    """
    assert TEST_ADAPTER.is_file(), f"Missing test adapter: {TEST_ADAPTER}"
    return TEST_ADAPTER_ID


@pytest.fixture(scope="session")
def wire_bond_adapter(registry_layer) -> str:
    """Registry id of the synthetic interposer adapter that also declares
    wire-bond pads (998/0)."""
    assert TEST_WIRE_BOND_ADAPTER.is_file(), \
        f"Missing test adapter: {TEST_WIRE_BOND_ADAPTER}"
    return TEST_WIRE_BOND_ADAPTER_ID
