"""ADK assembly DRC regression tests.

Each test invokes the standalone runner via subprocess against a synthesized
fixture and asserts the expected ASM rule set. PDK-independent: uses the
synthetic adapter at tests/fixtures/test_interposer_adapter.drc.
"""

import subprocess
import sys
from pathlib import Path

from run_drc import get_rules_with_violations

ADK_ROOT = Path(__file__).resolve().parents[1]
RUNNER = ADK_ROOT / "klayout" / "drc" / "run_drc.py"


def _run(layout: Path, adapter: Path, run_dir: Path,
         report: Path) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable, str(RUNNER),
        "--path", str(layout),
        "--interposer-adapter", str(adapter),
        "--run_dir", str(run_dir),
        "--report", str(report),
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def _violations(layout: Path, adapter: Path, tmp_path: Path) -> set:
    report = tmp_path / "report.lyrdb"
    proc = _run(layout, adapter, tmp_path, report)
    assert report.is_file(), (
        f"Report not generated: {report}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return get_rules_with_violations(report)


def test_assembly_ok_has_no_violations(fixture_layouts, test_adapter, tmp_path):
    violations = _violations(
        fixture_layouts["assembly_ok"], test_adapter, tmp_path
    )
    assert violations == set(), (
        f"Expected no violations on assembly_ok, got: {sorted(violations)}"
    )


def test_assembly_overlap_triggers_asm_a(fixture_layouts, test_adapter, tmp_path):
    violations = _violations(
        fixture_layouts["assembly_overlap"], test_adapter, tmp_path
    )
    assert "ASM.a" in violations, (
        f"Expected ASM.a on assembly_overlap, got: {sorted(violations)}"
    )


def test_assembly_too_close_triggers_asm_b(fixture_layouts, test_adapter, tmp_path):
    violations = _violations(
        fixture_layouts["assembly_too_close"], test_adapter, tmp_path
    )
    assert violations == {"ASM.b"}, (
        f"Expected ASM.b only on assembly_too_close, got: {sorted(violations)}"
    )


def test_assembly_too_small_triggers_asm_e(fixture_layouts, test_adapter, tmp_path):
    violations = _violations(
        fixture_layouts["assembly_too_small"], test_adapter, tmp_path
    )
    assert violations == {"ASM.e"}, (
        f"Expected ASM.e only on assembly_too_small, got: {sorted(violations)}"
    )


def test_assembly_pad_outside_triggers_asm_f(fixture_layouts, test_adapter, tmp_path):
    violations = _violations(
        fixture_layouts["assembly_pad_outside"], test_adapter, tmp_path
    )
    assert violations == {"ASM.f"}, (
        f"Expected ASM.f only on assembly_pad_outside, got: {sorted(violations)}"
    )


def test_runner_aborts_when_adapter_missing_required_input(
        fixture_layouts, tmp_path):
    """An adapter that does NOT declare chiplet_attachment_input must cause
    the runner to exit non-zero with an error naming the missing input."""
    bad_adapter = tmp_path / "broken_adapter.drc"
    bad_adapter.write_text(
        "# Deliberately broken adapter: declares the wrong name.\n"
        "wrong_name = polygons(999, 0)\n"
    )
    report = tmp_path / "report.lyrdb"
    proc = _run(fixture_layouts["assembly_ok"], bad_adapter, tmp_path, report)
    assert proc.returncode != 0, (
        "Runner must abort when adapter is missing required input.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    combined = proc.stdout + proc.stderr
    assert "chiplet_attachment_input" in combined, (
        f"Error message must name the missing input. Combined output:\n{combined}"
    )
