# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Tests for openroad/chiplet2dbx.py.

Three tiers:

  - pure unit tests + golden byte-comparisons (always run);
  - a vendored-reader drift guard against the chiplet-spec commit that
    vendor/README.md declares, resolved with git (skips when no chiplet-spec
    repository is reachable, or when the one found does not carry the
    pinned commit);
  - a live answer-key against the pinned OpenROAD 3dblox Docker image
    (skips when docker or the image is absent): read_3dbx + check_3dblox
    must come back clean on exported assemblies, and a mutated geometry
    must trip the linter -- proving the green run is not vacuous.
"""

import copy
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, NamedTuple, Optional

import pytest

# The exporter's vendored chiplet_format_io reader needs PyYAML. Skip this
# module (instead of erroring the whole collection) where it is missing.
pytest.importorskip("yaml", reason="chiplet2dbx needs PyYAML")

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
             "layout": "d1.gds", "top_cell": "DIE_A", "anchor": "gds_origin",
             "connection": "bump25", "orientation": "flip_chip",
             "dimensions": {"width": 300.0, "height": 200.0,
                            "thickness": 150.0},
             "position": {"x": 250.0, "y": 300.0, "z": 35.0},
             "rotation": {"z": 0.0}},
            {"id": "D2", "type": "die", "technology": "techb",
             "layout": "d2.gds", "top_cell": "DIE_B", "anchor": "gds_origin",
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


def attach_surface_assembly():
    """base_assembly with the interposer split: a thin die-attachment surface
    (13.83 um) decoupled from a thick physical body (300 um). Dies mount on the
    attachment surface, so their z is attachment_surface_z + stack height."""
    assembly = base_assembly()
    interposer = assembly["components"][0]
    interposer["attachment_surface_z"] = 13.83
    interposer["dimensions"]["thickness"] = 300.0  # physical body, not the mount ref
    assembly["components"][1]["position"]["z"] = 13.83 + 25.0  # bump25 total
    assembly["components"][2]["position"]["z"] = 13.83 + 40.0  # bump40 total
    return assembly


# A tiny two-die eda-views fixture: a flipped die U1 (DIE_F) and a face-up die
# U2 (DIE_U), each with an eda-views record and the die contract it is bound
# to, sharing the technology LEF, the pad LEF and the Liberty files byte for
# byte. Pads are (DEF instance, lower-left x, y in um, net, role); a pad
# master is 10 x 10 um, so a pad centre is its placement plus (5, 5).

VIEWS_FRAME = "die-local DEF frame, Y up; the flip-chip footprint mirrors X"

VIEWS_DIES = {
    "U1": ("DIE_F", 100, 80, [("PAD_A", 10, 20, "a", "SIGNAL"),
                              ("PAD_V", 70, 50, "VDD", "POWER")]),
    "U2": ("DIE_U", 100, 80, [("PAD_B", 60, 20, "b", "SIGNAL"),
                              ("PAD_G", 20, 50, "VSS", "GROUND")]),
}

VIEWS_TECH_LEF = """\
VERSION 5.8 ;
BUSBITCHARS "[]" ;
DIVIDERCHAR "/" ;
UNITS
  DATABASE MICRONS 1000 ;
END UNITS
MANUFACTURINGGRID 0.001 ;
LAYER M1
  TYPE ROUTING ;
  DIRECTION HORIZONTAL ;
  PITCH 1 ;
  WIDTH 1 ;
END M1
END LIBRARY
"""

VIEWS_PAD_LEF = """\
VERSION 5.8 ;
BUSBITCHARS "[]" ;
DIVIDERCHAR "/" ;
MACRO PADX
  CLASS COVER ;
  ORIGIN 0 0 ;
  SIZE 10 BY 10 ;
  PIN pad
    DIRECTION INOUT ;
    USE SIGNAL ;
    PORT
      LAYER M1 ;
        RECT 0 0 10 10 ;
    END
  END pad
END PADX
END LIBRARY
"""

VIEWS_LIBERTY = """\
library (fix_%s) {
  delay_model : table_lookup ;
  time_unit : "1ns" ;
  voltage_unit : "1V" ;
  current_unit : "1mA" ;
  capacitive_load_unit (1, pf) ;
  pulling_resistance_unit : "1kohm" ;
  cell (PADX) {
    area : 100 ;
    pin (pad) {
      direction : inout ;
      capacitance : 0.001 ;
    }
  }
}
"""

VIEWS_TOP = """\
// System top for views_demo.
module views_demo (
    inout wire ext
);
    wire n_ab;
    DIE_F U1 (
        .a(n_ab)
    );
    DIE_U U2 (
        .b(n_ab)
    );
endmodule
"""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _views_def(design, width, height, pads) -> str:
    lines = ["VERSION 5.8 ;", 'DIVIDERCHAR "/" ;', 'BUSBITCHARS "[]" ;',
             "DESIGN %s ;" % design, "UNITS DISTANCE MICRONS 1000 ;",
             "DIEAREA ( 0 0 ) ( %d %d ) ;" % (width * 1000, height * 1000),
             "COMPONENTS %d ;" % len(pads)]
    lines += ["    - %s PADX + FIXED ( %d %d ) N ;" % (name, x * 1000, y * 1000)
              for name, x, y, _, _ in pads]
    lines += ["END COMPONENTS", "PINS %d ;" % len(pads)]
    for name, x, y, net, role in pads:
        lines += ["    - %s + NET %s + DIRECTION INOUT + USE %s" % (net, net, role),
                  "      + PORT",
                  "        + LAYER M1 ( -5000 -5000 ) ( 5000 5000 )",
                  "        + FIXED ( %d %d ) N ;" % ((x + 5) * 1000, (y + 5) * 1000)]
    lines += ["END PINS", "NETS %d ;" % len(pads)]
    lines += ["    - %s ( PIN %s ) ( %s pad ) + USE %s ;" % (net, net, name, role)
              for name, x, y, net, role in pads]
    lines += ["END NETS", "END DESIGN"]
    return "\n".join(lines) + "\n"


def write_views_fixture(root: Path):
    """Write both records and contracts under ``root``; returns the
    ``die_views`` map {ref: (record, contract_dir)}."""
    die_views = {}
    for ref, (design, width, height, pads) in VIEWS_DIES.items():
        pins = {
            "version": "1.0", "chiplet_name": design, "dbu_um": 0.001,
            "coordinate_frame": VIEWS_FRAME,
            "die_bbox_dbu": {"x_min": 0.0, "y_min": 0.0,
                             "x_max": width * 1000.0, "y_max": height * 1000.0},
            "pins": [{"name": name, "center_x_dbu": (x + 5) * 1000.0,
                      "center_y_dbu": (y + 5) * 1000.0, "port": net,
                      "net": net, "role": role}
                     for name, x, y, net, role in pads],
        }
        pins_text = json.dumps(pins, indent=2) + "\n"
        contract = root / "contracts" / design
        contract.mkdir(parents=True)
        (contract / "pins.json").write_text(pins_text)
        (contract / "build-record.json").write_text('{"design": "%s"}\n' % design)
        layout_sha = hashlib.sha256(b"layout " + design.encode()).hexdigest()
        (contract / "die-contract.json").write_text(json.dumps({
            "schema": "die-output-contract", "version": "1.1",
            "die": {"name": design, "layout": "layout.gds",
                    "layout_sha256": layout_sha, "top_cell": design,
                    "dbu_um": 0.001, "bbox_um": [0, 0, width, height],
                    "frame": "die_local_gds_origin"},
            "build_record": {"path": "build-record.json",
                             "sha256": _sha(contract / "build-record.json")},
            "inventory": {"kind": "pins_json", "path": "pins.json",
                          "sha256": _sha(contract / "pins.json")},
        }, indent=2) + "\n")

        record = root / "records" / ref
        files = {
            "def/%s.def" % design: _views_def(design, width, height, pads),
            "tech_lef/fix_tech.lef": VIEWS_TECH_LEF,
            "lef/fix_pads.lef": VIEWS_PAD_LEF,
            "liberty/fix_typ.lib": VIEWS_LIBERTY % "typ",
            "liberty/fix_slow.lib": VIEWS_LIBERTY % "slow",
            "pins/pins.json": pins_text,
        }
        for rel, text in files.items():
            (record / rel).parent.mkdir(parents=True, exist_ok=True)
            (record / rel).write_text(text)
        kinds = [("def", None), ("tech_lef", ["typ", "slow"]), ("lef", None),
                 ("liberty", ["typ"]), ("liberty", ["slow"]), ("pins", None)]
        rows = []
        for (kind, corners), rel in zip(kinds, files):
            row = {"kind": kind, "file": rel, "sha256": _sha(record / rel),
                   "status": "recorded"}
            if corners:
                row["corners"] = corners
            rows.append(row)
        (record / "eda-views.json").write_text(json.dumps({
            "schema": "chiplet-flow-eda-views/v1", "design": design,
            "corners": ["typ", "slow"], "default_corner": "typ",
            "bound_to": {"layout_sha256": layout_sha,
                         "build_record_sha256":
                             _sha(contract / "build-record.json")},
            "views": rows,
        }, indent=2) + "\n")
        die_views[ref] = (record / "eda-views.json", contract)
    return die_views


def views_assembly():
    """Two record dies on a substrate: U1 flipped, U2 face up."""
    def die(ref, top_cell, x, orientation):
        return {"id": ref, "type": "die", "technology": "sg",
                "layout": "%s.gds" % top_cell.lower(), "top_cell": top_cell,
                "anchor": "bbox_center", "connection": "bump25",
                "orientation": orientation,
                "dimensions": {"width": 100.0, "height": 80.0,
                               "thickness": 150.0},
                "position": {"x": x, "y": 150.0, "z": 35.0},
                "rotation": {"z": 0.0}}
    return {
        "format_version": "1.0",
        "assembly": {"name": "views_demo", "units": "um"},
        "technologies": {"sg": {"dbu": 0.001}, "subst": {"dbu": 0.001}},
        "connection_stacks": {"bump25": {"layers": [
            {"name": "Cu", "material": "Cu", "height": 25.0,
             "diameter": 30.0}]}},
        "components": [
            {"id": "interposer", "type": "interposer", "technology": "subst",
             "top_cell": "SUBSTRATE",
             "dimensions": {"width": 400.0, "height": 300.0,
                            "thickness": 10.0},
             "position": {"x": 200.0, "y": 150.0, "z": 0.0}},
            die("U1", "DIE_F", 100.0, "flip_chip"),
            die("U2", "DIE_U", 300.0, "face_up"),
        ],
    }


def export_views_fixture(root: Path, **kwargs):
    """Export the fixture to ``root/out`` with the system top at ``root``."""
    die_views = write_views_fixture(root)
    (root / "top.v").write_text(VIEWS_TOP)
    kwargs.setdefault("top_verilog", root / "top.v")
    return chiplet2dbx.export_3dblox(views_assembly(), root / "out",
                                     die_views=die_views, **kwargs)


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
    with pytest.raises(ExportError, match="flip_chip"):
        chiplet2dbx.render_3dbx(assembly, "unit_demo.3dbv")


def test_die_anchor_bbox_center_fails():
    """A die declaring bbox_center can't be exported with the gds_origin
    die-local bump transform; hard error, not a silent misplacement."""
    assembly = base_assembly()
    assembly["components"][1]["anchor"] = "bbox_center"
    with pytest.raises(ExportError, match="bbox_center"):
        chiplet2dbx.render_3dbx(assembly, "unit_demo.3dbv")


def test_die_anchor_gds_origin_ok():
    """Explicit gds_origin exports normally (base_assembly dies declare it)."""
    assembly = base_assembly()
    assembly["components"][1]["anchor"] = "gds_origin"
    chiplet2dbx.render_3dbx(assembly, "unit_demo.3dbv")


def test_die_anchor_absent_fails():
    """An absent anchor defaults to bbox_center per the contract, which this
    exporter cannot handle; hard error, not a silent gds_origin assumption."""
    assembly = base_assembly()
    del assembly["components"][1]["anchor"]
    with pytest.raises(ExportError, match="anchor"):
        chiplet2dbx.render_3dbx(assembly, "unit_demo.3dbv")


@pytest.mark.parametrize("rot", ["90deg", None, [0, 0, 90]])
def test_malformed_rotation_is_export_error(rot):
    """A non-numeric rotation.z, or a non-dict rotation container (list),
    surfaces as a clean ExportError (exit 1), not a raw traceback; the exporter
    keeps its 0/1 contract (no exit-2 tier)."""
    assembly = base_assembly()
    assembly["components"][1]["rotation"] = (
        rot if isinstance(rot, list) else {"z": rot})
    with pytest.raises(ExportError, match="rotation"):
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


# ------------------------------------------------- attachment surface split

def test_attachment_surface_z_drives_mount_and_substrate():
    """A declared attachment_surface_z is the mount reference AND the thin
    substrate def thickness; the physical dimensions.thickness is not modelled."""
    assembly = attach_surface_assembly()
    # Mount surface = attachment_surface_z, so the die z-gap check passes and
    # the export runs end to end.
    dbx = chiplet2dbx.render_3dbx(assembly, "unit_demo.3dbv")
    assert "reference: DIE_A" in dbx
    dbv = chiplet2dbx.render_3dbv(assembly)
    # The substrate is a thin mount plane = 13.83, NOT the 300 um body.
    assert "thickness: 13.83\n" in dbv
    assert "thickness: 300" not in dbv


def test_attachment_surface_z_gap_measured_against_surface():
    """The z-gap assert measures against attachment_surface_z, not thickness."""
    assembly = attach_surface_assembly()
    assembly["components"][1]["position"]["z"] = 13.83 + 25.0 + 1.0  # off by 1 um
    with pytest.raises(ExportError, match="z gap"):
        chiplet2dbx.render_3dbx(assembly, "unit_demo.3dbv")


def test_attachment_surface_z_absent_falls_back_to_thickness():
    """Without attachment_surface_z, dimensions.thickness is the mount ref
    (legacy files); base_assembly's substrate = 10 relies on this fallback."""
    assembly = base_assembly()
    assert "attachment_surface_z" not in assembly["components"][0]
    dbv = chiplet2dbx.render_3dbv(assembly)
    assert "thickness: 10\n" in dbv  # substrate = dimensions.thickness


def test_non_positive_attachment_surface_z_fails():
    assembly = attach_surface_assembly()
    assembly["components"][0]["attachment_surface_z"] = 0.0
    with pytest.raises(ExportError, match="attachment_surface_z"):
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


# ---------------------------------------------------- eda-views records

def _edit_json(path: Path, edit) -> None:
    data = json.loads(path.read_text())
    edit(data)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _rebind(record: Path, rel: str) -> None:
    """Point the record row for ``rel`` at the file's current bytes."""
    def edit(doc):
        for row in doc["views"]:
            if row["file"] == rel:
                row["sha256"] = _sha(record.parent / rel)
    _edit_json(record, edit)


def _edit_pins(die_views, ref: str, edit) -> None:
    """Edit a die's pins view in its record and contract alike, rebinding
    both, so only the check under test can refuse."""
    record, contract = die_views[ref]
    _edit_json(record.parent / "pins" / "pins.json", edit)
    shutil.copyfile(record.parent / "pins" / "pins.json",
                    contract / "pins.json")
    _rebind(record, "pins/pins.json")

    def inventory(doc):
        doc["inventory"]["sha256"] = _sha(contract / "pins.json")
    _edit_json(contract / "die-contract.json", inventory)


def _edit_file(die_views, ref: str, rel: str, old: str, new: str) -> None:
    record = die_views[ref][0]
    path = record.parent / rel
    text = path.read_text()
    assert old in text, (rel, old)
    path.write_text(text.replace(old, new))
    _rebind(record, rel)


def _loaded(die_views, corner=None):
    return {ref: chiplet2dbx.load_views(record, contract, corner)
            for ref, (record, contract) in die_views.items()}


def test_views_golden(tmp_path):
    export_views_fixture(tmp_path)
    out = tmp_path / "out"
    for name in ("views_demo.3dbv", "views_demo.3dbx", "DIE_F.bmap"):
        golden = (GOLDEN_DIR / ("chiplet2dbx_%s.golden" % name)).read_text()
        assert (out / name).read_text() == golden, (name, REGOLDEN_HINT)


def test_views_bmap_is_the_def_frame(tmp_path):
    """Bumps sit at the pad centres in the DEF frame (placement + half the
    master), never at the --pins convention of outline centre + pin, and the
    flip moves into the orientation instead of the coordinates."""
    dbv, dbx, extra = export_views_fixture(tmp_path)
    bmap = (tmp_path / "out" / "DIE_F.bmap").read_text()
    assert "PAD_A PADX 15 25 a a\n" in bmap
    assert "PAD_V PADX 75 55 - -\n" in bmap
    assert "PAD_A PADX 65 " not in bmap  # 100/2 + 15
    assert "PAD_A PADX 85 " not in bmap  # mirrored in x
    text = dbx.read_text()
    assert "  U1:\n    loc: [50, 110]\n    z: 35\n    orient: MZ_MY\n" in text
    assert "  U2:\n    loc: [250, 110]\n    z: 35\n    orient: R0\n" in text
    assert "top: U1.regions.front" in text
    assert "top: U2.regions.back" in text


def test_views_pad_off_its_def_placement_refuses(tmp_path):
    die_views = write_views_fixture(tmp_path)
    _edit_file(die_views, "U1", "def/DIE_F.def",
               "PAD_A PADX + FIXED ( 10000 20000 )",
               "PAD_A PADX + FIXED ( 10001 20000 )")
    with pytest.raises(ExportError, match="PAD_A is centred at"):
        _loaded(die_views)


def test_views_name_record_files_in_place(tmp_path):
    dbv, dbx, extra = export_views_fixture(tmp_path)
    text = dbv.read_text()
    assert "APR_tech_file: [../records/U1/tech_lef/fix_tech.lef]" in text
    assert "DEF_file: ../records/U2/def/DIE_U.def\n" in text
    assert "verilog_file: ../top.v\n" in dbx.read_text()
    # Only the substrate gets a generated technology LEF; no bump LEF.
    assert sorted(p.name for p in extra) == [
        "DIE_F.bmap", "DIE_U.bmap", "subst.lef"]


def test_views_liberty_listed_once(tmp_path):
    """The target does not deduplicate Liberty, so a file both records name
    is listed once, on the first def in name order."""
    dbv, _, _ = export_views_fixture(tmp_path)
    text = dbv.read_text()
    assert text.count("liberty_file:") == 1
    assert "liberty_file: [../records/U1/liberty/fix_typ.lib]" in text
    assert "fix_slow.lib" not in text


def test_views_corner(tmp_path):
    die_views = write_views_fixture(tmp_path)
    _edit_json(die_views["U2"][0],
               lambda doc: doc.update(default_corner="slow"))
    with pytest.raises(ExportError, match="name one with --corner"):
        chiplet2dbx.export_3dblox(views_assembly(), tmp_path / "out",
                                  die_views=die_views)
    dbv, _, _ = chiplet2dbx.export_3dblox(
        views_assembly(), tmp_path / "out", die_views=die_views,
        corner="slow")
    assert "liberty_file: [../records/U1/liberty/fix_slow.lib]" in dbv.read_text()
    with pytest.raises(ExportError, match="has no corner 'fast'"):
        chiplet2dbx.export_3dblox(views_assembly(), tmp_path / "out",
                                  die_views=die_views, corner="fast")


def test_views_options_need_a_record(tmp_path):
    for kwargs in ({"corner": "typ"}, {"top_verilog": tmp_path / "top.v"}):
        with pytest.raises(ExportError, match="at least one"):
            chiplet2dbx.export_3dblox(base_assembly(), tmp_path, **kwargs)
    with pytest.raises(ExportError, match="pair"):
        chiplet2dbx.export_3dblox(views_assembly(), tmp_path,
                                  die_views={"U1": "eda-views.json"})


def test_views_path_must_be_plain(tmp_path):
    with pytest.raises(ExportError, match="cannot name"):
        chiplet2dbx._view_path(tmp_path / "a b" / "x.lef", tmp_path)


def _schema(dv):
    _edit_json(dv["U1"][0], lambda doc: doc.update(schema="other/v1"))


def _tampered(dv):
    path = dv["U1"][0].parent / "lef" / "fix_pads.lef"
    path.write_text(path.read_text() + "# edited\n")


def _dotdot(dv):
    def edit(doc):
        doc["views"][2]["file"] = "../U2/lef/fix_pads.lef"
    _edit_json(dv["U1"][0], edit)


def _escape(dv):
    path = dv["U1"][0].parent / "lef" / "fix_pads.lef"
    path.unlink()
    path.symlink_to(dv["U2"][0].parent / "lef" / "fix_pads.lef")


def _no_def(dv):
    _edit_json(dv["U1"][0], lambda doc: doc["views"].pop(0))


def _two_tech(dv):
    record = dv["U1"][0]
    shutil.copyfile(record.parent / "tech_lef" / "fix_tech.lef",
                    record.parent / "tech_lef" / "other.lef")
    def edit(doc):
        row = dict(doc["views"][1], file="tech_lef/other.lef")
        doc["views"].append(row)
    _edit_json(record, edit)


def _corners_text(dv):
    def edit(doc):
        doc["views"][1]["corners"] = "typ"
    _edit_json(dv["U1"][0], edit)


def _frame(dv):
    _edit_pins(dv, "U1", lambda pins: pins.update(coordinate_frame="GDS"))


def _pins_major(dv):
    _edit_pins(dv, "U1", lambda pins: pins.update(version="2.0"))


def _pins_units(dv):
    _edit_pins(dv, "U1", lambda pins: pins.update(dbu_um=0.01))


def _pin_row(**change):
    def mutate(dv):
        _edit_pins(dv, "U1", lambda pins: pins["pins"][0].update(change))
    return mutate


def _off_net(dv):
    _edit_file(dv, "U1", "def/DIE_F.def", "( PAD_A pad ) ", "")


def _def_edit(old, new):
    def mutate(dv):
        _edit_file(dv, "U1", "def/DIE_F.def", old, new)
    return mutate


def _contract(edit):
    def mutate(dv):
        _edit_json(dv["U1"][1] / "die-contract.json", edit)
    return mutate


def _record(edit):
    def mutate(dv):
        _edit_json(dv["U1"][0], edit)
    return mutate


def _two_pads_one_port(dv):
    """PAD_V joins net a and claims port a: the target binds a port to one
    bump, so the second pad would take it silently."""
    _edit_file(dv, "U1", "def/DIE_F.def", "( PAD_A pad ) ",
               "( PAD_A pad ) ( PAD_V pad ) ")
    _edit_pins(dv, "U1", lambda pins: pins["pins"][1].update(
        port="a", net="a", role="SIGNAL"))


def _pad_twice(dv):
    _edit_pins(dv, "U1", lambda pins: pins["pins"].append(pins["pins"][1]))


def _two_pin_master(dv):
    _edit_file(dv, "U1", "lef/fix_pads.lef", "  END pad\n",
               "  END pad\n  PIN other\n    DIRECTION INOUT ;\n  END other\n")


def _pin_named_like_master(dv):
    """A first PIN named like its macro must not end the macro early: the
    second PIN still counts."""
    _edit_file(dv, "U1", "lef/fix_pads.lef", "  PIN pad\n",
               "  PIN PADX\n    DIRECTION INOUT ;\n  END PADX\n  PIN pad\n")


def _half_dbu(dv):
    """An odd-DBU master puts PAD_A's centre between two DBU, where the DEF
    check alone would pass it."""
    _edit_file(dv, "U1", "lef/fix_pads.lef", "SIZE 10 BY 10",
               "SIZE 10.001 BY 10")
    _pin_row(center_x_dbu=15000.5)(dv)


def _half_dbu_y(dv):
    _edit_file(dv, "U1", "lef/fix_pads.lef", "SIZE 10 BY 10",
               "SIZE 10 BY 10.001")
    _pin_row(center_y_dbu=25000.5)(dv)


def _add_lef(dv, ref: str, rel: str, old: str = "", new: str = "") -> None:
    """Add a lef row ``rel`` holding the pad LEF with ``old`` replaced."""
    record = dv[ref][0]
    (record.parent / rel).write_text(VIEWS_PAD_LEF.replace(old, new))
    _edit_json(record, lambda doc: doc["views"].append(
        {"kind": "lef", "file": rel, "sha256": _sha(record.parent / rel),
         "status": "recorded"}))


def _unbound_digests(dv):
    """Absent digests on both sides must not compare equal."""
    _record(lambda doc: doc["bound_to"].pop("layout_sha256"))(dv)
    _contract(lambda doc: doc["die"].pop("layout_sha256"))(dv)


@pytest.mark.parametrize("mutate, message", [
    pytest.param(_schema, "is not a chiplet-flow-eda-views/v1 record",
                 id="schema"),
    pytest.param(_tampered, "is not the bytes its row binds", id="tampered"),
    pytest.param(_dotdot, "not a relative path inside", id="dotdot"),
    pytest.param(_escape, "which is not a file inside", id="symlink-escape"),
    pytest.param(_no_def, "has 0 def rows at corner typ", id="no-def"),
    pytest.param(_two_tech, "has 2 tech_lef rows at corner typ",
                 id="two-tech-lef"),
    pytest.param(_corners_text, "corners that are not a list",
                 id="corners-text"),
    pytest.param(_frame, "not version 1.x in the frame", id="pins-frame"),
    pytest.param(_pins_major, "not version 1.x in the frame",
                 id="pins-major"),
    pytest.param(_pins_units, "not in the DEF's units", id="pins-units"),
    pytest.param(_pin_row(role="ANALOG"), "has role 'ANALOG'", id="role"),
    pytest.param(_pin_row(name="PAD_Z"), "PAD_Z is not a DEF component",
                 id="unknown-pad"),
    pytest.param(_pin_row(port="zz"), "port zz is not a DEF pin on net a",
                 id="port-not-pin"),
    pytest.param(_pin_row(net="VDD", port="VDD"), "PAD_A is not on net VDD",
                 id="pad-on-other-net"),
    pytest.param(_off_net, "PAD_A is not on net a", id="def-pad-off-net"),
    pytest.param(_def_edit("( 10000 20000 ) N", "( 10000 20000 ) S"),
                 "PAD_A is placed S in the DEF", id="pad-turned-s"),
    pytest.param(_def_edit("( 10000 20000 ) N", "( 10000 20000 ) E"),
                 "PAD_A is placed E in the DEF", id="pad-turned-e"),
    pytest.param(_two_pads_one_port, "PAD_V: port a already has a pad",
                 id="two-pads-one-port"),
    pytest.param(_pad_twice, "lists no pads, or a pad twice", id="pad-twice"),
    pytest.param(_two_pin_master, "is a PADX with 2 pins",
                 id="two-pin-master"),
    pytest.param(_pin_named_like_master, "is a PADX with 2 pins",
                 id="pin-named-like-master"),
    pytest.param(_def_edit("PAD_A PADX", "PAD_A PADY"),
                 "is a PADY, which no LEF row sizes", id="unsized-master"),
    pytest.param(_def_edit("UNITS DISTANCE MICRONS 1000 ;\n", ""),
                 "states no UNITS or no rectangular DIEAREA", id="no-units"),
    pytest.param(_def_edit("DIEAREA", "# DIEAREA"),
                 "states no UNITS or no rectangular DIEAREA", id="no-diearea"),
    pytest.param(_pin_row(center_x_dbu="x"), "has a malformed pin",
                 id="pin-text-centre"),
    pytest.param(lambda dv: _edit_pins(dv, "U1", lambda pins: pins.update(
        dbu_um="0.001")), "not in the DEF's units", id="pins-units-text"),
    pytest.param(_record(lambda doc: doc.update(views={})), "lists no views",
                 id="views-not-list"),
    pytest.param(_record(lambda doc: doc.update(corners=[1],
                                                default_corner=1)),
                 "has no corner 1", id="corner-not-text"),
    pytest.param(_record(lambda doc: doc.update(bound_to=[])),
                 "bound_to of", id="bound-to-not-object"),
    pytest.param(_record(lambda doc: doc["bound_to"].update(
        layout_sha256="0" * 64)), "is not bound to the layout",
                 id="bound-to-other-layout"),
    pytest.param(_record(lambda doc: doc["bound_to"].update(
        build_record_sha256="0" * 64)), "is not bound to the layout",
                 id="bound-to-other-build-record"),
    pytest.param(_record(lambda doc: doc.update(corners="typ")),
                 "has no corner 'typ'", id="corners-not-list"),
    pytest.param(_record(lambda doc: doc["views"][0].update(
        status="qualified")), "the def row has status 'qualified'",
                 id="row-status"),
    pytest.param(lambda dv: _edit_pins(dv, "U1", lambda pins: pins.update(
        pins=[])), "lists no pads, or a pad twice", id="no-pads"),
    pytest.param(_def_edit("( 10000 20000 ) N", "( 10000 20001 ) N"),
                 "PAD_A is centred at (15000, 25000) DBU, and the DEF",
                 id="pad-off-in-y"),
    pytest.param(_half_dbu, "PAD_A is centred at (15000.5, 25000) DBU, "
                 "between two DBU", id="half-dbu-centre"),
    pytest.param(_half_dbu_y, "PAD_A is centred at (15000, 25000.5) DBU, "
                 "between two DBU", id="half-dbu-centre-y"),
    pytest.param(_pin_row(center_x_dbu=float("nan")),
                 "has a malformed pin (PAD_A is centred at (nan, 25000.0))",
                 id="nan-centre"),
    pytest.param(_pin_row(center_y_dbu=float("inf")),
                 "has a malformed pin (PAD_A is centred at (15000.0, inf))",
                 id="infinite-centre"),
    pytest.param(lambda dv: _add_lef(dv, "U1", "lef/more_pads.lef",
                                     "RECT 0 0 10 10", "RECT 0 0 5 5"),
                 "MACRO PADX has one body in fix_pads.lef and another in "
                 "more_pads.lef", id="macro-two-bodies"),
    pytest.param(_unbound_digests, "is not a non-empty string",
                 id="unbound-digests"),
    pytest.param(_record(lambda doc: doc.pop("design")),
                 "the design of", id="no-design"),
    pytest.param(_contract(lambda doc: doc.update(die="DIE_F")),
                 "the die of the contract", id="contract-die-not-object"),
    pytest.param(_contract(lambda doc: doc["inventory"].update(kind="csv")),
                 "lists no pins.json", id="contract-inventory-kind"),
])
def test_views_record_refusals(tmp_path, mutate, message):
    die_views = write_views_fixture(tmp_path)
    mutate(die_views)
    with pytest.raises(ExportError, match=re.escape(message)):
        _loaded(die_views)


def test_views_contract_binding(tmp_path):
    die_views = write_views_fixture(tmp_path)
    (r1, c1), (r2, c2) = die_views["U1"], die_views["U2"]
    with pytest.raises(ExportError, match="is not bound to the layout"):
        chiplet2dbx.load_views(r1, c2)
    (c1 / "pins.json").write_text((c1 / "pins.json").read_text() + " ")
    with pytest.raises(ExportError, match="is not the bytes the contract"):
        chiplet2dbx.load_views(r1, c1)
    _edit_json(c1 / "die-contract.json", lambda doc: doc["inventory"].update(
        sha256=_sha(c1 / "pins.json")))
    with pytest.raises(ExportError, match="the pin list of"):
        chiplet2dbx.load_views(r1, c1)
    _edit_json(c2 / "die-contract.json",
               lambda doc: doc.update(version="2.0"))
    with pytest.raises(ExportError, match="is not a die-output-contract"):
        chiplet2dbx.load_views(r2, c2)


def test_views_macro_bodies_across_dies(tmp_path):
    """The target binds a master to the first library that defines it, so a
    MACRO name with two bodies is refused across dies too; one body under two
    file names, or laid out differently, reads."""
    die_views = write_views_fixture(tmp_path)
    _edit_json(die_views["U2"][0], lambda doc: doc["views"].pop(2))
    _add_lef(die_views, "U2", "lef/fix_pads_b.lef", "RECT 0 0 10 10",
             "RECT 0 0 5 5")
    with pytest.raises(ExportError, match="MACRO PADX has one body in "
                       "fix_pads.lef of DIE_F and another in fix_pads_b.lef "
                       "of DIE_U"):
        _rendered(views_assembly(), _loaded(die_views))
    same = write_views_fixture(tmp_path / "same")
    _edit_json(same["U2"][0], lambda doc: doc["views"].pop(2))
    _add_lef(same, "U2", "lef/fix_pads_b.lef", "  SIZE 10 BY 10",
             "\n  # laid out apart\n\tSIZE   10 BY 10  ")
    _add_lef(same, "U1", "lef/more_pads.lef", "RECT 0 0 10 10",
             "RECT 0 0 10 10 ; #same rect")
    assert "fix_pads_b.lef" in _rendered(views_assembly(), _loaded(same))


def test_views_lines(tmp_path):
    """One line per die: the kinds read and the unqualified files among the
    rows used at the corner, or absent for a die without a record."""
    die_views = write_views_fixture(tmp_path)

    def unqualify(doc):
        for row in doc["views"][3:5]:  # the typ and slow Liberty rows
            row.update(status="unqualified", limit="a test limit")
    _edit_json(die_views["U1"][0], unqualify)
    loaded = _loaded({"U1": die_views["U1"]})
    assembly = views_assembly()
    assembly["components"][2]["anchor"] = "gds_origin"  # a black-box U2
    assert chiplet2dbx._views_lines(assembly, loaded) == [
        "U1: eda views: def, tech_lef, pins, lef, liberty; "
        "unqualified: liberty/fix_typ.lib",
        "U2: eda views: absent"]


def test_views_record_describes_the_contract_design(tmp_path):
    die_views = write_views_fixture(tmp_path)
    _edit_json(die_views["U1"][0], lambda doc: doc.update(design="DIE_X"))
    with pytest.raises(ExportError, match="describes DIE_X"):
        _loaded(die_views)


def _rendered(assembly, loaded):
    return chiplet2dbx.render_3dbv(assembly, die_views=loaded)


def test_views_die_checks(tmp_path):
    loaded = _loaded(write_views_fixture(tmp_path))
    assembly = views_assembly()
    assembly["components"][1]["top_cell"] = "DIE_X"
    with pytest.raises(ExportError, match="has top_cell 'DIE_X'"):
        _rendered(assembly, loaded)
    assembly = views_assembly()
    assembly["components"][1]["dimensions"]["width"] = 120.0
    with pytest.raises(ExportError, match="same outline"):
        _rendered(assembly, loaded)
    assembly = views_assembly()
    assembly["components"][1]["rotation"]["z"] = 90.0
    with pytest.raises(ExportError, match="quarter turn"):
        _rendered(assembly, loaded)
    assembly = views_assembly()
    assembly["components"][1]["anchor"] = "gds_origin"
    with pytest.raises(ExportError, match="must declare anchor: bbox_center"):
        _rendered(assembly, loaded)
    with pytest.raises(ExportError, match="both a pin list and"):
        chiplet2dbx.render_3dbv(views_assembly(), die_views=loaded,
                                die_pins={"U1": d1_pins()})
    with pytest.raises(ExportError, match="unknown die refs: U9"):
        _rendered(views_assembly(), dict(loaded, U9=loaded["U1"]))


def test_views_contract_bbox_and_units(tmp_path):
    for bbox in ([0, 0, 100, 90], ["0", "0", "a", "80"], "0 0 100 80"):
        die_views = write_views_fixture(tmp_path / str(len(str(bbox))))
        _edit_json(die_views["U1"][1] / "die-contract.json",
                   lambda doc: doc["die"].update(bbox_um=bbox))
        with pytest.raises(ExportError, match="same outline"):
            _rendered(views_assembly(), _loaded(die_views))
    die_views = write_views_fixture(tmp_path / "area")
    _edit_file(die_views, "U1", "def/DIE_F.def", "( 100000 80000 )",
               "( 100000 90000 )")
    with pytest.raises(ExportError, match="DIEAREA is"):
        _rendered(views_assembly(), _loaded(die_views))
    die_views = write_views_fixture(tmp_path / "units")
    _edit_file(die_views, "U2", "tech_lef/fix_tech.lef",
               "DATABASE MICRONS 1000", "DATABASE MICRONS 2000")
    with pytest.raises(ExportError, match="must be equal"):
        _rendered(views_assembly(), _loaded(die_views))


def test_views_same_stem_refused(tmp_path):
    """The target loads one file per stem and skips the rest silently."""
    die_views = write_views_fixture(tmp_path)
    _edit_file(die_views, "U2", "lef/fix_pads.lef", "END LIBRARY",
               "# another build\nEND LIBRARY")
    with pytest.raises(ExportError, match="share the stem 'fix_pads'"):
        _rendered(views_assembly(), _loaded(die_views))
    loaded = _loaded(write_views_fixture(tmp_path / "gen"))
    assembly = views_assembly()
    assembly["components"][0]["technology"] = "fix_tech"
    assembly["technologies"]["fix_tech"] = {"dbu": 0.001}
    with pytest.raises(ExportError, match="share the stem 'fix_tech'"):
        _rendered(assembly, loaded)
    die_views = write_views_fixture(tmp_path / "lib")
    _edit_file(die_views, "U2", "liberty/fix_typ.lib", "area : 100",
               "area : 101")
    with pytest.raises(ExportError, match="named fix_typ.lib"):
        _rendered(views_assembly(), _loaded(die_views))


@pytest.mark.parametrize("old, new, message", [
    ("module views_demo", "module other", "defines modules ['other']"),
    ("endmodule\n", "endmodule\nmodule extra;\nendmodule\n",
     "defines modules ['views_demo', 'extra']"),
    ("DIE_U U2 (", "DIE_U U9 (", "instantiates U9"),
    ("DIE_U U2 (", "DIE_U U1 (", "instantiates U1"),
    ("DIE_U U2 (", "DIE_F U2 (", "makes U2 a DIE_F"),
    (".b(n_ab)", ".VSS(n_ab)", "connects U2.VSS"),
    (".b(n_ab)", ".c(n_ab)", "connects U2.c"),
    ("DIE_U U2 (", "DIE_U #(.P(1)) U2 (", "is not a generated system top"),
    (".b(n_ab)", "n_ab", "is not a generated system top"),
    ("    wire n_ab;\n", "    assign ext = n_ab;\n",
     "is not a generated system top"),
    ("endmodule\n", "endmodule\nendmodule\n", "close with one endmodule"),
])
def test_top_verilog_checks(tmp_path, old, new, message):
    assert old in VIEWS_TOP
    top = tmp_path / "bad_top.v"
    top.write_text(VIEWS_TOP.replace(old, new))
    with pytest.raises(ExportError, match=re.escape(message)):
        export_views_fixture(tmp_path, top_verilog=top)


def test_top_verilog_needs_a_die(tmp_path):
    top = tmp_path / "empty_top.v"
    top.write_text("module views_demo;\nendmodule\n")
    with pytest.raises(ExportError, match="instantiates no die"):
        export_views_fixture(tmp_path, top_verilog=top)


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
    # Without --views stdout lists the written files and nothing else.
    lines = proc.stdout.splitlines()
    assert {Path(line).name for line in lines} >= {"unit_demo.3dbv",
                                                  "unit_demo.3dbx"}
    assert all(Path(line).is_file() for line in lines)


def test_cli_views_roundtrip(tmp_path):
    die_views = write_views_fixture(tmp_path)
    (tmp_path / "top.v").write_text(VIEWS_TOP)
    chiplet = tmp_path / "views_demo.chiplet"
    chiplet2dbx.cfio.dump(views_assembly(), chiplet)
    args = [sys.executable, str(SCRIPT), "--chiplet", str(chiplet),
            "--out-dir", str(tmp_path / "out"),
            "--top-verilog", str(tmp_path / "top.v")]
    for ref, (record, contract) in die_views.items():
        args += ["--views", "%s=%s" % (ref, record),
                 "--contract", "%s=%s" % (ref, contract)]
    proc = subprocess.run(args, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    golden = (GOLDEN_DIR / "chiplet2dbx_views_demo.3dbv.golden").read_text()
    assert (tmp_path / "out" / "views_demo.3dbv").read_text() == golden
    assert proc.stdout.splitlines()[-2:] == [
        "%s: eda views: def, tech_lef, pins, lef, liberty; unqualified: none"
        % ref for ref in ("U1", "U2")]
    proc = subprocess.run(args[:-2], capture_output=True, text=True)
    assert proc.returncode == 1
    assert "unpaired: U2" in proc.stderr


def test_cli_reports_errors(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--chiplet",
         str(tmp_path / "missing.chiplet")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1
    assert "chiplet2dbx: error:" in proc.stderr


# ------------------------------------------------- vendored reader guard
#
# The vendored reader is pinned by DECLARATION: vendor/README.md names the
# chiplet-spec commit the copy was taken from, and that declaration is the
# contract. The guard therefore compares the vendored bytes against THAT
# commit's blob, read out of a discoverable chiplet-spec repository with git,
# and never against the neighbour's working tree. A sibling checkout whose HEAD
# has moved on is not our contract and must not turn this red; re-vendoring
# means moving the pin and the copy together.

SPEC_REFERENCE_PATH = "reference/python/chiplet_format_io/__init__.py"

# The pin as vendor/README.md writes it: "... at `0d850ca` ...". The backticks
# delimit the token, so a 7 to 40 character all-hex match is a whole word.
_PIN_RE = re.compile(r"\bat `([0-9a-fA-F]{7,40})`")

# git's stderr when the repository genuinely does not have the pinned COMMIT.
# A shallow or partial clone lands here too, which is why that one case is a
# capability skip and not a failure. It is the only skip git can produce: once
# the commit is present, everything after it is the oracle's own business.
_ABSENT_OBJECT_MARKERS = (
    "not a valid object name",
    "bad object",
    "unknown revision",
    "does not exist",
)
_NOT_A_REPO_MARKER = "not a git repository"


class PinVerdict(NamedTuple):
    """Outcome of the declared-pin cross-check.

    status:
      match        the vendored bytes are the pinned blob
      mismatch     they differ: drift, or a pin moved without a re-copy
      skip         no chiplet-spec repository, or it does not carry the
                   pinned commit
      error        a git failure that is not commit absence (no git binary,
                   denied permissions, safe.directory, a pinned commit that
                   does not carry the reference file), or a missing vendored
                   copy
      parse_error  vendor/README.md does not declare exactly one pin

    Only ``skip`` is a capability gap. Everything else is the oracle telling
    the truth about the repository or about itself, so it fails.
    """
    status: str
    detail: str
    pin: Optional[str] = None


def _declared_pins(readme_path: Path) -> List[str]:
    """Every commit pin vendor/README.md declares, in file order."""
    return _PIN_RE.findall(readme_path.read_text())


def _git(spec_root: Path, args: List[str]) -> subprocess.CompletedProcess:
    """Run git in spec_root. Bytes in, bytes out: the blob must reach the
    comparison undecoded. Never shell=True, every argument is its own token."""
    return subprocess.run(["git", "-C", str(spec_root)] + args,
                          capture_output=True)


def _git_stderr(proc: subprocess.CompletedProcess) -> str:
    return proc.stderr.decode("utf-8", "replace").strip()


def _commit_absence_or_error(proc, pin: str, spec_root: Path) -> PinVerdict:
    """Verdict for a failed commit-existence probe. A repository that simply
    does not have the commit is a capability gap; anything else is not."""
    detail = _git_stderr(proc)
    if proc.returncode == 128 and any(m in detail.lower()
                                      for m in _ABSENT_OBJECT_MARKERS):
        return PinVerdict(
            "skip",
            "%s does not carry the declared pin %s: %s"
            % (spec_root, pin, detail), pin)
    return PinVerdict(
        "error",
        "git could not look up the declared pin %s in %s and this is not an "
        "absent commit, so the oracle is broken rather than unavailable: "
        "exit %d, %s" % (pin, spec_root, proc.returncode, detail), pin)


def _vendored_matches_declared_pin(adk_root, spec_root) -> PinVerdict:
    """Cross-check the vendored reader against the pin vendor/README.md
    declares, resolved through git in spec_root.

    Both roots are arguments so the sensitivity tests below can drive this over
    a synthetic repository instead of monkeypatching the real paths.
    """
    adk_root, spec_root = Path(adk_root), Path(spec_root)

    readme = adk_root / "vendor" / "README.md"
    if not readme.is_file():
        return PinVerdict("parse_error", "no vendoring declaration at %s" % readme)
    pins = _declared_pins(readme)
    if len(pins) != 1:
        return PinVerdict(
            "parse_error",
            "%s must declare exactly one vendoring pin in the \"at `<commit>`\" "
            "form; found %d %s. That README is the single declared authority "
            "for what is vendored." % (readme, len(pins), pins))
    pin = pins[0]

    vendored = adk_root / "vendor" / "chiplet_format_io" / "__init__.py"
    if not vendored.is_file():
        return PinVerdict("error", "vendored reader missing: %s" % vendored, pin)

    if not spec_root.is_dir():
        return PinVerdict(
            "skip",
            "no chiplet-spec repository at %s to resolve the declared pin %s "
            "in (set CHIPLET_SPEC_ROOT)" % (spec_root, pin), pin)

    try:
        probe = _git(spec_root, ["rev-parse", "--git-dir"])
        if probe.returncode != 0:
            detail = _git_stderr(probe)
            if _NOT_A_REPO_MARKER in detail.lower():
                return PinVerdict(
                    "skip",
                    "%s is not a git repository, so the declared pin %s cannot "
                    "be resolved (set CHIPLET_SPEC_ROOT)" % (spec_root, pin), pin)
            return PinVerdict(
                "error",
                "git failed in %s while looking for the declared pin %s: "
                "exit %d, %s" % (spec_root, pin, probe.returncode, detail), pin)

        have = _git(spec_root, ["cat-file", "-e", pin + "^{commit}"])
        if have.returncode != 0:
            return _commit_absence_or_error(have, pin, spec_root)

        # Past this point the repository demonstrably has the commit, so a
        # failure to read the reference file out of it is never a capability
        # gap: either the declared pin names a commit without the file, or the
        # repository cannot read its own blob. Both are failures.
        show = _git(spec_root, ["show", "%s:%s" % (pin, SPEC_REFERENCE_PATH)])
        if show.returncode != 0:
            return PinVerdict(
                "error",
                "the declared pin %s exists in %s but %s could not be read "
                "from it: exit %d, %s"
                % (pin, spec_root, SPEC_REFERENCE_PATH, show.returncode,
                   _git_stderr(show)), pin)
    except OSError as exc:
        # No git binary, or it could not be executed. The oracle cannot run,
        # which is a broken gate, not a missing neighbour.
        return PinVerdict(
            "error",
            "cannot run git to resolve the declared pin %s in %s: %s"
            % (pin, spec_root, exc), pin)

    vendored_bytes = vendored.read_bytes()
    if vendored_bytes == show.stdout:
        return PinVerdict(
            "match",
            "vendored reader is byte-identical to %s at the declared pin %s "
            "(%s)" % (SPEC_REFERENCE_PATH, pin, spec_root), pin)
    return PinVerdict(
        "mismatch",
        "vendored chiplet_format_io does not match the declared pin %s (%s in "
        "%s): vendored sha256 %s, pinned sha256 %s. Re-copy verbatim from the "
        "declared pin, or move the pin and the copy together."
        % (pin, SPEC_REFERENCE_PATH, spec_root,
           hashlib.sha256(vendored_bytes).hexdigest(),
           hashlib.sha256(show.stdout).hexdigest()), pin)


def test_vendored_reader_matches_spec_reference():
    """The vendored reader must be the blob vendor/README.md pins, byte for
    byte. The comparison runs through git against the DECLARED commit, so a
    chiplet-spec checkout that has moved ahead neither passes nor fails this;
    only the declaration does."""
    env = os.environ.get("CHIPLET_SPEC_ROOT")
    spec_root = Path(env) if env else ADK_ROOT.parent / "chiplet-spec"
    verdict = _vendored_matches_declared_pin(ADK_ROOT, spec_root)
    if verdict.status == "skip":
        pytest.skip(verdict.detail)
    assert verdict.status == "match", verdict.detail


# ------------------------------------- declared-pin oracle, sensitivity
#
# The oracle above is only worth its green when it can go red for the right
# reasons and stays green for the wrong ones. These build a throwaway
# chiplet-spec repository with git (local, no network) and drive the helper
# over it, so the real repositories are never touched or monkeypatched.

needs_git = pytest.mark.skipif(shutil.which("git") is None,
                               reason="git is needed to build the fixture repo")

_FIXTURE_GIT = ["-c", "user.name=t", "-c", "user.email=t@t",
                "-c", "commit.gpgsign=false"]


def _run_git(root: Path, args: List[str]) -> str:
    proc = subprocess.run(["git", "-C", str(root)] + args,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _spec_repo(root: Path, first: bytes, second: bytes):
    """A throwaway chiplet-spec: the reference reader at two commits, HEAD left
    on the second. Returns both commit ids."""
    reference = root / Path(SPEC_REFERENCE_PATH)
    reference.parent.mkdir(parents=True)
    _run_git(root, ["-c", "init.defaultBranch=main", "init", "-q", "."])
    commits = []
    for content in (first, second):
        reference.write_bytes(content)
        _run_git(root, ["add", "-A"])
        _run_git(root, _FIXTURE_GIT + ["commit", "-q", "-m", "revision"])
        commits.append(_run_git(root, ["rev-parse", "HEAD"]))
    return commits


def _readme(pin: str) -> str:
    return ("Currently vendored: reader 9.9.9, from chiplet-spec `origin/dev` "
            "at `%s` (sha256 `deadbeef...`).\n" % pin)


def _fake_adk(root: Path, vendored: bytes, readme: str) -> Path:
    (root / "vendor" / "chiplet_format_io").mkdir(parents=True)
    (root / "vendor" / "chiplet_format_io" / "__init__.py").write_bytes(vendored)
    (root / "vendor" / "README.md").write_text(readme)
    return root


@needs_git
def test_declared_pin_holds_when_the_spec_repo_moves_on(tmp_path):
    """The regression this rewrite exists for: the neighbour's HEAD moves to a
    new reader release, the declared pin does not, and the oracle stays green."""
    spec = tmp_path / "chiplet-spec"
    spec.mkdir()
    pinned, moved_on = _spec_repo(spec, b"pinned reader\n", b"newer reader\n")
    adk = _fake_adk(tmp_path / "adk", b"pinned reader\n", _readme(pinned))

    verdict = _vendored_matches_declared_pin(adk, spec)

    assert verdict.status == "match", verdict.detail
    assert _run_git(spec, ["rev-parse", "HEAD"]) == moved_on, (
        "the fixture must leave HEAD past the pin, or this proves nothing")


@needs_git
def test_declared_pin_catches_an_edited_vendored_copy(tmp_path):
    """Real drift: somebody edited the copy in place."""
    spec = tmp_path / "chiplet-spec"
    spec.mkdir()
    pinned, _ = _spec_repo(spec, b"pinned reader\n", b"newer reader\n")
    adk = _fake_adk(tmp_path / "adk", b"pinned reader\nlocal edit\n",
                    _readme(pinned))

    verdict = _vendored_matches_declared_pin(adk, spec)

    assert verdict.status == "mismatch", verdict.detail
    assert pinned in verdict.detail
    assert hashlib.sha256(b"pinned reader\n").hexdigest() in verdict.detail
    assert hashlib.sha256(b"pinned reader\nlocal edit\n").hexdigest() in \
        verdict.detail
    assert "Re-copy verbatim" in verdict.detail


@needs_git
def test_declared_pin_catches_a_pin_moved_without_a_recopy(tmp_path):
    """The other half of the contract: moving the declaration alone is drift
    too, so the pin and the copy can only travel together."""
    spec = tmp_path / "chiplet-spec"
    spec.mkdir()
    _, moved_on = _spec_repo(spec, b"pinned reader\n", b"newer reader\n")
    adk = _fake_adk(tmp_path / "adk", b"pinned reader\n", _readme(moved_on))

    verdict = _vendored_matches_declared_pin(adk, spec)

    assert verdict.status == "mismatch", verdict.detail
    assert moved_on in verdict.detail


@needs_git
def test_declared_pin_absent_from_the_repository_skips(tmp_path):
    """A repository that does not carry the pinned commit (a shallow clone, a
    fork) is a capability gap, so the oracle steps aside and says which pin it
    wanted and where it looked."""
    spec = tmp_path / "chiplet-spec"
    spec.mkdir()
    _spec_repo(spec, b"pinned reader\n", b"newer reader\n")
    absent = "0" * 40
    adk = _fake_adk(tmp_path / "adk", b"pinned reader\n", _readme(absent))

    verdict = _vendored_matches_declared_pin(adk, spec)

    assert verdict.status == "skip", verdict.detail
    assert absent in verdict.detail and str(spec) in verdict.detail


@needs_git
def test_declared_pin_commit_without_the_reference_path_is_an_error(tmp_path):
    """The commit is there, the reference file is not: the declaration points
    at something that cannot answer the question, which is the oracle being
    right rather than unavailable. Skipping here would let a bad pin pass as a
    missing capability."""
    spec = tmp_path / "chiplet-spec"
    spec.mkdir()
    _run_git(spec, ["-c", "init.defaultBranch=main", "init", "-q", "."])
    (spec / "README.md").write_text("a chiplet-spec with no reference reader\n")
    _run_git(spec, ["add", "-A"])
    _run_git(spec, _FIXTURE_GIT + ["commit", "-q", "-m", "no reader here"])
    pin = _run_git(spec, ["rev-parse", "HEAD"])
    adk = _fake_adk(tmp_path / "adk", b"pinned reader\n", _readme(pin))

    verdict = _vendored_matches_declared_pin(adk, spec)

    assert verdict.status == "error", verdict.detail
    assert pin in verdict.detail and SPEC_REFERENCE_PATH in verdict.detail


def test_declared_pin_skips_without_a_spec_repository(tmp_path):
    """No checkout at all, and a directory that is not a repository: both are
    the capability gap the guard has always skipped on."""
    adk = _fake_adk(tmp_path / "adk", b"pinned reader\n", _readme("0" * 40))

    gone = _vendored_matches_declared_pin(adk, tmp_path / "nowhere")
    assert gone.status == "skip", gone.detail
    assert str(tmp_path / "nowhere") in gone.detail and "0" * 40 in gone.detail

    plain = tmp_path / "plain"
    plain.mkdir()
    verdict = _vendored_matches_declared_pin(adk, plain)
    assert verdict.status == "skip", verdict.detail
    assert str(plain) in verdict.detail and "0" * 40 in verdict.detail


@needs_git
def test_declared_pin_git_failure_is_not_a_skip(tmp_path):
    """A git failure that is not absence (here a corrupt .git, in the field a
    permissions or safe.directory refusal) must FAIL. Skipping on it is how an
    oracle goes quietly out of service."""
    broken = tmp_path / "chiplet-spec"
    broken.mkdir()
    (broken / ".git").write_text("garbage, not a gitfile\n")
    adk = _fake_adk(tmp_path / "adk", b"pinned reader\n", _readme("0" * 40))

    verdict = _vendored_matches_declared_pin(adk, broken)

    assert verdict.status == "error", verdict.detail
    assert "gitfile" in verdict.detail


@pytest.mark.parametrize("readme, found", [
    ("Currently vendored: reader 9.9.9, from chiplet-spec `origin/dev`.\n", 0),
    (_readme("0" * 40) + _readme("1" * 40), 2),
])
def test_declared_pin_requires_exactly_one_declaration(tmp_path, readme, found):
    """Zero pins or two: the README stopped being an unambiguous authority, so
    the oracle fails instead of guessing which token to trust."""
    adk = _fake_adk(tmp_path / "adk", b"pinned reader\n", readme)

    verdict = _vendored_matches_declared_pin(adk, tmp_path / "nowhere")

    assert verdict.status == "parse_error", verdict.detail
    assert "exactly one" in verdict.detail and str(found) in verdict.detail


# ------------------------------------------------------ live answer-key

@functools.lru_cache(maxsize=None)
def _docker_image_available():
    docker = shutil.which("docker")
    if not docker:
        return False
    probe = subprocess.run([docker, "image", "inspect", DOCKER_IMAGE],
                           capture_output=True)
    return probe.returncode == 0


# A marker, not a skipif: a skipif condition runs at import, so the probe
# started docker while the module was collected, even in a run that selects
# none of these cases. The fixture below probes when a marked case sets up.
needs_openroad_image = pytest.mark.openroad_image


@pytest.fixture(autouse=True)
def _skip_without_openroad_image(request):
    if (request.node.get_closest_marker("openroad_image")
            and not _docker_image_available()):
        pytest.skip("docker or the pinned %s image is not available" % DOCKER_IMAGE)

ACCEPT_TCL = """\
read_3dbx %s
puts "READ_3DBX_OK"
check_3dblox
puts "CHECK_3DBLOX_OK"
exit
"""


def _run_openroad(workdir: Path, dbx_name: str, ok: bool = True) -> str:
    tcl = workdir / "accept.tcl"
    tcl.write_text(ACCEPT_TCL % dbx_name)
    # The image runs as an unprivileged user; pytest tmp dirs are 0700, so
    # run with the host uid to keep the mounted workdir readable. The caps
    # keep a runaway run from taking a shared host.
    proc = subprocess.run(
        ["docker", "run", "--rm",
         "--user", "%d:%d" % (os.getuid(), os.getgid()),
         "--cpus=2", "--memory=6g", "--memory-swap=6g", "--network=none",
         "-v", "%s:%s" % (workdir, workdir), "-w", str(workdir),
         DOCKER_IMAGE, "-exit", str(tcl)],
        capture_output=True, text=True, timeout=300,
    )
    output = proc.stdout + proc.stderr
    if ok:
        assert proc.returncode == 0, output
    return output


def _assert_clean(output: str, allowed=()) -> None:
    assert "READ_3DBX_OK" in output
    assert "CHECK_3DBLOX_OK" in output
    for line in output.splitlines():
        if "[WARNING" in line or "[ERROR" in line:
            assert any(code in line for code in allowed), output


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


# The live record tests mount tmp_path, which holds the records, the out dir
# and the system top, since the .3dbv names the record files in place.

@needs_openroad_image
def test_live_check_3dblox_with_views(tmp_path):
    """Both record dies load with their DEF, LEF and bump maps, the system
    top is read with each instance on its die module, and the linter is
    clean. OpenSTA black-boxes each die module of the top once (STA-0198);
    nothing else may warn. The target links no STA network after read_3dbx
    (STA-1570 on any query), so the black-box lines are what shows the
    instances of the top."""
    dbv, dbx, _ = export_views_fixture(tmp_path)
    output = _run_openroad(tmp_path, "out/" + dbx.name)
    _assert_clean(output, allowed=("STA-0198",))
    assert output.count("STA-0198") == 2, output
    for module, inst in (("DIE_F", "U1"), ("DIE_U", "U2")):
        assert ("module %s not found. Creating black box for %s."
                % (module, inst)) in output, output


@needs_openroad_image
def test_live_views_def_is_read(tmp_path):
    """Negative control: a DEF master no LEF defines must fail the read, so
    the green run above did read the record's DEF and LEF."""
    dbv, dbx, _ = export_views_fixture(tmp_path)
    path = tmp_path / "records" / "U1" / "def" / "DIE_F.def"
    path.write_text(path.read_text().replace("PAD_A PADX", "PAD_A NOPE"))
    output = _run_openroad(tmp_path, "out/" + dbx.name, ok=False)
    assert "ODB-0092" in output, output


@needs_openroad_image
def test_live_views_bump_moves_a_fixed_pad(tmp_path):
    """Negative control: a bump 1 um off its DEF pad would move a FIXED
    instance, which the target refuses, so the bump map is in the DEF frame."""
    dbv, dbx, _ = export_views_fixture(tmp_path)
    bmap = tmp_path / "out" / "DIE_F.bmap"
    bmap.write_text(bmap.read_text().replace("PAD_A PADX 15 25",
                                             "PAD_A PADX 16 25"))
    output = _run_openroad(tmp_path, "out/" + dbx.name, ok=False)
    assert "ODB-0359" in output, output


@needs_openroad_image
def test_live_views_top_is_read(tmp_path):
    """Negative control: a top without the Design's module must fail the
    read, so the green run above did read the system top."""
    dbv, dbx, _ = export_views_fixture(tmp_path)
    top = tmp_path / "top.v"
    top.write_text(top.read_text().replace("module views_demo",
                                           "module renamed"))
    output = _run_openroad(tmp_path, "out/" + dbx.name, ok=False)
    assert "STA-1390" in output, output
