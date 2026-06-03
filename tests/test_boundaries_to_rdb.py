"""Tests for boundaries_to_rdb: manifest -> KLayout .lyrdb marker database.

The viewer's core/CLI is fully testable headless via the standalone klayout
python module; the GUI macro (klayout/macros/show_boundaries.lym) is thin glue
over build_rdb, so the build logic is exercised here once and shared by both.
"""
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("klayout.db")
pytest.importorskip("klayout.rdb")

ADK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADK / "klayout" / "macros"))

import boundaries_to_rdb as core  # noqa: E402


def _manifest(boundaries, **top):
    m = {"schema": "adk-boundary-manifest", "version": "1.0.0",
         "top_cell": "INTERPOSER", "dbu_um": 0.001}
    m.update(top)
    m["boundaries"] = boundaries
    return m


def _subcategories(r):
    return sorted(sc.name()
                  for c in r.each_category()
                  for sc in c.each_sub_category())


# --- path / manifest plumbing ------------------------------------------------

def test_find_sidecar_mirrors_runner_rule():
    assert core.find_sidecar("/x/board_complete.gds") == \
        Path("/x/board_complete.boundaries.json")
    assert core.find_sidecar("/x/y.oas").name == "y.boundaries.json"


def test_load_manifest_rejects_foreign(tmp_path):
    p = tmp_path / "f.json"
    p.write_text(json.dumps({"schema": "something-else"}))
    with pytest.raises(ValueError):
        core.load_manifest(p)


def test_load_manifest_accepts(tmp_path):
    m = _manifest([{"instance": "U1",
                    "polygon_um": [[0, 0], [10, 0], [10, 10]]}])
    p = tmp_path / "b.boundaries.json"
    p.write_text(json.dumps(m))
    assert core.load_manifest(p)["boundaries"][0]["instance"] == "U1"


# --- rdb structure -----------------------------------------------------------

def test_one_subcategory_per_chiplet():
    m = _manifest([
        {"instance": "U3", "source_die": "ACME_PHY", "class": "chiplet",
         "polygon_um": [[0, 0], [100, 0], [100, 50], [0, 50]]},
        {"instance": "U7", "source_die": "FOO",
         "polygon_um": [[200, 0], [300, 0], [300, 50], [200, 50]]},
    ])
    r = core.build_rdb(m)
    assert r.num_items() == 2
    assert [c.name() for c in r.each_category()] == [core.CATEGORY]
    assert _subcategories(r) == ["U3", "U7"]


def test_empty_manifest_is_valid_and_empty():
    assert core.build_rdb(_manifest([])).num_items() == 0


def test_degenerate_polygon_skipped():
    r = core.build_rdb(_manifest([
        {"instance": "U3", "polygon_um": [[0, 0], [10, 0]]},  # 2 pts
    ]))
    assert r.num_items() == 0


def test_label_fallbacks_instance_then_die_then_index():
    m = _manifest([
        {"source_die": "ONLY_DIE", "polygon_um": [[0, 0], [10, 0], [10, 10]]},
        {"polygon_um": [[0, 0], [10, 0], [10, 10]]},
    ])
    assert _subcategories(core.build_rdb(m)) == ["ONLY_DIE", "boundary_1"]


def test_category_name_sanitized():
    # '.' / '/' are RDB category-path separators; keep the instance atomic.
    m = _manifest([
        {"instance": "U3.A", "polygon_um": [[0, 0], [10, 0], [10, 10]]},
    ])
    assert _subcategories(core.build_rdb(m)) == ["U3_A"]


# --- the load-bearing unit invariant: markers are in microns -----------------

def test_markers_use_microns_not_dbu(tmp_path):
    m = _manifest([
        {"instance": "U3",
         "polygon_um": [[1100, 725], [1300, 725], [1300, 875], [1100, 875]],
         "polygon_dbu": [[1100000, 725000], [1300000, 725000],
                         [1300000, 875000], [1100000, 875000]]},
    ])
    txt = core.write_lyrdb(m, tmp_path / "b.lyrdb").read_text()
    assert "1100,725" in txt          # microns from polygon_um
    assert "1100000" not in txt        # never raw DBU (would render 1000x off)


def test_polygon_um_derived_from_dbu_when_absent(tmp_path):
    m = _manifest([
        {"instance": "U3",
         "polygon_dbu": [[1100000, 725000], [1300000, 725000],
                         [1300000, 875000], [1100000, 875000]]},
    ], dbu_um=0.001)
    txt = core.write_lyrdb(m, tmp_path / "b.lyrdb").read_text()
    assert "1100,725" in txt           # 1100000 * 0.001 um
    assert "1100000" not in txt


# --- CLI ---------------------------------------------------------------------

def test_resolve_input_gds_without_sidecar_errors(tmp_path):
    gds = tmp_path / "x.gds"
    gds.write_bytes(b"")  # need not be a real GDS; only the sidecar matters
    with pytest.raises(FileNotFoundError):
        core._resolve_input(str(gds))


def test_resolve_input_finds_sidecar(tmp_path):
    (tmp_path / "x.boundaries.json").write_text(json.dumps(
        _manifest([{"instance": "U1",
                    "polygon_um": [[0, 0], [10, 0], [10, 10]]}])))
    manifest, default_out = core._resolve_input(str(tmp_path / "x.gds"))
    assert manifest["boundaries"][0]["instance"] == "U1"
    assert default_out.name == "x.boundaries.lyrdb"


def test_cli_writes_lyrdb(tmp_path):
    (tmp_path / "x.boundaries.json").write_text(json.dumps(
        _manifest([{"instance": "U1",
                    "polygon_um": [[0, 0], [10, 0], [10, 10]]}])))
    assert core.main([str(tmp_path / "x.gds")]) == 0
    assert (tmp_path / "x.boundaries.lyrdb").is_file()


def test_cli_bad_input_returns_1(tmp_path):
    assert core.main([str(tmp_path / "missing.gds")]) == 1
