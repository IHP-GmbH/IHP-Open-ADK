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


def d1_pins():
    """Synthetic pin list for D1 (die-local GDS um; duplicate name on purpose)."""
    return [
        {"name": "A0", "x_um": -100.0, "y_um": -50.0},
        {"name": "A0", "x_um": 100.0, "y_um": -50.0},
        {"name": "VDD", "x_um": 0.0, "y_um": 60.0},
    ]


def bump_assembly():
    """base_assembly plus a netlist block binding D1's VDD pin."""
    assembly = base_assembly()
    assembly["netlist"] = {"nets": [
        {"name": "VDD_NET",
         "connections": [{"component": "D1", "pin": "VDD",
                          "layer": "TopMetal2"}]},
    ]}
    return assembly


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


def test_render_3dbv_bumps_golden():
    golden = (GOLDEN_DIR / "chiplet2dbx_unit_demo_bumps.3dbv.golden").read_text()
    rendered = chiplet2dbx.render_3dbv(bump_assembly(),
                                       die_pins={"D1": d1_pins()})
    assert rendered == golden, REGOLDEN_HINT


def test_render_bmap_golden():
    golden = (GOLDEN_DIR / "chiplet2dbx_unit_demo.bmap.golden").read_text()
    rendered = chiplet2dbx.render_bmap(bump_assembly(), "DIE_A__bump25",
                                       die_pins={"D1": d1_pins()})
    assert rendered == golden, REGOLDEN_HINT


def test_render_tech_lef_route_layer_golden():
    golden = (GOLDEN_DIR / "chiplet2dbx_tech_route.lef.golden").read_text()
    rendered = chiplet2dbx.render_tech_lef(1000, "BUMP_ATTACH")
    assert rendered == golden, REGOLDEN_HINT


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


# ----------------------------------------------------------- bump maps

def test_bump_rows_mirror_and_bind():
    text = chiplet2dbx.render_bmap(bump_assembly(), "DIE_A__bump25",
                                   die_pins={"D1": d1_pins()})
    # flip_chip pre-mirrors x: pin (-100,-50) on a 300x200 die -> (250, 50);
    # the duplicate name gets a suffix.
    assert "A0 BUMP_BUMP25 250 50 - -" in text
    assert "A0_2 BUMP_BUMP25 50 50 - -" in text
    # netlist-bound pin emits port + net
    assert "VDD BUMP_BUMP25 150 160 VDD VDD_NET" in text


def test_bump_rows_face_up_not_mirrored():
    assembly = bump_assembly()
    assembly["components"][1]["orientation"] = "face_up"
    text = chiplet2dbx.render_bmap(assembly, "DIE_A__bump25",
                                   die_pins={"D1": d1_pins()})
    assert "A0 BUMP_BUMP25 50 50 - -" in text


def test_def_splits_by_method_with_pins():
    assembly = bump_assembly()
    clone = copy.deepcopy(assembly["components"][1])
    clone["id"] = "D3"
    clone["connection"] = "bump40"
    clone["position"] = {"x": 250.0, "y": 650.0, "z": 50.0}
    assembly["components"].append(clone)
    pins = {"D1": d1_pins(), "D3": d1_pins()}
    dbv = chiplet2dbx.render_3dbv(assembly, die_pins=pins)
    assert "  DIE_A__bump25:" in dbv
    assert "  DIE_A__bump40:" in dbv
    assert "LEF_file: [bump25__techb.lef]" in dbv
    assert "LEF_file: [bump40__techb.lef]" in dbv


def test_shared_def_requires_identical_pins():
    # no netlist block: both dies bind identically, so they share a def
    assembly = base_assembly()
    clone = copy.deepcopy(assembly["components"][1])
    clone["id"] = "D3"
    clone["position"] = {"x": 250.0, "y": 650.0, "z": 35.0}
    assembly["components"].append(clone)
    other = d1_pins()
    other[0]["x_um"] = -90.0
    with pytest.raises(ExportError, match="different pin lists"):
        chiplet2dbx.render_3dbv(
            assembly, die_pins={"D1": d1_pins(), "D3": other})


def test_pin_outside_outline_fails():
    pins = d1_pins()
    pins.append({"name": "FAR", "x_um": 400.0, "y_um": 0.0})
    with pytest.raises(ExportError, match="FAR"):
        chiplet2dbx.render_3dbv(bump_assembly(), die_pins={"D1": pins})


def test_pins_for_unknown_ref_fail():
    with pytest.raises(ExportError, match="unknown die refs"):
        chiplet2dbx.render_3dbv(bump_assembly(), die_pins={"D9": d1_pins()})


def test_pins_without_connection_fail():
    assembly = bump_assembly()
    del assembly["components"][1]["connection"]
    with pytest.raises(ExportError, match="no connection method"):
        chiplet2dbx.render_3dbv(assembly, die_pins={"D1": d1_pins()})


def test_macro_naming_matches_generator():
    """adk's bmap cell column must match the interconnect PDK generator."""
    try:
        generator = chiplet2dbx._bump_lef_generator()
    except ExportError:
        pytest.skip("interconnect_pdk sibling checkout not available")
    for method in ("cupillar_opt1", "vendorx_microbump", "bump25"):
        assert chiplet2dbx._bump_macro_name(method) == \
            generator.bump_macro_name(method)


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


def _demo_chiplet_path():
    env = os.environ.get("CHIPLET2DBX_DEMO_CHIPLET")
    demo = Path(env) if env else (
        ADK_ROOT.parent / "kicad_designs" / "interposer_wire_bonding_demo"
        / "interposer_wire_bonding_demo.chiplet")
    return demo if demo.is_file() else None


@needs_openroad_image
def test_live_check_3dblox_wire_bond_demo(tmp_path):
    demo = _demo_chiplet_path()
    if demo is None:
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


def _export_with_bumps(assembly, tmp_path, die_pins):
    try:
        return chiplet2dbx.export_3dblox(assembly, tmp_path,
                                         die_pins=die_pins)
    except ExportError as exc:
        if "bump LEF generator" in str(exc):
            pytest.skip("interconnect_pdk sibling checkout not available")
        raise


def live_bump_assembly():
    """bump_assembly rebased onto a real manifest method: the bump macro
    LEF is manifest-driven, so live runs need a method the interconnect
    PDK actually defines."""
    assembly = bump_assembly()
    assembly["connection_stacks"]["cupillar_opt1"] = {"layers": [
        {"name": "CuPillar", "material": "Cu", "height": 28.0,
         "diameter": 44.0},
        {"name": "SnAgCap", "material": "SnAg", "height": 16.0,
         "diameter": 44.0},
    ]}
    d1 = assembly["components"][1]
    d1["connection"] = "cupillar_opt1"
    d1["position"]["z"] = 54.0  # 10 (mount) + 44 (stack)
    return assembly


@needs_openroad_image
def test_live_check_3dblox_with_bumps(tmp_path):
    dbv, dbx, extra = _export_with_bumps(
        live_bump_assembly(), tmp_path, {"D1": d1_pins()})
    assert any(p.suffix == ".bmap" for p in extra)
    _assert_clean(_run_openroad(tmp_path, dbx.name))


@needs_openroad_image
def test_live_bump_alignment_sees_bumps(tmp_path):
    """Negative control: a bump outside its region must trip ODB-0463."""
    dbv, dbx, extra = _export_with_bumps(
        live_bump_assembly(), tmp_path, {"D1": d1_pins()})
    bmap = next(p for p in extra if p.suffix == ".bmap")
    bmap.write_text(bmap.read_text().replace(
        "A0 BUMP_CUPILLAR_OPT1 250 50", "A0 BUMP_CUPILLAR_OPT1 5000 50"))
    output = _run_openroad(tmp_path, dbx.name)
    assert "ODB-0463" in output, output


def test_unknown_manifest_method_fails_loudly(tmp_path):
    """A .chiplet stack id absent from the interconnect manifest cannot
    get a bump macro; the export must say so instead of guessing."""
    try:
        chiplet2dbx._bump_lef_generator()
    except ExportError:
        pytest.skip("interconnect_pdk sibling checkout not available")
    with pytest.raises(ExportError, match="not in the interconnect PDK"):
        chiplet2dbx.export_3dblox(bump_assembly(), tmp_path,
                                  die_pins={"D1": d1_pins()})


@needs_openroad_image
def test_live_check_3dblox_demo_with_bumps(tmp_path):
    demo = _demo_chiplet_path()
    pins = os.environ.get("CHIPLET2DBX_DEMO_PINS")
    pins = Path(pins) if pins else (
        ADK_ROOT.parent / "adk-tools" / "examples" / "two_die_interposer"
        / "chiplets" / "metal_test_chiplet.pins.json")
    if demo is None or not pins.is_file():
        pytest.skip("wire-bond demo .chiplet or pins.json not available")
    assembly = chiplet2dbx.load_chiplet(demo)
    dbv, dbx, _ = _export_with_bumps(
        assembly, tmp_path, {"U1": pins, "U2": pins})
    _assert_clean(_run_openroad(tmp_path, dbx.name))
