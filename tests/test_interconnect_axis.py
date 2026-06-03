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
import subprocess
import sys
from pathlib import Path

import klayout.db as kdb

from run_drc import get_rules_with_violations

ADK_ROOT = Path(__file__).resolve().parents[1]
RUNNER = ADK_ROOT / "klayout" / "drc" / "run_drc.py"
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


def _run(layout, run_dir, report, interconnect_adapter=None):
    cmd = [
        sys.executable, str(RUNNER),
        "--path", str(layout),
        "--interposer-adapter", str(TEST_ADAPTER),
        "--run_dir", str(run_dir),
        "--report", str(report),
    ]
    if interconnect_adapter:
        cmd += ["--interconnect-adapter", interconnect_adapter]
    return subprocess.run(cmd, capture_output=True, text=True)


def _violations(layout, run_dir, interconnect_adapter=None):
    report = Path(run_dir) / "report.lyrdb"
    proc = _run(layout, run_dir, report, interconnect_adapter)
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
    """ihp_cupillar (40 um spacing / 80 um pitch) flags pads only 20 um apart."""
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
