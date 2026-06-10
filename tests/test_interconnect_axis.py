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
pytestmark = pytest.mark.skipif(
    shutil.which("klayout") is None,
    reason="klayout CLI not on PATH (runner shells `klayout -b`)")
TEST_ADAPTER = Path(__file__).resolve().parent / "fixtures" / "test_interposer_adapter.drc"

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
        "--interposer-adapter", str(TEST_ADAPTER),
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
