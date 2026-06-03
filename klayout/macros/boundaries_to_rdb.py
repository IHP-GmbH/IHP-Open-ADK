#!/usr/bin/env python3
"""Render the chiplet boundary manifest as a KLayout report database (.lyrdb).

This is the *viewer* half of the boundary contract. The ADK assembly DRC
(``adk/klayout/drc/run_drc.py``) is the *checker*; both read the same
``<gds-stem>.boundaries.json`` sidecar, so what you see here is exactly what the
DRC injects -- single source of truth -- and nothing is written into the GDS.

Why a ``.lyrdb`` and not a GDS annotation layer: the report database is the same
mechanism KLayout already uses to show DRC violations, so loading it is a
gesture users already know (Tools > Marker Browser). It overlays markers on the
layout without touching any fabrication-layer namespace -- the whole reason the
boundary left exchange0/190 in the first place.

Two entry points share ``build_rdb``:

* **CLI** -- ``python boundaries_to_rdb.py <assembly.gds|manifest.json> [-o out.lyrdb]``
  writes the ``.lyrdb`` next to the input (testable, headless, CI-friendly).
* **GUI** -- the ``show_boundaries.lym`` macro builds the same database in memory
  and shows it in the current layout view (one click, no file to load).

Coordinates: markers use ``polygon_um`` (microns). A KLayout RDB value carries
micron user units, so ``polygon_dbu`` would render 1000x too large. ``polygon_um``
is optional in the schema, so it is derived from ``polygon_dbu * dbu_um`` when the
producer omitted it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import klayout.db as _kdb
    import klayout.rdb as _krdb
except ImportError:  # older KLayout binary exposes the API only as `pya`
    import pya as _kdb
    import pya as _krdb

# Only used when the manifest omits dbu_um (1 nm; the IHP/typical database unit).
DEFAULT_DBU_UM = 0.001
CATEGORY = "chiplet_boundary"


def find_sidecar(layout_path) -> Path:
    """Mirror ``run_drc.resolve_manifest_path``'s auto-discovery rule:
    ``<layout-stem>.boundaries.json`` next to the GDS. Kept independent so the
    KLayout macro does not import the runner's heavyweight module tree."""
    layout = Path(layout_path)
    return layout.with_name(layout.stem + ".boundaries.json")


def load_manifest(path) -> dict:
    data = json.loads(Path(path).read_text())
    if data.get("schema") != "adk-boundary-manifest":
        raise ValueError(
            f"{path}: not an ADK boundary manifest "
            f"(schema={data.get('schema')!r})")
    return data


def _polygon_um(boundary: dict, dbu_um: float) -> List[Tuple[float, float]]:
    """Boundary contour in microns. Prefer ``polygon_um``; derive it from
    ``polygon_dbu`` when the producer omitted it (it is optional)."""
    if boundary.get("polygon_um"):
        return [(float(x), float(y)) for x, y in boundary["polygon_um"]]
    return [(x * dbu_um, y * dbu_um) for x, y in boundary["polygon_dbu"]]


def _label(boundary: dict, index: int) -> str:
    return (boundary.get("instance")
            or boundary.get("source_die")
            or f"boundary_{index}")


def _safe(name: str) -> str:
    # '.' and '/' are RDB category-path separators; keep instance names atomic.
    return name.replace(".", "_").replace("/", "_")


def build_rdb(manifest: dict) -> "_krdb.ReportDatabase":
    """Build an in-memory report database: one category ``chiplet_boundary``,
    one sub-category per placed chiplet (named by instance), one polygon marker
    each. The caller saves it (``.save``) or shows it in a view (``add_rdb``)."""
    r = _krdb.ReportDatabase("adk-chiplet-boundaries")
    top_cell = manifest.get("top_cell") or "TOP"
    cell = r.create_cell(top_cell)
    root = r.create_category(CATEGORY)
    root.description = ("ADK chiplet mechanical boundaries "
                       "(viewer-only; read by no DRC rule)")
    dbu_um = float(manifest.get("dbu_um") or DEFAULT_DBU_UM)

    for i, b in enumerate(manifest.get("boundaries", [])):
        pts = _polygon_um(b, dbu_um)
        if len(pts) < 3:
            continue
        label = _label(b, i)
        sub = r.create_category(root, _safe(label))
        src = b.get("source_die")
        cls = b.get("class")
        desc = " / ".join(x for x in (src, cls) if x)
        if desc:
            sub.description = desc
        item = r.create_item(cell.rdb_id(), sub.rdb_id())
        item.add_value(_krdb.RdbItemValue(
            _kdb.DPolygon([_kdb.DPoint(x, y) for x, y in pts])))
        # Readable tag so the value list also names the chiplet.
        item.add_value(_krdb.RdbItemValue(label + (f" ({src})" if src else "")))
    return r


def write_lyrdb(manifest: dict, out_path) -> Path:
    out = Path(out_path)
    build_rdb(manifest).save(str(out))
    return out


def _resolve_input(arg: str) -> Tuple[dict, Path]:
    """Accept a manifest ``.json`` directly or a ``.gds`` whose sidecar is
    auto-discovered. Returns ``(manifest, default_output_path)``."""
    p = Path(arg)
    if p.suffix.lower() == ".json":
        manifest_path = p
    else:
        manifest_path = find_sidecar(p)
        if not manifest_path.is_file():
            raise FileNotFoundError(
                f"No boundary sidecar for {p}: expected {manifest_path}. "
                f"Pass the manifest .json directly, or generate the GDS with "
                f"hyp_to_gds / blackbox_chiplet (which emit the sidecar).")
    return load_manifest(manifest_path), manifest_path.with_suffix(".lyrdb")


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Render an ADK boundary manifest as a KLayout .lyrdb "
                    "marker database for eyeball inspection.")
    ap.add_argument("input",
                    help="assembly .gds (sidecar auto-discovered) or the "
                         "<gds-stem>.boundaries.json manifest itself")
    ap.add_argument("-o", "--output",
                    help="output .lyrdb (default: <manifest-stem>.lyrdb)")
    args = ap.parse_args(argv)
    try:
        manifest, default_out = _resolve_input(args.input)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    out = Path(args.output) if args.output else default_out
    write_lyrdb(manifest, out)
    n = len(manifest.get("boundaries", []))
    print(f"Wrote {out} ({n} boundar{'y' if n == 1 else 'ies'}).")
    print(f"In KLayout: open the GDS, then Tools > Marker Browser and load "
          f"'{out.name}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
