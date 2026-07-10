# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Tests for openroad/chiplet2dbx.py.

Three tiers:

  - pure unit tests + golden byte-comparisons (always run);
  - a vendored-reader drift guard against a chiplet-spec sibling checkout
    (skips when the sibling is absent);
  - a live answer-key against the pinned OpenROAD 3dblox Docker image
    (skips when docker or the image is absent): read_3dbx + check_3dblox
    must come back clean on exported assemblies, and a mutated geometry
    must trip the linter -- proving the green run is not vacuous.
"""

import copy
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ADK_ROOT = Path(__file__).resolve().parents[1]
if str(ADK_ROOT / "openroad") not in sys.path:
    sys.path.insert(0, str(ADK_ROOT / "openroad"))

import chiplet2dbx  # noqa: E402
from chiplet2dbx import ExportError  # noqa: E402

SCRIPT = ADK_ROOT / "openroad" / "chiplet2dbx.py"
GOLDEN_DIR = ADK_ROOT / "tests" / "golden"
DOCKER_IMAGE = "openroad-3dblox:ad9e7248"

REGOLDEN_HINT = (
    "regenerate with: python tests/regolden_chiplet2dbx.py "
    "(after reviewing the diff)"
)


def base_assembly():
    """Small two-die assembly exercising def dedup inputs and two stacks."""
    return {
        "format_version": "1.0",
        "assembly": {"name": "unit_demo", "units": "um"},
        "technologies": {
            "techa": {"dbu": 0.001},
            "techb": {"dbu": 0.001},
        },
        "connection_stacks": {
            "bump25": {"layers": [
                {"name": "Cu", "material": "Cu", "height": 20.0,
                 "diameter": 30.0},
                {"name": "Cap", "material": "SnAg", "height": 5.0,
                 "diameter": 30.0},
            ]},
            "bump40": {"layers": [
                {"name": "Ball", "material": "SAC305", "height": 40.0,
                 "diameter": 40.0},
            ]},
        },
        "components": [
            {"id": "interposer", "type": "interposer", "technology": "techa",
             "top_cell": "SUBSTRATE",
             "dimensions": {"width": 1000.0, "height": 800.0,
                            "thickness": 10.0},
             "position": {"x": 500.0, "y": 400.0, "z": 0.0}},
            {"id": "D1", "type": "die", "technology": "techb",
             "layout": "d1.gds", "top_cell": "DIE_A",
             "connection": "bump25", "orientation": "flip_chip",
             "dimensions": {"width": 300.0, "height": 200.0,
                            "thickness": 150.0},
             "position": {"x": 250.0, "y": 300.0, "z": 35.0},
             "rotation": {"z": 0.0}},
            {"id": "D2", "type": "die", "technology": "techb",
             "layout": "d2.gds", "top_cell": "DIE_B",
             "connection": "bump40", "orientation": "flip_chip",
             "dimensions": {"width": 200.0, "height": 200.0,
                            "thickness": 100.0},
             "position": {"x": 700.0, "y": 300.0, "z": 50.0},
             "rotation": {"z": 0.0}},
        ],
    }


# ---------------------------------------------------------------- goldens

def test_render_3dbv_golden():
    golden = (GOLDEN_DIR / "chiplet2dbx_unit_demo.3dbv.golden").read_text()
    assert chiplet2dbx.render_3dbv(base_assembly()) == golden, REGOLDEN_HINT


def test_render_3dbx_golden():
    golden = (GOLDEN_DIR / "chiplet2dbx_unit_demo.3dbx.golden").read_text()
    rendered = chiplet2dbx.render_3dbx(base_assembly(), "unit_demo.3dbv")
    assert rendered == golden, REGOLDEN_HINT


def test_render_tech_lef_golden():
    golden = (GOLDEN_DIR / "chiplet2dbx_tech.lef.golden").read_text()
    assert chiplet2dbx.render_tech_lef(1000) == golden, REGOLDEN_HINT


# ------------------------------------------------------------- semantics

def test_center_to_corner_conversion():
    text = chiplet2dbx.render_3dbx(base_assembly(), "unit_demo.3dbv")
    # interposer center (500,400) with 1000x800 outline -> corner (0,0)
    assert "loc: [0, 0]" in text
    # D1 center (250,300), 300x200 -> corner (100, 200)
    assert "loc: [100, 200]" in text


def test_die_def_dedup_same_source():
    assembly = base_assembly()
    d1 = assembly["components"][1]
    clone = copy.deepcopy(d1)
    clone["id"] = "D3"
    clone["position"] = {"x": 250.0, "y": 650.0, "z": 35.0}
    assembly["components"].append(clone)
    dbv = chiplet2dbx.render_3dbv(assembly)
    assert dbv.count("  DIE_A:") == 1
    dbx = chiplet2dbx.render_3dbx(assembly, "unit_demo.3dbv")
    assert dbx.count("reference: DIE_A") == 2


def test_die_def_name_collision_gets_suffix():
    assembly = base_assembly()
    d1 = assembly["components"][1]
    clone = copy.deepcopy(d1)
    clone["id"] = "D3"
    clone["layout"] = "other.gds"  # same top_cell, different source
    clone["position"] = {"x": 250.0, "y": 650.0, "z": 35.0}
    assembly["components"].append(clone)
    dbv = chiplet2dbx.render_3dbv(assembly)
    assert "  DIE_A:" in dbv
    assert "  DIE_A_2:" in dbv


def test_orient_composition():
    assembly = base_assembly()
    d1 = assembly["components"][1]

    d1["rotation"]["z"] = 90.0
    text = chiplet2dbx.render_3dbx(assembly, "unit_demo.3dbv")
    assert "orient: MZ_R90" in text
    # quarter turn swaps spans: 300x200 die centered at (250,300)
    # -> corner (250-100, 300-150) = (150, 150)
    assert "loc: [150, 150]" in text

    d1["orientation"] = "face_up"
    text = chiplet2dbx.render_3dbx(assembly, "unit_demo.3dbv")
    assert "orient: R90" in text
    # face_up dies attach through their back region
    assert "top: D1.regions.back" in text


def test_non_90_rotation_fails():
    assembly = base_assembly()
    assembly["components"][1]["rotation"]["z"] = 45.0
    with pytest.raises(ExportError, match="multiples"):
        chiplet2dbx.render_3dbx(assembly, "unit_demo.3dbv")


def test_face_down_fails():
    assembly = base_assembly()
    assembly["components"][1]["orientation"] = "face_down"
    with pytest.raises(ExportError, match="face_down"):
        chiplet2dbx.render_3dbx(assembly, "unit_demo.3dbv")


def test_z_gap_mismatch_fails():
    assembly = base_assembly()
    assembly["components"][1]["position"]["z"] = 36.0
    with pytest.raises(ExportError, match="z gap"):
        chiplet2dbx.render_3dbx(assembly, "unit_demo.3dbv")


def test_unresolvable_connection_fails():
    assembly = base_assembly()
    assembly["components"][1]["connection"] = "nope"
    with pytest.raises(ExportError, match="does not resolve"):
        chiplet2dbx.render_3dbx(assembly, "unit_demo.3dbv")


def test_missing_connection_fails():
    assembly = base_assembly()
    del assembly["components"][1]["connection"]
    with pytest.raises(ExportError, match="no connection stack"):
        chiplet2dbx.render_3dbx(assembly, "unit_demo.3dbv")


def test_zero_thickness_die_fails_pointing_at_producer():
    assembly = base_assembly()
    assembly["components"][1]["dimensions"]["thickness"] = 0.0
    with pytest.raises(ExportError, match="DIE_THICKNESS_UM"):
        chiplet2dbx.render_3dbv(assembly)


def test_non_um_units_fail():
    assembly = base_assembly()
    assembly["assembly"]["units"] = "mm"
    with pytest.raises(ExportError, match="units"):
        chiplet2dbx.render_3dbv(assembly)


def test_inconsistent_dbu_fails():
    assembly = base_assembly()
    assembly["technologies"]["techb"]["dbu"] = 0.002
    with pytest.raises(ExportError, match="dbu"):
        chiplet2dbx.render_3dbv(assembly)


def test_interposer_count_enforced():
    assembly = base_assembly()
    assembly["components"].append(copy.deepcopy(assembly["components"][0]))
    with pytest.raises(ExportError, match="exactly one interposer"):
        chiplet2dbx.render_3dbv(assembly)


# ------------------------------------------------------------------- CLI

def test_cli_roundtrip(tmp_path):
    chiplet = tmp_path / "unit_demo.chiplet"
    chiplet2dbx.cfio.dump(base_assembly(), chiplet)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--chiplet", str(chiplet),
         "--out-dir", str(tmp_path / "out")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    out = tmp_path / "out"
    for name in ("unit_demo.3dbv", "unit_demo.3dbx",
                 "techa.lef", "techb.lef"):
        assert (out / name).is_file(), name


def test_cli_reports_errors(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--chiplet",
         str(tmp_path / "missing.chiplet")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert "chiplet2dbx: error:" in proc.stderr


# ------------------------------------------------- vendored reader guard

def test_vendored_reader_matches_spec_reference():
    env = os.environ.get("CHIPLET_SPEC_ROOT")
    spec_root = Path(env) if env else ADK_ROOT.parent / "chiplet-spec"
    reference = (spec_root / "reference" / "python" / "chiplet_format_io"
                 / "__init__.py")
    if not reference.is_file():
        pytest.skip("chiplet-spec sibling checkout not available")
    vendored = ADK_ROOT / "vendor" / "chiplet_format_io" / "__init__.py"
    assert vendored.read_bytes() == reference.read_bytes(), (
        "vendored chiplet_format_io drifted from the chiplet-spec reference; "
        "re-copy it verbatim"
    )


# ------------------------------------------------------ live answer-key

def _docker_image_available():
    docker = shutil.which("docker")
    if not docker:
        return False
    probe = subprocess.run([docker, "image", "inspect", DOCKER_IMAGE],
                           capture_output=True)
    return probe.returncode == 0


needs_openroad_image = pytest.mark.skipif(
    not _docker_image_available(),
    reason="docker or the pinned %s image is not available" % DOCKER_IMAGE,
)

ACCEPT_TCL = """\
read_3dbx %s
puts "READ_3DBX_OK"
check_3dblox
puts "CHECK_3DBLOX_OK"
exit
"""


def _run_openroad(workdir: Path, dbx_name: str) -> str:
    tcl = workdir / "accept.tcl"
    tcl.write_text(ACCEPT_TCL % dbx_name)
    # The image runs as an unprivileged user; pytest tmp dirs are 0700, so
    # run with the host uid to keep the mounted workdir readable.
    proc = subprocess.run(
        ["docker", "run", "--rm",
         "--user", "%d:%d" % (os.getuid(), os.getgid()),
         "-v", "%s:%s" % (workdir, workdir), "-w", str(workdir),
         DOCKER_IMAGE, "-exit", str(tcl)],
        capture_output=True, text=True, timeout=300,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    return output


def _assert_clean(output: str) -> None:
    assert "READ_3DBX_OK" in output
    assert "CHECK_3DBLOX_OK" in output
    assert "[WARNING" not in output, output
    assert "[ERROR" not in output, output


@needs_openroad_image
def test_live_check_3dblox_synthetic(tmp_path):
    dbv, dbx, _ = chiplet2dbx.export_3dblox(base_assembly(), tmp_path)
    _assert_clean(_run_openroad(tmp_path, dbx.name))


@needs_openroad_image
def test_live_check_3dblox_wire_bond_demo(tmp_path):
    env = os.environ.get("CHIPLET2DBX_DEMO_CHIPLET")
    demo = Path(env) if env else (
        ADK_ROOT.parent / "kicad_designs" / "interposer_wire_bonding_demo"
        / "interposer_wire_bonding_demo.chiplet")
    if not demo.is_file():
        pytest.skip("wire-bond demo .chiplet not available")
    assembly = chiplet2dbx.load_chiplet(demo)
    dbv, dbx, _ = chiplet2dbx.export_3dblox(assembly, tmp_path)
    _assert_clean(_run_openroad(tmp_path, dbx.name))


@needs_openroad_image
def test_live_linter_sees_geometry(tmp_path):
    """Negative control: a 1 um z error must trip the connection check."""
    dbv, dbx, _ = chiplet2dbx.export_3dblox(base_assembly(), tmp_path)
    text = dbx.read_text().replace("z: 35\n", "z: 36\n")
    assert "z: 36" in text
    dbx.write_text(text)
    output = _run_openroad(tmp_path, dbx.name)
    assert "ODB-0207" in output, output
    assert "ODB-0273" in output, output
