# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Tests for checks/pads_vs_pillars.py.

Everything is synthesized on the fly: `.chiplet` assemblies, pillar-manifest
sidecars, gds_to_kicad-style pin lists, and (for the --gds-pads path) a tiny
die GDS built with klayout.db on the chiplet_pads.json layers. Expected
global pad positions are computed by an independent reference transform in
this module, so a producer-frame regression in the tool cannot hide behind
its own math.
"""

import json
import math
import sys
from pathlib import Path

import pytest

# The check imports chiplet2dbx, whose vendored chiplet_format_io reader
# needs PyYAML. Skip the module (not error the collection) where missing.
yaml = pytest.importorskip("yaml", reason="pads_vs_pillars needs PyYAML")

ADK_ROOT = Path(__file__).resolve().parents[1]
if str(ADK_ROOT / "checks") not in sys.path:
    sys.path.insert(0, str(ADK_ROOT / "checks"))

import pads_vs_pillars as pvp  # noqa: E402

PADS_CONFIG = json.loads(
    (ADK_ROOT / "config" / "chiplet_pads.json").read_text())["layers"]


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def make_die(ref="U1", x=1000.0, y=500.0, rotation=0.0,
             orientation=None, connection="cupillar_opt1", layout=None):
    die = {
        "id": ref,
        "type": "die",
        "position": {"x": x, "y": y, "z": 0.0},
        "rotation": {"z": rotation},
    }
    if connection:
        die["connection"] = connection
    if orientation:
        die["orientation"] = orientation
    if layout:
        die["layout"] = layout
    return die


def write_chiplet(tmp_path, dies, name="unit"):
    assembly = {
        "format_version": "1.0",
        "assembly": {"name": name, "units": "um"},
        "components": [
            {"id": "interposer", "type": "interposer",
             "position": {"x": 2000.0, "y": 1500.0, "z": 0.0}},
        ] + dies,
    }
    path = tmp_path / (name + ".chiplet")
    path.write_text(yaml.safe_dump(assembly, sort_keys=False))
    return path


def make_pillar(ref="U1", pin_name="", x=0.0, y=0.0,
                method="cupillar_opt1", diameter=75.0, **extra):
    entry = {"device_ref": ref, "pin_name": pin_name, "method": method,
             "x_um": x, "y_um": y, "diameter_um": diameter}
    entry.update(extra)
    return entry


def write_manifest(tmp_path, pillars, version="1.0.0", units="um",
                   schema="adk-pillar-manifest", name="unit"):
    manifest = {
        "schema": schema,
        "version": version,
        "generator": "test",
        "assembly_gds": name + "_interposer.gds",
        "units": units,
        "pillars": pillars,
    }
    path = tmp_path / (name + "_interposer.pillars.json")
    path.write_text(json.dumps(manifest, indent=2))
    return path


def write_pinlist(tmp_path, pins, ref="U1"):
    """gds_to_kicad-style *.pins.json: die-local, DBU (1 nm), y-up."""
    data = {
        "version": 1,
        "chiplet_name": ref,
        "dbu_um": 0.001,
        "pins": [
            {"name": name, "center_x_dbu": x * 1000.0,
             "center_y_dbu": y * 1000.0}
            for name, x, y in pins
        ],
    }
    path = tmp_path / (ref + ".pins.json")
    path.write_text(json.dumps(data))
    return path


def reference_transform(pad_xy, position, rotation_deg, mirrored):
    """Independent frame reference: global = position + R(rot) * M * pad."""
    px, py = pad_xy
    if mirrored:
        px = -px
    a = math.radians(rotation_deg)
    return (position[0] + px * math.cos(a) - py * math.sin(a),
            position[1] + px * math.sin(a) + py * math.cos(a))


def run_main(chiplet, manifest, *extra):
    return pvp.main(["--chiplet", str(chiplet), "--pillars", str(manifest)]
                    + list(extra))


# ---------------------------------------------------------------------------
# Alignment and placement transform
# ---------------------------------------------------------------------------

def test_aligned_die_passes(tmp_path):
    chiplet = write_chiplet(tmp_path, [make_die()])
    pins = write_pinlist(tmp_path, [("A", 10.0, 5.0), ("B", -10.0, 5.0)])
    manifest = write_manifest(tmp_path, [
        make_pillar(pin_name="A", x=1010.0, y=505.0),
        make_pillar(pin_name="B", x=990.0, y=505.0),
    ])
    assert run_main(chiplet, manifest, "--pins", "U1=%s" % pins) == 0


def test_translated_die_is_misaligned(tmp_path, capsys):
    chiplet = write_chiplet(tmp_path, [make_die()])
    pins = write_pinlist(tmp_path, [("A", 10.0, 5.0)])
    manifest = write_manifest(tmp_path, [
        make_pillar(pin_name="A", x=1015.0, y=505.0),  # 5 um off in x
    ])
    assert run_main(chiplet, manifest, "--pins", "U1=%s" % pins) == 1
    out = capsys.readouterr().out
    assert "MISALIGNED" in out
    assert "5.000000" in out


def test_moved_by_auto_resolve_demotes_to_warning(tmp_path, capsys):
    """A named pair beyond tolerance whose pillar carries
    moved_by_auto_resolve: true is the producer's own collision-resolver
    shift, not a placement error: warning, exit 0."""
    chiplet = write_chiplet(tmp_path, [make_die()])
    pins = write_pinlist(tmp_path, [("A", 10.0, 5.0)])
    manifest = write_manifest(tmp_path, [
        make_pillar(pin_name="A", x=1015.0, y=505.0,  # 5 um off in x
                    moved_by_auto_resolve=True),
    ])
    assert run_main(chiplet, manifest, "--pins", "U1=%s" % pins) == 0
    captured = capsys.readouterr()
    assert "MISALIGNED" not in captured.out
    assert "moved_by_auto_resolve" in captured.err
    assert "PASSED" in captured.out


@pytest.mark.parametrize("extra", [{}, {"moved_by_auto_resolve": False}])
def test_unmoved_pillar_beyond_tolerance_stays_misaligned(tmp_path, extra):
    """Only an explicit true flag demotes; false or absent keeps the
    finding."""
    chiplet = write_chiplet(tmp_path, [make_die()])
    pins = write_pinlist(tmp_path, [("A", 10.0, 5.0)])
    manifest = write_manifest(tmp_path, [
        make_pillar(pin_name="A", x=1015.0, y=505.0, **extra),
    ])
    assert run_main(chiplet, manifest, "--pins", "U1=%s" % pins) == 1


def test_moved_pillar_within_tolerance_is_silent(tmp_path, capsys):
    """A moved pillar that still lands within tolerance needs no warning."""
    chiplet = write_chiplet(tmp_path, [make_die()])
    pins = write_pinlist(tmp_path, [("A", 10.0, 5.0)])
    manifest = write_manifest(tmp_path, [
        make_pillar(pin_name="A", x=1010.5, y=505.0,
                    moved_by_auto_resolve=True),
    ])
    assert run_main(chiplet, manifest, "--pins", "U1=%s" % pins) == 0
    assert "moved_by_auto_resolve" not in capsys.readouterr().err


def test_auto_resolve_shift_bounds_demotion(tmp_path, capsys):
    """With a recorded shift the demotion is bounded: a deviation within
    shift + tolerance is a warning, beyond it is MISALIGNED again -- the
    flag can only excuse the shift the producer actually applied."""
    chiplet = write_chiplet(tmp_path, [make_die()])
    pins = write_pinlist(tmp_path, [("A", 10.0, 5.0)])
    # Pillar 5 um off; recorded shift 5 um, tolerance 1 um -> budget 6 um.
    ok = write_manifest(tmp_path, [
        make_pillar(pin_name="A", x=1015.0, y=505.0,
                    moved_by_auto_resolve=True, auto_resolve_shift_um=5.0),
    ], name="within")
    assert run_main(chiplet, ok, "--pins", "U1=%s" % pins) == 0
    assert "shift" in capsys.readouterr().err
    # Same pillar but the shift was only 1 um: 5 um deviation exceeds the
    # 2 um budget -> real misalignment, not excusable by the flag.
    bad = write_manifest(tmp_path, [
        make_pillar(pin_name="A", x=1015.0, y=505.0,
                    moved_by_auto_resolve=True, auto_resolve_shift_um=1.0),
    ], name="beyond")
    assert run_main(chiplet, bad, "--pins", "U1=%s" % pins) == 1
    out = capsys.readouterr().out
    assert "MISALIGNED" in out
    assert "recorded auto-resolve shift" in out


def test_negative_auto_resolve_shift_exits_2(tmp_path):
    """A negative recorded shift is a malformed manifest."""
    chiplet = write_chiplet(tmp_path, [make_die()])
    manifest = write_manifest(tmp_path, [
        make_pillar(pin_name="A", auto_resolve_shift_um=-1.0),
    ])
    assert run_main(chiplet, manifest) == 2


def test_match_device_without_warnings_list_keeps_finding():
    """Importer compatibility: no warnings list supplied -> the moved-flag
    pair stays a MISALIGNED finding (legacy strict behavior)."""
    pads = [{"name": "A", "x_um": 0.0, "y_um": 0.0}]
    pillars = [make_pillar(pin_name="A", x=5.0, y=0.0,
                           moved_by_auto_resolve=True)]
    findings, matched = pvp.match_device("U1", pads, pillars,
                                         tolerance_um=1.0)
    assert matched == 1
    assert [f["type"] for f in findings] == ["MISALIGNED"]
    warnings = []
    findings, matched = pvp.match_device("U1", pads, pillars,
                                         tolerance_um=1.0,
                                         warnings=warnings)
    assert matched == 1 and findings == []
    assert len(warnings) == 1 and "moved_by_auto_resolve" in warnings[0]


@pytest.mark.parametrize("rotation", [90.0, 180.0, 270.0, 37.5])
def test_rotation_transform(tmp_path, rotation):
    pads = [("A", 10.0, 5.0), ("B", -20.0, 15.0)]
    position = (1000.0, 500.0)
    chiplet = write_chiplet(
        tmp_path, [make_die(x=position[0], y=position[1],
                            rotation=rotation)])
    pins = write_pinlist(tmp_path, pads)
    pillars = []
    for name, px, py in pads:
        gx, gy = reference_transform((px, py), position, rotation, False)
        pillars.append(make_pillar(pin_name=name, x=gx, y=gy))
    manifest = write_manifest(tmp_path, pillars)
    assert run_main(chiplet, manifest, "--pins", "U1=%s" % pins,
                    "--tolerance-um", "0.001") == 0


@pytest.mark.parametrize("rotation", [0.0, 90.0])
def test_flip_chip_mirror(tmp_path, rotation):
    """flip_chip mirrors x BEFORE rotation; unmirrored pillars must fail."""
    pads = [("A", 10.0, 5.0)]
    position = (1000.0, 500.0)
    chiplet = write_chiplet(
        tmp_path, [make_die(x=position[0], y=position[1], rotation=rotation,
                            orientation="flip_chip")])
    pins = write_pinlist(tmp_path, pads)
    gx, gy = reference_transform((10.0, 5.0), position, rotation, True)
    manifest = write_manifest(tmp_path, [make_pillar(pin_name="A", x=gx, y=gy)])
    assert run_main(chiplet, manifest, "--pins", "U1=%s" % pins) == 0

    # The unmirrored position is 20 um away in the mirrored axis: MISALIGNED.
    ux, uy = reference_transform((10.0, 5.0), position, rotation, False)
    bad = write_manifest(tmp_path, [make_pillar(pin_name="A", x=ux, y=uy)],
                         name="unmirrored")
    assert run_main(chiplet, bad, "--pins", "U1=%s" % pins) == 1


def test_face_down_is_a_hard_error(tmp_path):
    chiplet = write_chiplet(
        tmp_path, [make_die(orientation="face_down")])
    pins = write_pinlist(tmp_path, [("A", 10.0, 5.0)])
    manifest = write_manifest(tmp_path, [make_pillar(pin_name="A")])
    assert run_main(chiplet, manifest, "--pins", "U1=%s" % pins) == 2


# ---------------------------------------------------------------------------
# Matching semantics
# ---------------------------------------------------------------------------

def test_pin_name_mismatch_yields_both_findings(tmp_path):
    """Two conflicting names never cross-match, even at distance zero."""
    chiplet = write_chiplet(tmp_path, [make_die()])
    pins = write_pinlist(tmp_path, [("A", 10.0, 5.0)])
    manifest = write_manifest(tmp_path, [
        make_pillar(pin_name="B", x=1010.0, y=505.0),
    ])
    report_path = tmp_path / "report.json"
    assert run_main(chiplet, manifest, "--pins", "U1=%s" % pins,
                    "--json", str(report_path)) == 1
    report = json.loads(report_path.read_text())
    kinds = sorted(f["type"] for f in report["findings"])
    assert kinds == ["PAD_WITHOUT_PILLAR", "PILLAR_WITHOUT_PAD"]


def test_tolerance_edge(tmp_path):
    """distance == tolerance passes; just beyond fails."""
    chiplet = write_chiplet(tmp_path, [make_die()])
    pins = write_pinlist(tmp_path, [("A", 10.0, 5.0)])
    exactly = write_manifest(tmp_path, [
        make_pillar(pin_name="A", x=1010.0, y=506.0),  # d = 1.0 = tolerance
    ], name="edge")
    assert run_main(chiplet, exactly, "--pins", "U1=%s" % pins) == 0
    beyond = write_manifest(tmp_path, [
        make_pillar(pin_name="A", x=1010.0, y=506.01),
    ], name="beyond")
    assert run_main(chiplet, beyond, "--pins", "U1=%s" % pins) == 1


def test_unnamed_nearest_unique_fallback(tmp_path):
    """Unnamed pillars match the nearest free pad within tolerance."""
    chiplet = write_chiplet(tmp_path, [make_die()])
    pins = write_pinlist(tmp_path, [("A", 10.0, 5.0), ("B", -10.0, 5.0)])
    manifest = write_manifest(tmp_path, [
        make_pillar(pin_name="", x=1010.2, y=505.0),
        make_pillar(pin_name="", x=989.9, y=505.0),
    ])
    assert run_main(chiplet, manifest, "--pins", "U1=%s" % pins) == 0


def test_ambiguous_match(tmp_path):
    """One unnamed pad equidistant from two pillars: undecidable."""
    chiplet = write_chiplet(tmp_path, [make_die()])
    pins = write_pinlist(tmp_path, [("", 0.0, 0.0)])
    manifest = write_manifest(tmp_path, [
        make_pillar(pin_name="", x=1000.5, y=500.0),
        make_pillar(pin_name="", x=999.5, y=500.0),
    ])
    report_path = tmp_path / "report.json"
    assert run_main(chiplet, manifest, "--pins", "U1=%s" % pins,
                    "--json", str(report_path)) == 1
    report = json.loads(report_path.read_text())
    assert any(f["type"] == "AMBIGUOUS_MATCH" for f in report["findings"])


def test_match_device_unit():
    """Direct unit check of match_device (no files)."""
    pads = [{"name": "A", "x_um": 0.0, "y_um": 0.0},
            {"name": "", "x_um": 100.0, "y_um": 0.0}]
    pillars = [{"pin_name": "A", "method": "m", "x_um": 0.2, "y_um": 0.0},
               {"pin_name": "", "method": "m", "x_um": 100.3, "y_um": 0.0},
               {"pin_name": "C", "method": "m", "x_um": 50.0, "y_um": 0.0}]
    findings, matched = pvp.match_device("U9", pads, pillars,
                                         tolerance_um=1.0)
    assert matched == 2
    assert [f["type"] for f in findings] == ["PILLAR_WITHOUT_PAD"]
    assert findings[0]["pillar_pin_name"] == "C"


# ---------------------------------------------------------------------------
# Exit codes, validation, strict, report
# ---------------------------------------------------------------------------

def test_bad_manifest_version_exits_2(tmp_path, capsys):
    chiplet = write_chiplet(tmp_path, [make_die()])
    manifest = write_manifest(tmp_path, [], version="0.9.0")
    assert run_main(chiplet, manifest) == 2
    assert "version" in capsys.readouterr().err


@pytest.mark.parametrize("mutation", [
    {"schema": "something-else"},
    {"units": "mm"},
])
def test_foreign_manifest_exits_2(tmp_path, mutation):
    chiplet = write_chiplet(tmp_path, [make_die()])
    manifest = write_manifest(tmp_path, [], **mutation)
    assert run_main(chiplet, manifest) == 2


def test_malformed_pillar_entry_exits_2(tmp_path):
    chiplet = write_chiplet(tmp_path, [make_die()])
    manifest = write_manifest(tmp_path, [
        {"device_ref": "U1", "pin_name": "A", "method": "m",
         "x_um": "not-a-number", "y_um": 0.0, "diameter_um": 75.0},
    ])
    assert run_main(chiplet, manifest) == 2


def test_missing_manifest_exits_2(tmp_path):
    chiplet = write_chiplet(tmp_path, [make_die()])
    assert run_main(chiplet, tmp_path / "absent.pillars.json") == 2


def test_unknown_ref_exits_2(tmp_path, capsys):
    chiplet = write_chiplet(tmp_path, [make_die()])
    pins = write_pinlist(tmp_path, [("A", 10.0, 5.0)], ref="U9")
    manifest = write_manifest(tmp_path, [])
    assert run_main(chiplet, manifest, "--pins", "U9=%s" % pins) == 2
    assert "U9" in capsys.readouterr().err
    assert run_main(chiplet, manifest, "--gds-pads", "U9") == 2


def test_strict_flags_unchecked_die(tmp_path, capsys):
    """A die with a connection method and no pad source: warning by
    default (exit 0), NO_PAD_SOURCE finding with --strict (exit 1)."""
    chiplet = write_chiplet(tmp_path, [make_die()])
    manifest = write_manifest(tmp_path, [make_pillar(pin_name="A")])
    assert run_main(chiplet, manifest) == 0
    assert "UNCHECKED" in capsys.readouterr().err
    report_path = tmp_path / "report.json"
    assert run_main(chiplet, manifest, "--strict",
                    "--json", str(report_path)) == 1
    report = json.loads(report_path.read_text())
    assert [f["type"] for f in report["findings"]] == ["NO_PAD_SOURCE"]


@pytest.mark.parametrize("bad_tol", ["nan", "-1.0", "inf"])
def test_pathological_tolerance_exits_2(tmp_path, bad_tol, capsys):
    """NaN would fail-open ('dist > nan' is False), negative and inf are
    nonsense: all must be rejected, never a silent PASS."""
    chiplet = write_chiplet(tmp_path, [make_die()])
    pins = write_pinlist(tmp_path, [("A", 10.0, 5.0)])
    manifest = write_manifest(tmp_path, [
        make_pillar(pin_name="A", x=1100.0, y=505.0),  # 90 um off
    ])
    assert run_main(chiplet, manifest, "--pins", "U1=%s" % pins,
                    "--tolerance-um", bad_tol) == 2
    assert "tolerance" in capsys.readouterr().err


def test_non_um_assembly_exits_2(tmp_path, capsys):
    """A .chiplet declaring mm units would compare positions at 1000x
    scale: reject it, do not silently PASS (mirrors chiplet2dbx)."""
    chiplet = write_chiplet(tmp_path, [make_die()])
    # Rewrite assembly.units to mm.
    doc = yaml.safe_load(chiplet.read_text())
    doc["assembly"]["units"] = "mm"
    chiplet.write_text(yaml.safe_dump(doc, sort_keys=False))
    pins = write_pinlist(tmp_path, [("A", 10.0, 5.0)])
    manifest = write_manifest(tmp_path, [make_pillar(pin_name="A",
                                                     x=1010.0, y=505.0)])
    assert run_main(chiplet, manifest, "--pins", "U1=%s" % pins) == 2
    assert "units" in capsys.readouterr().err


def test_no_json_report_on_validation_error(tmp_path):
    """--json output is not written when the run aborts at exit 2."""
    chiplet = write_chiplet(tmp_path, [make_die()])
    manifest = write_manifest(tmp_path, [], version="0.9.0")
    report_path = tmp_path / "report.json"
    assert run_main(chiplet, manifest, "--json", str(report_path)) == 2
    assert not report_path.exists()


def test_gds_pads_without_klayout_exits_2(tmp_path, monkeypatch, capsys):
    """--gds-pads on an interpreter without klayout.db fails loudly (2),
    not with an opaque ImportError traceback."""
    import builtins
    real_import = builtins.__import__

    def _no_klayout(name, *args, **kwargs):
        if name == "klayout.db" or name.startswith("klayout"):
            raise ImportError("simulated missing klayout")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_klayout)
    gds = tmp_path / "die.gds"
    gds.write_text("")  # never read: the import guard fires first
    chiplet = write_chiplet(tmp_path, [make_die(layout=str(gds))])
    manifest = write_manifest(tmp_path, [])
    assert run_main(chiplet, manifest, "--gds-pads", "U1") == 2
    assert "klayout" in capsys.readouterr().err


def test_empty_pillars_with_no_connected_dies_passes(tmp_path):
    """A bump-path run with zero bumps writes an empty manifest; a die
    without a connection method needs no pad source."""
    chiplet = write_chiplet(tmp_path, [make_die(connection=None)])
    manifest = write_manifest(tmp_path, [])
    assert run_main(chiplet, manifest) == 0


def test_json_report_content(tmp_path):
    chiplet = write_chiplet(tmp_path, [make_die()])
    pins = write_pinlist(tmp_path, [("A", 10.0, 5.0)])
    manifest = write_manifest(tmp_path, [
        make_pillar(pin_name="A", x=1015.0, y=505.0),
    ])
    report_path = tmp_path / "report.json"
    assert run_main(chiplet, manifest, "--pins", "U1=%s" % pins,
                    "--json", str(report_path)) == 1
    report = json.loads(report_path.read_text())
    assert report["tool"] == "pads_vs_pillars"
    assert report["tolerance_um"] == 1.0
    assert report["summary"]["passed"] is False
    assert report["summary"]["devices_checked"] == 1
    assert report["devices"]["U1"] == {
        "pads": 1, "pillars": 1, "matched": 1, "findings": 1}
    (finding,) = report["findings"]
    assert finding["type"] == "MISALIGNED"
    assert finding["device_ref"] == "U1"
    assert finding["pad_name"] == "A"
    assert finding["distance_um"] == pytest.approx(5.0)
    assert finding["pad_x_um"] == pytest.approx(1010.0)
    assert finding["pillar_x_um"] == pytest.approx(1015.0)


def test_manifest_example_obeys_schema(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (ADK_ROOT / "config" / "schema"
         / "pillar_manifest.schema.json").read_text())
    manifest_path = write_manifest(tmp_path, [
        make_pillar(pin_name="A", x=1.0, y=2.0),
        make_pillar(pin_name="", x=3.0, y=4.0, moved_by_auto_resolve=True),
    ])
    manifest = json.loads(manifest_path.read_text())
    jsonschema.validate(manifest, schema)
    # And the reader accepts exactly what the schema accepts here.
    pvp.load_pillar_manifest(manifest_path)


# ---------------------------------------------------------------------------
# --gds-pads: extraction from a synthetic die GDS
# ---------------------------------------------------------------------------

def _write_die_gds(path):
    """Tiny black-box die: two pads + labels on the chiplet_pads.json
    layers (never hardcoded)."""
    kdb = pytest.importorskip("klayout.db")
    drawing = PADS_CONFIG["pad_drawing"]
    text = PADS_CONFIG["pad_text"]
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("DIE")
    pad_li = ly.layer(drawing["gds_layer"], drawing["gds_datatype"])
    txt_li = ly.layer(text["gds_layer"], text["gds_datatype"])

    def um(v):
        return int(round(v * 1000))

    # Pad A centered (20, 10); pad B centered (-20, 10).
    top.shapes(pad_li).insert(kdb.Box(um(15), um(5), um(25), um(15)))
    top.shapes(pad_li).insert(kdb.Box(um(-25), um(5), um(-15), um(15)))
    top.shapes(txt_li).insert(kdb.Text("A", um(20), um(10)))
    top.shapes(txt_li).insert(kdb.Text("B", um(-20), um(10)))
    ly.write(str(path))
    return {"A": (20.0, 10.0), "B": (-20.0, 10.0)}


def test_gds_pads_extraction(tmp_path):
    pytest.importorskip("klayout.db")
    gds = tmp_path / "die.gds"
    expected = _write_die_gds(gds)
    pads = pvp.extract_gds_pads(gds)
    assert {p["name"]: (p["x_um"], p["y_um"]) for p in pads} == expected


def test_gds_pads_end_to_end(tmp_path):
    pytest.importorskip("klayout.db")
    gds = tmp_path / "die.gds"
    expected = _write_die_gds(gds)
    position = (1000.0, 500.0)
    rotation = 90.0
    chiplet = write_chiplet(
        tmp_path, [make_die(x=position[0], y=position[1], rotation=rotation,
                            layout=str(gds))])
    pillars = [
        make_pillar(pin_name=name,
                    x=reference_transform(xy, position, rotation, False)[0],
                    y=reference_transform(xy, position, rotation, False)[1])
        for name, xy in expected.items()
    ]
    manifest = write_manifest(tmp_path, pillars)
    assert run_main(chiplet, manifest, "--gds-pads", "U1") == 0

    # Perturbed pillar: the extracted, named pads pin it as MISALIGNED.
    pillars[0]["x_um"] += 3.0
    bad = write_manifest(tmp_path, pillars, name="perturbed")
    assert run_main(chiplet, bad, "--gds-pads", "U1") == 1


def test_gds_pads_empty_die_exits_2(tmp_path, capsys):
    kdb = pytest.importorskip("klayout.db")
    gds = tmp_path / "empty.gds"
    ly = kdb.Layout()
    ly.dbu = 0.001
    ly.create_cell("DIE")
    ly.write(str(gds))
    chiplet = write_chiplet(tmp_path, [make_die(layout=str(gds))])
    manifest = write_manifest(tmp_path, [])
    assert run_main(chiplet, manifest, "--gds-pads", "U1") == 2
    assert "pad_drawing" in capsys.readouterr().err
