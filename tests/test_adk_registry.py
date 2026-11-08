# SPDX-License-Identifier: Apache-2.0
"""Unit tests for adk_registry, the anchored ID->location resolver.

KLayout-free: exercises the resolver's regex, fail-closed exception mapping,
the root directory-return invariant, provenance, and the data-vs-code root
enumeration (D4). Live sibling checkouts are never required: every root/adapter
resolution that needs a concrete target uses a fixture registry injected
through ``ADK_REGISTRY_DIR`` and a tmp tree, so the suite passes in an isolated
worktree as well as in a full ecosystem checkout.
"""

import json
import re
import sys
from pathlib import Path

import pytest

ADK_ROOT = Path(__file__).resolve().parents[1]
if str(ADK_ROOT) not in sys.path:
    sys.path.insert(0, str(ADK_ROOT))

import adk_registry as reg  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_registry_env(monkeypatch, tmp_path):
    """Baseline every test on the built-in registry alone.

    Clears the process-env override and points XDG at an empty dir, so neither
    a developer's real ``~/.config/adk-tools/registry.json`` nor a leaked
    ``ADK_REGISTRY_DIR`` can perturb a result.
    """
    monkeypatch.delenv("ADK_REGISTRY_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))


def _write_override(monkeypatch, tmp_path, payload):
    """Write an override registry and point ADK_REGISTRY_DIR at its dir."""
    d = tmp_path / "override"
    d.mkdir(exist_ok=True)
    (d / "registry.json").write_text(json.dumps(payload))
    monkeypatch.setenv("ADK_REGISTRY_DIR", str(d))
    return d / "registry.json"


# --------------------------------------------------------------------------- #
# Id regex validation                                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("good", [
    "intm4tm2", "ihp_cupillar", "ADK", "INTERCONNECT_PDK",
    "a", "A0", "_x", "x.y-z_1", "vendorx_microbump",
])
def test_validate_id_accepts_well_formed(good):
    assert reg.validate_id(good) == good


@pytest.mark.parametrize("bad", [
    "", ".", "..", "-x", ".hidden", "a/b", "/abs/path.drc",
    "~/x", "a b", "${VAR}", "x$y", "a\\b", "a\ty",
    # REG-1: an otherwise-valid id with a single trailing newline. Python's
    # ``$`` matches just before it, so ``^...$`` would ACCEPT these and pass the
    # value into a quoted ``.chiplet`` scalar. ``\A``/``\Z`` reject them.
    "intm4tm2\n", "a\n",
])
def test_validate_id_rejects_paths_and_malformed(bad):
    with pytest.raises(reg.IdLookupError):
        reg.validate_id(bad)


def test_validate_id_path_message_is_specific():
    with pytest.raises(reg.IdLookupError, match="path was given where a registry id"):
        reg.validate_id("/abs/interposer.drc")


def test_regex_pattern_is_the_contract():
    # The exact contract regex; a drift here is a contract change. Anchored with
    # \A/\Z, never ^/$: Python's $ also matches before a single trailing newline
    # (REG-1), which would let "id\n" pass into an emitted .chiplet scalar.
    assert reg.ID_PATTERN == r"\A[A-Za-z0-9_][A-Za-z0-9_.-]*\Z"
    assert "$" not in reg.ID_PATTERN and reg.ID_PATTERN.endswith(r"\Z")
    # The exported constant must be safe however a port applies it.
    rx = re.compile(reg.ID_PATTERN)
    assert rx.match("intm4tm2") and rx.fullmatch("intm4tm2")
    assert rx.match("intm4tm2\n") is None and rx.search("intm4tm2\n") is None


# --------------------------------------------------------------------------- #
# Fail closed with the three distinct exception types + exit mapping           #
# --------------------------------------------------------------------------- #
def test_unknown_id_is_caller_error_exit_1():
    with pytest.raises(reg.IdLookupError) as exc:
        reg.resolve_root("definitely_not_registered")
    assert reg.exit_code_for(exc.value) == 1


def test_malformed_id_is_caller_error_exit_1():
    with pytest.raises(reg.IdLookupError) as exc:
        reg.resolve_adapter("../escape")
    assert reg.exit_code_for(exc.value) == 1


def test_absent_override_registry_is_source_error_exit_5(monkeypatch, tmp_path):
    # ADK_REGISTRY_DIR set but no registry.json inside it.
    monkeypatch.setenv("ADK_REGISTRY_DIR", str(tmp_path / "empty-dir"))
    (tmp_path / "empty-dir").mkdir()
    with pytest.raises(reg.RegistrySourceError) as exc:
        reg.resolve_root("ADK")
    assert reg.exit_code_for(exc.value) == 5


def test_malformed_registry_is_source_error_exit_5(monkeypatch, tmp_path):
    d = tmp_path / "override"
    d.mkdir()
    (d / "registry.json").write_text("{ this is not json")
    monkeypatch.setenv("ADK_REGISTRY_DIR", str(d))
    with pytest.raises(reg.RegistrySourceError) as exc:
        reg.resolve_root("ADK")
    assert reg.exit_code_for(exc.value) == 5


def test_wrong_schema_is_source_error_exit_5(monkeypatch, tmp_path):
    _write_override(monkeypatch, tmp_path,
                    {"schema": "something-else", "version": "1.0", "roots": {}})
    with pytest.raises(reg.RegistrySourceError) as exc:
        reg.resolve_root("ADK")
    assert reg.exit_code_for(exc.value) == 5


def test_unreadable_major_is_version_error_exit_6(monkeypatch, tmp_path):
    _write_override(monkeypatch, tmp_path,
                    {"schema": "adk-registry", "version": "2.0", "roots": {}})
    with pytest.raises(reg.RegistryVersionError) as exc:
        reg.resolve_root("ADK")
    assert reg.exit_code_for(exc.value) == 6


def test_same_major_higher_minor_is_tolerated(monkeypatch, tmp_path):
    real = tmp_path / "real_root"
    real.mkdir()
    _write_override(monkeypatch, tmp_path, {
        "schema": "adk-registry", "version": "1.99",
        "roots": {"TESTROOT": str(real)},
    })
    assert reg.resolve_root("TESTROOT").value == real


def test_exit_code_for_unknown_exception_is_internal():
    assert reg.exit_code_for(ValueError("x")) == 7


def test_three_exception_types_are_distinct_siblings():
    for name in ("IdLookupError", "RegistrySourceError", "RegistryVersionError"):
        assert issubclass(getattr(reg, name), reg.RegistryError)
    # Distinct, non-overlapping (a version error must not be caught as source).
    assert not issubclass(reg.RegistryVersionError, reg.RegistrySourceError)
    assert not issubclass(reg.RegistrySourceError, reg.RegistryVersionError)
    assert not issubclass(reg.IdLookupError, reg.RegistrySourceError)


# --------------------------------------------------------------------------- #
# Root resolution returns a directory (invariant)                              #
# --------------------------------------------------------------------------- #
def test_root_returns_directory(monkeypatch, tmp_path):
    real = tmp_path / "a_root_dir"
    real.mkdir()
    _write_override(monkeypatch, tmp_path,
                    {"schema": "adk-registry", "version": "1.0",
                     "roots": {"TESTROOT": str(real)}})
    res = reg.resolve_root("TESTROOT")
    assert isinstance(res.value, Path)
    assert res.value.is_dir()
    assert res.value == real


def test_root_mapped_to_file_fails_closed(monkeypatch, tmp_path):
    afile = tmp_path / "not_a_dir.txt"
    afile.write_text("x")
    _write_override(monkeypatch, tmp_path,
                    {"schema": "adk-registry", "version": "1.0",
                     "roots": {"TESTROOT": str(afile)}})
    with pytest.raises(reg.RegistrySourceError) as exc:
        reg.resolve_root("TESTROOT")
    assert reg.exit_code_for(exc.value) == 5


def test_root_mapped_to_missing_path_fails_closed(monkeypatch, tmp_path):
    _write_override(monkeypatch, tmp_path,
                    {"schema": "adk-registry", "version": "1.0",
                     "roots": {"TESTROOT": str(tmp_path / "nope")}})
    with pytest.raises(reg.RegistrySourceError):
        reg.resolve_root("TESTROOT")


def test_builtin_ADK_root_resolves_to_a_directory():
    # 'ADK' maps to '.', the registry file's own dir: always a real directory,
    # so this invariant holds without any sibling checkout present.
    res = reg.resolve_root("ADK")
    assert res.value.is_dir()
    assert res.value == ADK_ROOT


def test_relative_value_resolves_against_declaring_file(monkeypatch, tmp_path):
    sub = tmp_path / "override" / "child"
    sub.mkdir(parents=True)
    _write_override(monkeypatch, tmp_path,
                    {"schema": "adk-registry", "version": "1.0",
                     "roots": {"TESTROOT": "child"}})
    assert reg.resolve_root("TESTROOT").value == sub


# --------------------------------------------------------------------------- #
# Provenance (D1: no silent authority)                                         #
# --------------------------------------------------------------------------- #
def test_provenance_points_at_builtin_by_default():
    res = reg.resolve_adapter("intm4tm2")
    assert res.source == (ADK_ROOT / "adk_registry.builtin.json")


def test_override_changes_provenance_and_value(monkeypatch, tmp_path):
    real = tmp_path / "over_root"
    real.mkdir()
    src = _write_override(monkeypatch, tmp_path,
                          {"schema": "adk-registry", "version": "1.0",
                           "roots": {"ADK": str(real)}})
    res = reg.resolve_root("ADK")
    assert res.value == real          # override beat the built-in '.'
    assert res.source == src          # and provenance names the override file


# --------------------------------------------------------------------------- #
# Built-in adapters really resolve to the vetted .drc files                    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("adapter_id,rel", [
    ("intm4tm2", "pdk_adapters/interposer/intm4tm2.drc"),
    ("ihp_cupillar", "pdk_adapters/interconnect/ihp_cupillar.drc"),
    ("ihp_sbump", "pdk_adapters/interconnect/ihp_sbump.drc"),
    ("vendorx_microbump", "pdk_adapters/interconnect/vendorx_microbump.drc"),
])
def test_builtin_adapters_resolve_to_real_drc(adapter_id, rel):
    res = reg.resolve_adapter(adapter_id)
    assert res.value == (ADK_ROOT / rel).resolve()
    assert res.value.is_file()


def test_available_lists_ids():
    assert set(reg.available("adapter")) >= {
        "intm4tm2", "ihp_cupillar", "ihp_sbump", "vendorx_microbump"}
    assert set(reg.available("root")) == {
        "ADK", "INTERPOSER_PDK", "INTERCONNECT_PDK", "GDS_TO_KICAD"}
    assert "worker" in reg.available("interpreter")


# --------------------------------------------------------------------------- #
# D4: data-vs-code root enumeration                                            #
# --------------------------------------------------------------------------- #
# The authoritative classification of every ecosystem-root variable. The
# ``root`` side becomes registry ids; the ``path`` side stays a validated path
# (data / user-chosen output), never an id. "BOTH gds_to_kicad repository
# roots" from the design are covered here: GDS_TO_KICAD (its own repo root) and
# INTERPOSER_PDK (the interposer repo root gds_to_kicad joins a fixed relative
# path onto, to reach a shipped board template) are both on the id side.
_ROOT_VAR_TO_ID = {
    "ADK_ROOT": "ADK",
    "INTERPOSER_PDK_ROOT": "INTERPOSER_PDK",
    "INTERCONNECT_PDK_ROOT": "INTERCONNECT_PDK",
    "GDS_TO_KICAD_ROOT": "GDS_TO_KICAD",
}
# Ecosystem vars that STAY validated paths per the sharpened D4 rule: a place
# the user legitimately chooses (PDK_ROOT) or a writable output base
# (GDS_TO_KICAD_DATA_DIR). Neither is ever a registry id.
_PATH_VARS = {"PDK_ROOT", "GDS_TO_KICAD_DATA_DIR"}


def _code_ecosystem_vars():
    """The ecosystem-root variables the ADK code actually enumerates.

    Read from ``checks/pads_vs_pillars.py``'s ``_PATH_VAR_MARKERS`` table by a
    lightweight text scan (no heavy klayout import). Couples this test to the
    code's own list, so a NEW ecosystem-root variable forces an explicit
    id-vs-path decision here instead of landing on the wrong side unnoticed.
    """
    text = (ADK_ROOT / "checks" / "pads_vs_pillars.py").read_text()
    m = re.search(r"_PATH_VAR_MARKERS\s*=\s*\{(.*?)\n\}", text, re.DOTALL)
    assert m, "could not locate _PATH_VAR_MARKERS in pads_vs_pillars.py"
    return set(re.findall(r'"([A-Z][A-Z0-9_]*)":\s*\(', m.group(1)))


def test_every_ecosystem_var_is_classified_id_or_path():
    code_vars = _code_ecosystem_vars()
    classified = set(_ROOT_VAR_TO_ID) | _PATH_VARS
    unclassified = code_vars - classified
    assert not unclassified, (
        "unclassified ecosystem-root variable(s) %s: decide id vs path per the "
        "D4 rule and update this test + the registry." % sorted(unclassified))


def test_id_and_path_sides_are_disjoint():
    assert not (set(_ROOT_VAR_TO_ID) & _PATH_VARS)


def test_registry_roots_equal_the_id_side_exactly():
    # Every code root becomes a registry id, and NOTHING else is a root.
    assert set(reg.available("root")) == set(_ROOT_VAR_TO_ID.values())


def test_path_vars_are_not_registry_roots():
    roots = set(reg.available("root"))
    # Neither the raw var name nor its '_ROOT'-stripped form is ever a root id.
    for var in _PATH_VARS:
        assert var not in roots
        assert var.replace("_ROOT", "") not in roots


def test_both_gds_to_kicad_roots_are_ids():
    roots = set(reg.available("root"))
    assert {"GDS_TO_KICAD", "INTERPOSER_PDK"} <= roots
