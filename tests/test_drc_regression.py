"""ADK assembly DRC regression tests.

Each test invokes the standalone runner via subprocess against a synthesized
fixture and asserts the expected ASM rule set. PDK-independent: uses the
synthetic adapter at tests/fixtures/test_interposer_adapter.drc.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from run_drc import get_rules_with_violations

ADK_ROOT = Path(__file__).resolve().parents[1]
RUNNER = ADK_ROOT / "klayout" / "drc" / "run_drc.py"


def _run(layout: Path, adapter: Path, run_dir: Path,
         report: Path, legacy: bool = False) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable, str(RUNNER),
        "--path", str(layout),
        "--interposer-adapter", str(adapter),
        "--run_dir", str(run_dir),
        "--report", str(report),
    ]
    if legacy:
        cmd.append("--legacy-exchange0")
    return subprocess.run(cmd, capture_output=True, text=True)


def _violations(layout: Path, adapter: Path, run_dir: Path,
                legacy: bool = False) -> set:
    run_dir = Path(run_dir)
    report = run_dir / "report.lyrdb"
    proc = _run(layout, adapter, run_dir, report, legacy=legacy)
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


# Standard fixtures whose authoritative manifest matches their GDS exchange0
# geometry, so manifest-native and legacy modes must agree exactly.
_STANDARD = [
    "assembly_ok", "assembly_overlap", "assembly_too_close",
    "assembly_too_small", "assembly_pad_outside",
]


def test_legacy_and_manifest_modes_agree(fixture_layouts, test_adapter, tmp_path):
    """For every standard fixture the manifest-native path and the legacy
    exchange0 path must report the identical violation set (the manifest is
    built to match the GDS exchange0 geometry). This is the migration
    equivalence gate: switching the boundary source must not change results."""
    for i, name in enumerate(_STANDARD):
        manifest_v = _violations(
            fixture_layouts[name], test_adapter, tmp_path / f"m_{i}")
        legacy_v = _violations(
            fixture_layouts[name], test_adapter, tmp_path / f"l_{i}", legacy=True)
        assert manifest_v == legacy_v, (
            f"{name}: manifest {sorted(manifest_v)} != legacy {sorted(legacy_v)}"
        )


def test_collision_internal_exchange0_ignored_in_manifest_mode(
        fixture_layouts, test_adapter, tmp_path):
    """The collision fixture has overlapping exchange0 boxes in the GDS but a
    clean manifest. Manifest-native mode must be clean (it never reads the fab
    layer); legacy mode must see ASM.a. This is the collision-proofness gate."""
    manifest_v = _violations(
        fixture_layouts["collision_internal_exchange0"], test_adapter,
        tmp_path / "m")
    assert manifest_v == set(), (
        "Manifest mode must ignore the GDS exchange0 geometry; "
        f"got {sorted(manifest_v)}"
    )
    legacy_v = _violations(
        fixture_layouts["collision_internal_exchange0"], test_adapter,
        tmp_path / "l", legacy=True)
    assert "ASM.a" in legacy_v, (
        f"Legacy mode reads the overlapping exchange0; expected ASM.a, "
        f"got {sorted(legacy_v)}"
    )


def test_runner_aborts_without_manifest(fixture_layouts, test_adapter, tmp_path):
    """A GDS with no boundary manifest must make the runner exit non-zero in
    manifest mode (never a vacuous pass). The error must name the manifest."""
    orphan = tmp_path / "orphan.gds"
    shutil.copy(fixture_layouts["assembly_ok"], orphan)  # GDS only, not its sidecar
    report = tmp_path / "report.lyrdb"
    proc = _run(orphan, test_adapter, tmp_path, report)
    assert proc.returncode != 0, (
        "Runner must abort when no manifest is present.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    combined = (proc.stdout + proc.stderr).lower()
    assert "manifest" in combined, (
        f"Error must mention the missing manifest. Output:\n{combined}"
    )
