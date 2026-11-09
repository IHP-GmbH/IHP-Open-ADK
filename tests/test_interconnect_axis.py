# SPDX-License-Identifier: Apache-2.0
"""ADK interconnect-axis (IXN) regression tests.

Exercises the optional second adapter axis. With --interconnect-adapter the
bump-to-bump IXN.b/IXN.e rules run over chiplet_attachment_input; without it no
IXN rule runs (identical to the interposer-only path). Also demonstrates method
modularity: the SAME geometry fails under the IHP cu-pillar adapter and passes
under the fine-pitch vendor adapter -- the method rules travel with the adapter,
not the interposer.

Self-contained: synthesizes its own GDS + boundary manifest, no committed
binaries and no real PDK.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import klayout.db as kdb
import pytest

from run_drc import get_rules_with_violations

ADK_ROOT = Path(__file__).resolve().parents[1]
RUNNER = ADK_ROOT / "klayout" / "drc" / "run_drc.py"

# The runner shells `klayout -b` for the deck itself; on a bare checkout
# without the KLayout binary (e.g. a CI runner) the whole module skips.
# The in-image verify gate has the binary and runs everything.
pytestmark = [
    pytest.mark.klayout_cli,
    pytest.mark.skipif(
        shutil.which("klayout") is None,
        reason="klayout CLI not on PATH (runner shells `klayout -b`)"),
]
# Adapters are named by registry id only; tests/fixtures/registry.json (wired
# in by conftest's session registry_layer fixture) declares this one.
TEST_ADAPTER_ID = "test_interposer"

ATTACHMENT_LAYER = (999, 0)  # must match test_interposer_adapter.drc


def _um(v):
    return int(round(v * 1000))


def _build_close_pads_gds(out: Path):
    """One large chiplet with two attachment pads 20 um apart (edge-to-edge),
    60 um pitch. All ASM rules are clean (single 500x500 chiplet, pads inside),
    so only the IXN axis can flag this -- and only under a coarse method."""
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("TOP")
    pad = ly.layer(*ATTACHMENT_LAYER)
    # pad1 40x40 at x[100,140], pad2 40x40 at x[160,200]: spacing 20 um, pitch 60 um.
    top.shapes(pad).insert(kdb.Box(_um(100), _um(100), _um(140), _um(140)))
    top.shapes(pad).insert(kdb.Box(_um(160), _um(100), _um(200), _um(140)))
    ly.write(str(out))
    manifest = {
        "schema": "adk-boundary-manifest",
        "version": "1.0.0",
        "generator": "test_interconnect_axis",
        "assembly_gds": out.name,
        "dbu_um": 0.001,
        "top_cell": "TOP",
        "boundaries": [{
            "instance": "U1",
            "source_die": "TEST",
            "class": "chiplet",
            "polygon_dbu": [
                [_um(0), _um(0)], [_um(500), _um(0)],
                [_um(500), _um(500)], [_um(0), _um(500)],
            ],
        }],
    }
    out.with_name(out.stem + ".boundaries.json").write_text(json.dumps(manifest, indent=2))


def _run(layout, run_dir, report, interconnect_adapter=None,
         interconnect_methods=None):
    cmd = [
        sys.executable, str(RUNNER),
        "--path", str(layout),
        "--interposer-adapter", TEST_ADAPTER_ID,
        "--run_dir", str(run_dir),
        "--report", str(report),
    ]
    if interconnect_adapter:
        cmd += ["--interconnect-adapter", interconnect_adapter]
    if interconnect_methods:
        cmd += ["--interconnect-methods", str(interconnect_methods)]
    return subprocess.run(cmd, capture_output=True, text=True)


def _violations(layout, run_dir, interconnect_adapter=None,
                interconnect_methods=None):
    report = Path(run_dir) / "report.lyrdb"
    proc = _run(layout, run_dir, report, interconnect_adapter,
                interconnect_methods)
    assert report.is_file(), (
        f"No report generated.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return get_rules_with_violations(report)


def _checked(layout, run_dir, expect_exit, interconnect_adapter=None,
             interconnect_methods=None):
    """Run the runner and require its NATIVE exit: 1 when a finding is
    expected, 0 when the run must be clean. Unlike _violations, a crashed or
    aborted run (any other exit, or no report) fails HERE instead of reading
    as 'no violations' from a missing or partial report, and the exit must
    agree with the report's items (exit 1 iff some rule has items)."""
    report = Path(run_dir) / "report.lyrdb"
    proc = _run(layout, run_dir, report, interconnect_adapter,
                interconnect_methods)
    assert proc.returncode == expect_exit, (
        f"exit {proc.returncode}, expected {expect_exit}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    assert report.is_file(), (
        f"No report generated.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    rules = get_rules_with_violations(report)
    assert bool(rules) == (expect_exit == 1), (
        f"exit {proc.returncode} disagrees with the report items: {sorted(rules)}")
    return rules


def test_no_interconnect_adapter_runs_no_ixn(tmp_path):
    """Without --interconnect-adapter, no IXN rule runs (interposer-only path)."""
    gds = tmp_path / "close_pads.gds"
    _build_close_pads_gds(gds)
    v = _violations(gds, tmp_path / "run")
    assert not any(r.startswith("IXN") for r in v), (
        f"IXN must not run without an interconnect adapter, got {sorted(v)}"
    )


def test_ihp_cupillar_flags_close_pads(tmp_path):
    """ihp_cupillar (40 um spacing / 75 um pitch) flags pads only 20 um apart."""
    gds = tmp_path / "close_pads.gds"
    _build_close_pads_gds(gds)
    v = _violations(gds, tmp_path / "run", interconnect_adapter="ihp_cupillar")
    assert "IXN.b" in v, f"Expected IXN.b under ihp_cupillar, got {sorted(v)}"
    assert "IXN.e" in v, f"Expected IXN.e under ihp_cupillar, got {sorted(v)}"


def test_vendorx_microbump_accepts_same_geometry(tmp_path):
    """The SAME 20 um-spacing geometry passes under the fine-pitch vendor
    adapter: swap the interconnect adapter, change the verdict, same layout."""
    gds = tmp_path / "close_pads.gds"
    _build_close_pads_gds(gds)
    v = _violations(gds, tmp_path / "run", interconnect_adapter="vendorx_microbump")
    assert not any(r.startswith("IXN") for r in v), (
        f"Fine-pitch vendor must accept 20 um spacing, got {sorted(v)}"
    )


# ---------------------------------------------------------------------------
# Per-method mode (--interconnect-methods): each method's numbers run only on
# the attachment pads under ITS dies' boundaries; pads of different methods
# get a conservative cross-method spacing; pads outside every method region
# keep the assembly-global adapter numbers.
# ---------------------------------------------------------------------------

COARSE = {"IXN_spacing": 40.0, "IXN_pitch": 75.0, "IXN_pad_size": 35.0}
FINE = {"IXN_spacing": 15.0, "IXN_pitch": 50.0, "IXN_pad_size": 35.0}


def _boundary(instance, x0, y0, x1, y1):
    return {
        "instance": instance,
        "source_die": "TEST",
        "class": "chiplet",
        "polygon_dbu": [
            [_um(x0), _um(y0)], [_um(x1), _um(y0)],
            [_um(x1), _um(y1)], [_um(x0), _um(y1)],
        ],
    }


def _write_sidecars(gds: Path, boundaries, methods):
    manifest = {
        "schema": "adk-boundary-manifest",
        "version": "1.0.0",
        "generator": "test_interconnect_axis",
        "assembly_gds": gds.name,
        "dbu_um": 0.001,
        "top_cell": "TOP",
        "boundaries": boundaries,
    }
    gds.with_name(gds.stem + ".boundaries.json").write_text(
        json.dumps(manifest, indent=2))
    methods_file = gds.with_name(gds.stem + ".ixn_methods.json")
    methods_file.write_text(json.dumps({
        "schema": "adk-ixn-methods",
        "version": "1.0.0",
        "assembly_gds": gds.name,
        "methods": methods,
    }, indent=2))
    return methods_file


def _build_mixed_methods_gds(out: Path):
    """U1 (coarse method) and U2 (fine method), far apart. Both carry a pad
    pair 20 um apart: that violates the coarse numbers (40/75) and satisfies
    the fine ones (15/50) -- only a per-method scope can tell them apart."""
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("TOP")
    pad = ly.layer(*ATTACHMENT_LAYER)
    # U1 pads: 40x40 at x[100,140] and x[160,200] -> 20 um space, 60 um pitch.
    top.shapes(pad).insert(kdb.Box(_um(100), _um(100), _um(140), _um(140)))
    top.shapes(pad).insert(kdb.Box(_um(160), _um(100), _um(200), _um(140)))
    # U2 pads: same pattern shifted +1000 um in x.
    top.shapes(pad).insert(kdb.Box(_um(1100), _um(100), _um(1140), _um(140)))
    top.shapes(pad).insert(kdb.Box(_um(1160), _um(100), _um(1200), _um(140)))
    ly.write(str(out))
    return _write_sidecars(out, [
        _boundary("U1", 0, 0, 500, 500),
        _boundary("U2", 1000, 0, 1500, 500),
    ], {
        "coarse_method": dict(COARSE, dies=["U1"]),
        "fine_method": dict(FINE, dies=["U2"]),
    })


def test_per_method_scopes_rules_to_each_dies_pads(tmp_path):
    """Same 20 um geometry on both dies: the coarse method flags ITS die's
    pads, the fine method accepts ITS OWN; the global IXN.b/IXN.e names do
    not appear in per-method mode."""
    gds = tmp_path / "mixed.gds"
    methods = _build_mixed_methods_gds(gds)
    v = _violations(gds, tmp_path / "run", interconnect_methods=methods)
    assert "IXN.b.coarse_method" in v, f"got {sorted(v)}"
    assert "IXN.e.coarse_method" in v, f"got {sorted(v)}"
    assert not any(r.endswith("fine_method") and not r.startswith("IXN.x")
                   for r in v), f"fine method must pass on its die, got {sorted(v)}"
    assert "IXN.b" not in v and "IXN.e" not in v, (
        f"global IXN names must not appear in per-method mode, got {sorted(v)}"
    )
    # Dies are ~800 um apart: no cross-method proximity.
    assert not any(r.startswith("IXN.x") for r in v), f"got {sorted(v)}"


def test_per_method_cross_method_spacing(tmp_path):
    """A coarse-method pad 20 um from a fine-method pad violates the
    cross-method check (max of the two spacings = 40 um), even though each
    method's intra-region check cannot see the other's pads."""
    gds = tmp_path / "cross.gds"
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("TOP")
    pad = ly.layer(*ATTACHMENT_LAYER)
    # One pad per die, 20 um apart across adjacent boundaries.
    top.shapes(pad).insert(kdb.Box(_um(460), _um(100), _um(500), _um(140)))
    top.shapes(pad).insert(kdb.Box(_um(520), _um(100), _um(560), _um(140)))
    ly.write(str(gds))
    methods = _write_sidecars(gds, [
        _boundary("U1", 0, 0, 500, 500),
        _boundary("U2", 520, 0, 1020, 500),
    ], {
        "coarse_method": dict(COARSE, dies=["U1"]),
        "fine_method": dict(FINE, dies=["U2"]),
    })
    v = _violations(gds, tmp_path / "run", interconnect_methods=methods)
    assert "IXN.x.coarse_method.fine_method" in v, f"got {sorted(v)}"
    # A single pad per region: no intra-method spacing violations.
    assert "IXN.b.coarse_method" not in v, f"got {sorted(v)}"
    assert "IXN.b.fine_method" not in v, f"got {sorted(v)}"


def test_per_method_unclaimed_pads_use_adapter_numbers(tmp_path):
    """Pads outside every method region fall back to the assembly-global
    adapter numbers (IXN.b.unclaimed) when an adapter is loaded."""
    gds = tmp_path / "unclaimed.gds"
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("TOP")
    pad = ly.layer(*ATTACHMENT_LAYER)
    # Claimed (fine) pads, fine-clean.
    top.shapes(pad).insert(kdb.Box(_um(100), _um(100), _um(140), _um(140)))
    # Unclaimed pair 20 um apart, far outside the boundary.
    top.shapes(pad).insert(kdb.Box(_um(2000), _um(100), _um(2040), _um(140)))
    top.shapes(pad).insert(kdb.Box(_um(2060), _um(100), _um(2100), _um(140)))
    ly.write(str(gds))
    methods = _write_sidecars(gds, [
        _boundary("U1", 0, 0, 500, 500),
    ], {
        "fine_method": dict(FINE, dies=["U1"]),
    })
    v = _violations(gds, tmp_path / "run",
                    interconnect_adapter="ihp_cupillar",
                    interconnect_methods=methods)
    assert "IXN.b.unclaimed" in v, f"got {sorted(v)}"
    assert "IXN.e.unclaimed" in v, f"got {sorted(v)}"
    # Without an adapter the unclaimed pads are not checked on this axis.
    v = _violations(gds, tmp_path / "run_noadapter",
                    interconnect_methods=methods)
    assert not any(r.startswith("IXN.b.unclaimed") for r in v), f"got {sorted(v)}"


# ---------------------------------------------------------------------------
# Third shipped adapter coverage + new fail-loud deck guards
# ---------------------------------------------------------------------------

def test_ihp_sbump_flags_close_pads(tmp_path):
    """ihp_sbump (70 um spacing / 130 um pitch / 60 um pad) flags pads 20 um
    apart. The third shipped interconnect adapter, previously untested in DRC."""
    gds = tmp_path / "close_pads.gds"
    _build_close_pads_gds(gds)
    v = _violations(gds, tmp_path / "run", interconnect_adapter="ihp_sbump")
    assert "IXN.b" in v, f"Expected IXN.b under ihp_sbump, got {sorted(v)}"
    assert "IXN.e" in v, f"Expected IXN.e under ihp_sbump, got {sorted(v)}"


def _single_pad_gds(out: Path):
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("TOP")
    pad = ly.layer(*ATTACHMENT_LAYER)
    top.shapes(pad).insert(kdb.Box(_um(100), _um(100), _um(140), _um(140)))
    ly.write(str(out))


def test_per_method_unknown_die_aborts(tmp_path):
    """A method referencing a die instance absent from the boundary manifest
    must fail loud, not silently drop that die's pads from its region."""
    gds = tmp_path / "u.gds"
    _single_pad_gds(gds)
    methods = _write_sidecars(gds, [_boundary("U1", 0, 0, 500, 500)],
                              {"m": dict(FINE, dies=["U_MISSING"])})
    report = tmp_path / "run" / "report.lyrdb"
    proc = _run(gds, tmp_path / "run", report, interconnect_methods=methods)
    assert proc.returncode != 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "U_MISSING" in (proc.stdout + proc.stderr)


def test_per_method_pitch_not_exceeding_pad_aborts(tmp_path):
    """A method whose IXN_pitch <= IXN_pad_size yields a non-positive
    pitch->space threshold; the deck must reject it, not silently no-op."""
    gds = tmp_path / "p.gds"
    _single_pad_gds(gds)
    bad = {"IXN_spacing": 40.0, "IXN_pitch": 30.0, "IXN_pad_size": 35.0,
           "dies": ["U1"]}
    methods = _write_sidecars(gds, [_boundary("U1", 0, 0, 500, 500)], {"m": bad})
    report = tmp_path / "run" / "report.lyrdb"
    proc = _run(gds, tmp_path / "run", report, interconnect_methods=methods)
    assert proc.returncode != 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "must exceed" in (proc.stdout + proc.stderr)


# ---------------------------------------------------------------------------
# Named-vs-unclaimed pair coverage. IXN.b.<mid> sees only its own
# method's pads and IXN.b.unclaimed only unclaimed-vs-unclaimed, so a claimed
# pad too close to an unclaimed one was checked by nothing and signed off
# clean in per-method mode (a physical false clean). The deck now checks each
# named method against the unclaimed pads with the stricter of the method's
# and the adapter's spacing, and reports unclaimed pads as IXN.unassociated
# when no adapter defines their rule.
# ---------------------------------------------------------------------------

def _claimed_unclaimed_gds(out: Path, unclaimed_x0: float, method_rules):
    """One 35x35 pad entirely inside U1 (claimed by the method), one 35x35 pad
    entirely outside every boundary (unclaimed) whose left edge is at
    unclaimed_x0. U1 spans x 0..500; the claimed pad ends at x=495, so the
    edge gap is unclaimed_x0 - 495. Mirrors the audit's pair_coverage cases."""
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("TOP")
    pad = ly.layer(*ATTACHMENT_LAYER)
    top.shapes(pad).insert(kdb.Box(_um(460), _um(100), _um(495), _um(135)))
    top.shapes(pad).insert(kdb.Box(_um(unclaimed_x0), _um(100),
                                   _um(unclaimed_x0 + 35), _um(135)))
    ly.write(str(out))
    return _write_sidecars(out, [_boundary("U1", 0, 0, 500, 500)],
                           {"the_method": dict(method_rules, dies=["U1"])})


def test_named_vs_unclaimed_close_pair_is_flagged(tmp_path):
    """The named-vs-unclaimed regression: a claimed pad 20 um from an unclaimed pad, with
    an adapter loaded (40 um spacing), must be flagged in per-method mode.
    Neither intra-region check can see this pair; the cross check must.
    Red on the deck before the fix (exit 0, zero markers), green after."""
    gds = tmp_path / "split.gds"
    methods = _claimed_unclaimed_gds(gds, 515, COARSE)   # 495 -> 515: 20 um gap
    v = _checked(gds, tmp_path / "run", 1,
                 interconnect_adapter="ihp_cupillar",
                 interconnect_methods=methods)
    assert "IXN.x.the_method.unclaimed" in v, f"got {sorted(v)}"
    # One pad per side: no intra-region violations, and no global names.
    assert "IXN.b.the_method" not in v, f"got {sorted(v)}"
    assert "IXN.b.unclaimed" not in v, f"got {sorted(v)}"
    assert "IXN.b" not in v and "IXN.e" not in v, f"got {sorted(v)}"


def test_named_vs_unclaimed_far_pair_is_clean(tmp_path):
    """Negative control: the same split with the unclaimed pad 105 um away
    (well over the 40 um spacing) reports no IXN violation at all."""
    gds = tmp_path / "far.gds"
    methods = _claimed_unclaimed_gds(gds, 600, COARSE)   # 495 -> 600: 105 um gap
    v = _checked(gds, tmp_path / "run", 0,
                 interconnect_adapter="ihp_cupillar",
                 interconnect_methods=methods)
    assert not any(r.startswith("IXN") for r in v), f"got {sorted(v)}"


def test_named_vs_unclaimed_uses_stricter_of_method_and_adapter(tmp_path):
    """The cross rule is max(method spacing, adapter spacing), mirroring
    IXN.x.<m1>.<m2>. A FINE method (15 um) would accept a 20 um gap on its
    own; with the coarse adapter (40 um) loaded the pair must still fail."""
    gds = tmp_path / "strict.gds"
    methods = _claimed_unclaimed_gds(gds, 515, FINE)     # 20 um gap, fine method
    v = _checked(gds, tmp_path / "run", 1,
                 interconnect_adapter="ihp_cupillar",
                 interconnect_methods=methods)
    assert "IXN.x.the_method.unclaimed" in v, (
        f"adapter spacing (40) must dominate the fine method (15), got {sorted(v)}")


def test_no_adapter_unclaimed_pads_are_reported_unassociated(tmp_path):
    """Without an adapter the unclaimed pads have no rule at all, so a
    named-vs-unclaimed pair is unverifiable. The deck must say so as a
    finding (exit 1), not log a line and sign off clean. No number is
    invented: no cross-pair rule fires without an adapter."""
    gds = tmp_path / "noadapter.gds"
    methods = _claimed_unclaimed_gds(gds, 515, COARSE)
    v = _checked(gds, tmp_path / "run", 1, interconnect_methods=methods)
    assert "IXN.unassociated" in v, f"got {sorted(v)}"
    assert not any(r.startswith("IXN.x") for r in v), f"got {sorted(v)}"


def test_no_adapter_all_pads_claimed_has_no_unassociated_finding(tmp_path):
    """Negative control for the finding: when every pad is claimed there are
    no unclaimed pads to report, adapter or not."""
    gds = tmp_path / "allclaimed.gds"
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("TOP")
    pad = ly.layer(*ATTACHMENT_LAYER)
    top.shapes(pad).insert(kdb.Box(_um(100), _um(100), _um(135), _um(135)))
    ly.write(str(gds))
    methods = _write_sidecars(gds, [_boundary("U1", 0, 0, 500, 500)],
                              {"the_method": dict(COARSE, dies=["U1"])})
    v = _checked(gds, tmp_path / "run", 0, interconnect_methods=methods)
    assert "IXN.unassociated" not in v, f"got {sorted(v)}"


def test_abort_satisfies_neither_verdict(tmp_path):
    """Process-failure control for the helper the cases above rely on. A run
    that aborts inside the deck (methods file naming a die the manifest does
    not have) must satisfy NEITHER verdict: not 'clean' (exit 0, empty report)
    and not 'finding' (exit 1 with items). Whatever exit KLayout uses for a
    raise, _checked must go red both ways instead of handing back an empty
    rule set that reads as a clean run."""
    gds = tmp_path / "abort.gds"
    _claimed_unclaimed_gds(gds, 515, COARSE)
    methods = _write_sidecars(gds, [_boundary("U1", 0, 0, 500, 500)],
                              {"the_method": dict(COARSE, dies=["U9"])})
    for expect in (0, 1):
        with pytest.raises(AssertionError):
            _checked(gds, tmp_path / f"run{expect}", expect,
                     interconnect_adapter="ihp_cupillar",
                     interconnect_methods=methods)


# The exact raise 8_2_interconnect.drc emits for the stimulus below: methods
# naming die U9 while the manifest declares only U1. Pinning it (die names
# included) is what keeps the test about THIS abort: a missing adapter, a
# broken KLayout or any other engine failure also exits 2 with the runner's
# banner, and would otherwise satisfy the case without the deck ever reaching
# its manifest cross-check.
ABORT_DECK_DIAGNOSTIC = (
    "Per-method interconnect references die instance(s) not in the boundary "
    "manifest: U9. Manifest instances: U1."
)


def test_abort_exits_2_with_an_engine_diagnostic(tmp_path):
    """A deck abort is an ENGINE failure, not a verdict. KLayout
    itself exits 1 on a raise, the code this runner uses for findings, and the
    aborted run leaves an empty report that reads as clean; the runner must
    therefore exit 2 with a diagnostic naming the failure, never 0 or 1.
    Both diagnostics are required: the runner's own banner, and the deck's
    raise text for this stimulus (a method referencing die U9 against a
    manifest holding only U1), so a generic engine failure from another cause
    cannot pass the test.
    Reverting the mapping in run_drc.py (returning the deck's own code) turns
    this test red on the exit assertion; that is the sensitivity witness."""
    gds = tmp_path / "abort.gds"
    _claimed_unclaimed_gds(gds, 515, COARSE)
    methods = _write_sidecars(gds, [_boundary("U1", 0, 0, 500, 500)],
                              {"the_method": dict(COARSE, dies=["U9"])})
    run_dir = tmp_path / "run_abort"
    report = run_dir / "report.lyrdb"
    proc = _run(gds, run_dir, report, interconnect_adapter="ihp_cupillar",
                interconnect_methods=methods)
    assert proc.returncode == 2, (
        f"exit {proc.returncode}, expected 2 (engine failure)\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    assert "ADK DRC engine failure" in proc.stderr, proc.stderr
    # The deck's raise, compared on collapsed whitespace so a line break in
    # KLayout's error formatting cannot hide it. Channel is not pinned: only
    # the runner's banner has a proven stream, the deck's error is KLayout's.
    combined = " ".join((proc.stdout + "\n" + proc.stderr).split())
    assert ABORT_DECK_DIAGNOSTIC in combined, (
        f"deck diagnostic {ABORT_DECK_DIAGNOSTIC!r} not found\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    # Whatever the aborted deck left behind carries no finding.
    assert not report.is_file() or not get_rules_with_violations(report)
