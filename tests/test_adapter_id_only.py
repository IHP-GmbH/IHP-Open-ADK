# SPDX-License-Identifier: Apache-2.0
"""Security acceptance: an adapter is selected by REGISTRY ID, never by path.

An adapter .drc is EVALUATED Ruby inside adk_assembly.drc, so the argument that
selects one is code selection. An untrusted .chiplet reaches those arguments
(interposer.adapter / interconnect.adapter), so a resolver that accepts a path
lets a downloaded project point the deck at a planted file: a permissive deck
signs off fab-bound geometry, a hostile one executes.

This module pins the closure at the boundary that enforces it. It is
deliberately KLayout-CLI-free: a security test that self-skips on a bare runner
proves nothing, and it must be able to tell "refused" from "klayout missing" --
which is why the runner resolves adapters BEFORE probing the KLayout binary
(the reorder is pinned below).

The refusal marker is the substring "a path was given where a registry id is
required": a bare runner also exits 1 for "klayout not found", so the exit code
alone would not discriminate.
"""

import subprocess
import sys
from pathlib import Path

import pytest

import run_drc

ADK_ROOT = Path(__file__).resolve().parents[1]
DRU_GENERATOR = ADK_ROOT / "kicad" / "dru" / "generate_assembly_dru.py"

if str(ADK_ROOT) not in sys.path:
    sys.path.insert(0, str(ADK_ROOT))
sys.path.insert(0, str(ADK_ROOT / "kicad" / "dru"))

import adk_registry  # noqa: E402
import generate_assembly_dru  # noqa: E402

PATH_MARKER = "a path was given where a registry id is required"

# The two forms the threat model names, on both axes:
#  - an absolute path to a planted deck (the documented, now removed, form);
#  - a shortname that ESCAPES the adapter directory with '..' without looking
#    like a path to a naive check. Under the old directory join
#    '../interconnect/ihp_cupillar' resolved to a real file outside the vetted
#    interposer directory.
ESCAPE_SHORTNAME = "../interconnect/ihp_cupillar"


@pytest.fixture
def planted_deck(tmp_path) -> Path:
    """A real, readable .drc outside every vetted directory.

    It exists on disk on purpose: the old resolver's path branch accepted ANY
    existing .drc, so a refusal here cannot be an accident of the file missing.
    """
    deck = tmp_path / "planted.drc"
    deck.write_text(
        "# Planted adapter. A permissive deck of this shape is the quiet half\n"
        "# of the threat: it would sign off fab-bound geometry.\n"
        "chiplet_attachment_input = polygons(999, 0)\n"
        "drc_rules['ASM_b'] = 0.0\n"
    )
    return deck


# --------------------------------------------------------------------------- #
# run_drc: both axes, both forms, refused at the CLI boundary                   #
# --------------------------------------------------------------------------- #
@pytest.fixture(params=["interposer", "interconnect"])
def runner_resolver(request):
    """The two run_drc resolvers, so every case runs on both axes."""
    return {
        "interposer": run_drc.resolve_adapter,
        "interconnect": run_drc.resolve_interconnect_adapter,
    }[request.param]


def test_absolute_path_is_refused(runner_resolver, planted_deck, caplog):
    with pytest.raises(SystemExit) as exc:
        runner_resolver(str(planted_deck))
    assert exc.value.code == 1
    assert PATH_MARKER in caplog.text, caplog.text


def test_shortname_directory_escape_is_refused(runner_resolver, caplog):
    with pytest.raises(SystemExit) as exc:
        runner_resolver(ESCAPE_SHORTNAME)
    assert exc.value.code == 1
    assert PATH_MARKER in caplog.text, caplog.text


def test_relative_path_is_refused(runner_resolver, planted_deck, caplog, monkeypatch):
    """A CWD-relative deck: the old resolver accepted it with no absolute path
    in sight, which is what made a downloaded archive enough."""
    monkeypatch.chdir(planted_deck.parent)
    with pytest.raises(SystemExit) as exc:
        runner_resolver("./" + planted_deck.name)
    assert exc.value.code == 1
    assert PATH_MARKER in caplog.text, caplog.text


def test_unregistered_plain_id_is_refused(runner_resolver, caplog):
    """A regex-valid id that is simply not registered fails closed (exit 1),
    with the unknown-id message rather than the path marker."""
    with pytest.raises(SystemExit) as exc:
        runner_resolver("definitely_not_registered")
    assert exc.value.code == 1
    assert "unknown adapter id" in caplog.text, caplog.text


# --------------------------------------------------------------------------- #
# The reorder: refusal precedes any tooling probe                               #
# --------------------------------------------------------------------------- #
def test_refusal_precedes_the_klayout_probe(tmp_path, monkeypatch):
    """Adapter resolution must run BEFORE check_klayout_version().

    With the probe monkeypatched to raise, a run carrying a path-shaped adapter
    must still exit 1 (refused) rather than surface the RuntimeError: nothing
    is spawned, and no geometry is read, on a rejected input. Guards the main()
    ordering, which is what lets this whole module stay KLayout-free.
    """
    def _boom():
        raise RuntimeError("check_klayout_version must not be reached")

    monkeypatch.setattr(run_drc, "check_klayout_version", _boom)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "run_drc.py",
        "--path", str(tmp_path / "no_such_layout.gds"),
        "--interposer-adapter", "/abs/evil.drc",
    ])
    with pytest.raises(SystemExit) as exc:
        run_drc.main()
    assert exc.value.code == 1


# --------------------------------------------------------------------------- #
# generate_assembly_dru: the second consumer closes with the same rule          #
# --------------------------------------------------------------------------- #
@pytest.fixture(params=["interposer", "interconnect"])
def dru_resolver(request):
    return {
        "interposer": generate_assembly_dru.resolve_adapter_path,
        "interconnect": generate_assembly_dru.resolve_interconnect_adapter_path,
    }[request.param]


def test_dru_absolute_path_is_refused(dru_resolver, planted_deck):
    with pytest.raises(adk_registry.IdLookupError) as exc:
        dru_resolver(str(planted_deck))
    assert PATH_MARKER in str(exc.value)
    assert adk_registry.exit_code_for(exc.value) == 1


def test_dru_shortname_directory_escape_is_refused(dru_resolver):
    with pytest.raises(adk_registry.IdLookupError) as exc:
        dru_resolver(ESCAPE_SHORTNAME)
    assert PATH_MARKER in str(exc.value)
    assert adk_registry.exit_code_for(exc.value) == 1


def test_dru_cli_refuses_a_path_with_exit_1(tmp_path):
    """End to end through the generator's CLI: a path exits 1 and says why."""
    planted = tmp_path / "planted.drc"
    planted.write_text("drc_rules['ASM_b'] = 1.0\n")
    proc = subprocess.run(
        [sys.executable, str(DRU_GENERATOR),
         "--interposer-adapter", str(planted)],
        capture_output=True, text=True)
    assert proc.returncode == 1
    assert PATH_MARKER in proc.stderr, proc.stderr


# --------------------------------------------------------------------------- #
# Negative controls: the closure refuses paths, not work                        #
# --------------------------------------------------------------------------- #
def test_registered_test_id_still_resolves():
    """The fixture-registry id resolves to a real deck (conftest's
    registry_layer wires tests/fixtures in as the env registry layer)."""
    resolved = Path(run_drc.resolve_adapter("test_interposer"))
    assert resolved.is_file()
    assert resolved.name == "test_interposer_adapter.drc"


@pytest.mark.parametrize("adapter_id", [
    "intm4tm2", "ihp_cupillar", "ihp_sbump", "vendorx_microbump"])
def test_builtin_ids_still_resolve(adapter_id):
    """Every shipped id keeps working on both consumers: the hard break removes
    the path form, not the ids."""
    resolved = Path(run_drc.resolve_adapter(adapter_id))
    assert resolved.is_file()
    assert generate_assembly_dru.resolve_adapter_path(adapter_id) == resolved


# --------------------------------------------------------------------------- #
# run_assembly_drc: the in-process door has the lock too                         #
# --------------------------------------------------------------------------- #
# run_assembly_drc EVALs the adapter and is importable, so an in-process caller
# (Studio, Mosaic) that forwards a document-derived path must be refused at the
# entry, before any KLayout spawn -- the CLI resolver is not the only door.
# PLUG-6 showed an upstream guard can be bypassed, so this is a real leg, not
# belt-and-suspenders. The guard fires with no KLayout binary present.
def test_run_assembly_drc_refuses_an_unvetted_interposer_path(tmp_path):
    with pytest.raises(adk_registry.IdLookupError):
        run_drc.run_assembly_drc(
            str(tmp_path / "x.gds"), "/tmp/planted_evil.drc", "TOP", tmp_path)


def test_run_assembly_drc_refuses_an_unvetted_interconnect_path(tmp_path):
    vetted = str(Path(run_drc.resolve_adapter("test_interposer")))
    with pytest.raises(adk_registry.IdLookupError):
        run_drc.run_assembly_drc(
            str(tmp_path / "x.gds"), vetted, "TOP", tmp_path,
            interconnect_adapter_path="/tmp/planted_evil.drc")
