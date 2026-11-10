# SPDX-License-Identifier: Apache-2.0
"""The runner's exit-code contract, with no KLayout spawn.

`tests/test_interconnect_axis.py` proves the mapping end to end on a real deck
abort, but it needs the KLayout CLI and a full DRC run, so the whole module
skips on a bare checkout and the contract goes unpinned exactly where it is
cheapest to break. These tests drive `check_drc_results()` and `main()` in
process, stubbing the deck spawn and the tooling probe, so 0/1/2 stays pinned
wherever pytest runs.

Scope, mirroring the runner's own comment: only the deck paths are covered
here. Preflight and resolution failures (argument validation, adapter and
registry resolution) keep their own codes and are covered elsewhere
(`test_run_drc_unit.py`, `test_adapter_id_only.py`).
"""
import argparse
import json
import logging
import subprocess
import sys

import pytest

import run_drc
from run_drc import EXIT_CLEAN, EXIT_ENGINE, EXIT_FINDINGS, check_drc_results


@pytest.fixture(autouse=True)
def _root_logging_restored():
    """main() calls logging.basicConfig. That is a no-op while pytest's own
    root handler is installed, which is the normal case, but do not leave it to
    chance: snapshot the root logger so no handler this module provokes can
    leak into the rest of the suite."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


# --- check_drc_results: a run that wrote no report --------------------------

def test_missing_report_exits_engine_failure(tmp_path, caplog):
    """A deck that exits 0 without writing its report gives no verdict either.
    The runner used to exit 1 here, which reads as findings."""
    missing = tmp_path / "never_written.lyrdb"
    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as excinfo:
            check_drc_results(missing)
    assert excinfo.value.code == EXIT_ENGINE
    assert "ADK DRC engine failure" in caplog.text
    assert str(missing) in caplog.text


# --- main(): the deck failed vs the deck finished ---------------------------


def _prepare_main(monkeypatch, tmp_path):
    """Put main() on its own path up to the deck spawn.

    Stubbed: argv (the --list-adapters shortcut), argument parsing, interposer
    adapter resolution and the KLayout version probe, which shells the binary.
    Everything else is real: the layout file, the boundary manifest and the
    validators that read them, so main() walks its own preconditions instead of
    a mock of them. Returns the namespace for a caller that needs to retune it.
    """
    layout = tmp_path / "assembly.gds"
    layout.write_bytes(b"")  # never read: --topcell is explicit below
    manifest = tmp_path / "assembly.boundaries.json"
    manifest.write_text(json.dumps({
        "schema": "adk-boundary-manifest",
        "version": run_drc.SUPPORTED_MANIFEST_VERSION,
        "generator": "test_run_drc_exit_codes",
        "assembly_gds": layout.name,
        "dbu_um": 0.001,
        "top_cell": "TOP",
        "boundaries": [{"instance": "U1", "source_die": "TEST",
                        "class": "chiplet",
                        "polygon_dbu": [[0, 0], [10, 0], [10, 10], [0, 10]]}],
    }))

    args = argparse.Namespace(
        path=str(layout),
        interposer_adapter="test_interposer",
        interconnect_adapter=None,
        interconnect_methods=None,
        list_adapters=False,
        topcell="TOP",
        run_dir=str(tmp_path / "run"),
        threads=4,
        run_mode="tiling",
        report=str(tmp_path / "run" / "report.lyrdb"),
        manifest=None,
        legacy_exchange0=False,
    )

    monkeypatch.setattr(sys, "argv", ["run_drc.py"])
    monkeypatch.setattr(run_drc, "parse_args", lambda: args)
    # The resolved value only travels to the (stubbed) deck spawn, so a marker
    # keeps this module off the adapter axis entirely; id-only resolution has
    # its own tests.
    monkeypatch.setattr(run_drc, "resolve_adapter", lambda _id: "<stub adapter>")
    monkeypatch.setattr(run_drc, "check_klayout_version", lambda: None)
    return args


def _record_check_drc_results(monkeypatch, violations=frozenset()):
    """Replace check_drc_results with a recorder. main() must not reach it on
    an engine failure, and calling the real one there would parse a report that
    an aborted run never wrote."""
    calls = []

    def _recorder(report_path):
        calls.append(report_path)
        return set(violations)

    monkeypatch.setattr(run_drc, "check_drc_results", _recorder)
    return calls


@pytest.mark.parametrize("returncode", [1, 3, -9])
def test_deck_failure_maps_to_exit_2(monkeypatch, tmp_path, caplog, returncode):
    """Whatever code the deck died with, the runner exits 2. 1 is the code
    KLayout uses for a raise and the one this runner uses for findings, 3 is an
    arbitrary tooling code, and -9 is the signal-style code Python reports for
    a killed child (an OOM'd deck). None of them is a verdict, so none is
    passed through, and the report is never consulted: an aborted run wrote
    nothing to consult. Restoring `return e.returncode or 2` turns the 1 and 3
    cases red."""
    _prepare_main(monkeypatch, tmp_path)
    calls = _record_check_drc_results(monkeypatch)

    def _deck_failed(*args, **kwargs):
        # cmd is only the exception's metadata; nothing is spawned here.
        raise subprocess.CalledProcessError(returncode, ["klayout", "-b"])

    monkeypatch.setattr(run_drc, "run_assembly_drc", _deck_failed)

    with caplog.at_level(logging.DEBUG):
        assert run_drc.main() == EXIT_ENGINE

    assert calls == [], "the report must not be read after an engine failure"
    assert "ADK DRC engine failure" in caplog.text
    assert str(returncode) in caplog.text
    # No verdict was reached, so nothing may look like one in the log.
    assert "ADK DRC PASSED" not in caplog.text
    assert "ADK DRC FAILED" not in caplog.text
    assert "Total DRC time" not in caplog.text


@pytest.mark.parametrize("violations, expected", [
    ({"ASM.a", "IXN.b"}, EXIT_FINDINGS),
    (set(), EXIT_CLEAN),
])
def test_completed_run_returns_its_verdict(monkeypatch, tmp_path, caplog,
                                           violations, expected):
    """The deck ran to the end and the report was read: 1 with items, 0
    without. The completion line is asserted here so its ABSENCE in the engine
    failure case above means something."""
    _prepare_main(monkeypatch, tmp_path)
    calls = _record_check_drc_results(monkeypatch, violations)
    report = tmp_path / "run" / "report.lyrdb"
    monkeypatch.setattr(run_drc, "run_assembly_drc",
                        lambda *args, **kwargs: report)

    with caplog.at_level(logging.DEBUG):
        assert run_drc.main() == expected

    assert calls == [report]
    assert "Total DRC time" in caplog.text
