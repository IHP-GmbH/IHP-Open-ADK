# SPDX-License-Identifier: Apache-2.0
"""Unit tests for run_drc.py helpers that do not shell out to `klayout -b`.

These run even without the KLayout CLI (they need only the klayout python module
that conftest already imports), so they pin the RDB parser, the path/manifest
validators, and the new structural/version checks that the subprocess DRC tests
(klayout-CLI-gated) never exercise directly.
"""
import json
import xml.etree.ElementTree as ET

import klayout.db as kdb
import pytest

from run_drc import (
    SUPPORTED_IXN_METHODS_VERSION,
    SUPPORTED_MANIFEST_VERSION,
    check_layout_path,
    get_rules_with_violations,
    get_run_top_cell_name,
    resolve_ixn_methods_path,
    resolve_manifest_path,
)


# --- get_rules_with_violations: parse <items>/<category> by tag --------------
# (Was a hardcoded root[7]/rule[1] positional index; these pin the tag-based
# parse and the error/edge paths a real report never exercises.)

def _write_lyrdb(path, categories):
    items = "".join(f"<item><category>'{c}'</category></item>" for c in categories)
    path.write_text(
        "<report-database><description/><tags/><categories/><cells/>"
        f"<items>{items}</items></report-database>"
    )


def test_get_rules_returns_category_set(tmp_path):
    rdb = tmp_path / "r.lyrdb"
    _write_lyrdb(rdb, ["ASM.a", "ASM.b", "IXN.b.coarse_method"])
    assert get_rules_with_violations(rdb) == {
        "ASM.a", "ASM.b", "IXN.b.coarse_method"}


def test_get_rules_empty_items(tmp_path):
    rdb = tmp_path / "r.lyrdb"
    _write_lyrdb(rdb, [])
    assert get_rules_with_violations(rdb) == set()


def test_get_rules_no_items_element_is_empty(tmp_path):
    # A report with no <items> must yield an empty set, not the IndexError the
    # old positional root[7] index would raise on a short/truncated database.
    rdb = tmp_path / "r.lyrdb"
    rdb.write_text("<report-database><description/></report-database>")
    assert get_rules_with_violations(rdb) == set()


def test_get_rules_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        get_rules_with_violations(tmp_path / "nope.lyrdb")


def test_get_rules_malformed_xml_raises(tmp_path):
    rdb = tmp_path / "bad.lyrdb"
    rdb.write_text("<report-database><items>")  # truncated
    with pytest.raises(ET.ParseError):
        get_rules_with_violations(rdb)


# --- check_layout_path -------------------------------------------------------

def test_check_layout_path_missing(tmp_path):
    with pytest.raises(SystemExit):
        check_layout_path(str(tmp_path / "nope.gds"))


def test_check_layout_path_wrong_extension(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("not a layout")
    with pytest.raises(SystemExit):
        check_layout_path(str(f))


def test_check_layout_path_accepts_gds_gz(tmp_path):
    f = tmp_path / "x.gds.gz"
    f.write_bytes(b"")  # content not validated here, only the extension
    assert check_layout_path(str(f)) == str(f.resolve())


# --- get_run_top_cell_name ---------------------------------------------------

def test_top_cell_override_skips_read():
    assert get_run_top_cell_name("MY_TOP", "/does/not/exist.gds") == "MY_TOP"


def test_top_cell_multiple_tops_aborts(tmp_path):
    gds = tmp_path / "two_tops.gds"
    ly = kdb.Layout()
    ly.create_cell("TOP_A")
    ly.create_cell("TOP_B")
    ly.write(str(gds))
    with pytest.raises(SystemExit):
        get_run_top_cell_name("", str(gds))


# --- resolve_manifest_path: structural validation ----------------------------

def _manifest(tmp_path, **overrides):
    m = {"schema": "adk-boundary-manifest", "version": SUPPORTED_MANIFEST_VERSION,
         "dbu_um": 0.001, "top_cell": "TOP",
         "boundaries": [{"instance": "U1",
                         "polygon_dbu": [[0, 0], [10, 0], [10, 10], [0, 10]]}]}
    m.update(overrides)
    p = tmp_path / "m.boundaries.json"
    p.write_text(json.dumps(m))
    return p


def test_manifest_valid_resolves(tmp_path):
    p = _manifest(tmp_path)
    assert resolve_manifest_path("x.gds", str(p), False) == p.resolve()


def test_manifest_missing_polygon_dbu_aborts(tmp_path):
    p = _manifest(tmp_path, boundaries=[{"instance": "U1"}])
    with pytest.raises(SystemExit):
        resolve_manifest_path("x.gds", str(p), False)


def test_manifest_boundaries_not_list_aborts(tmp_path):
    p = _manifest(tmp_path, boundaries={"not": "a list"})
    with pytest.raises(SystemExit):
        resolve_manifest_path("x.gds", str(p), False)


def test_manifest_short_polygon_aborts(tmp_path):
    p = _manifest(tmp_path, boundaries=[{"polygon_dbu": [[0, 0], [10, 0]]}])
    with pytest.raises(SystemExit):
        resolve_manifest_path("x.gds", str(p), False)


def test_manifest_non_object_aborts(tmp_path):
    p = tmp_path / "m.boundaries.json"
    p.write_text("[1, 2, 3]")  # JSON array, not an object
    with pytest.raises(SystemExit):
        resolve_manifest_path("x.gds", str(p), False)


# --- resolve_ixn_methods_path: schema/version validation ---------------------

def _methods(tmp_path, **overrides):
    m = {"schema": "adk-ixn-methods", "version": SUPPORTED_IXN_METHODS_VERSION,
         "methods": {"m1": {"IXN_spacing": 40.0, "IXN_pitch": 75.0,
                            "IXN_pad_size": 35.0, "dies": ["U1"]}}}
    m.update(overrides)
    p = tmp_path / "x.ixn_methods.json"
    p.write_text(json.dumps(m))
    return p


def test_ixn_methods_valid_resolves(tmp_path):
    p = _methods(tmp_path)
    assert resolve_ixn_methods_path(str(p)) == p.resolve()


def test_ixn_methods_wrong_schema_aborts(tmp_path):
    p = _methods(tmp_path, schema="something-else")
    with pytest.raises(SystemExit):
        resolve_ixn_methods_path(str(p))


def test_ixn_methods_wrong_version_aborts(tmp_path):
    p = _methods(tmp_path, version="9.9.9")
    with pytest.raises(SystemExit):
        resolve_ixn_methods_path(str(p))
