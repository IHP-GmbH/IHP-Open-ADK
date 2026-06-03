# KLayout assets

Assembly DRC deck and standalone runner.

## Files (populated in subsequent commits)

- `drc/adk_assembly.drc` — top-level deck. JSON loader + eval-with-shared-locals
  pattern modelled on `interposer/.../intm4tm2.drc`.
- `drc/rule_decks/layers_def.drc` — abstract layer definitions (only
  `exchange0_drw`, `exchange1_drw`). Everything else comes from the interposer
  adapter.
- `drc/rule_decks/8_1_assembly.drc` — the four ASM rules (a, b, e, f),
  expressed in terms of abstract names.
- `drc/run_drc.py` — standalone runner. `--interposer-adapter` is REQUIRED
  and is validated before deck evaluation; missing required inputs abort
  the run with an explicit error.
- `lyp/adk_layers.lyp` — LYP fragment for `exchange0` / `exchange1`. Loaded
  alongside the interposer LYP when both are needed.

## Macros (boundary viewer)

The boundary contract is a `<gds-stem>.boundaries.json` sidecar (see
`config/schema/boundary_manifest.schema.json`), deliberately outside any
fab-layer namespace. These render it back for eyeball inspection — **viewer
only**: nothing is written into the GDS and no DRC rule reads them. They consume
the *same* sidecar `run_drc.py` does, so what you see is exactly what the checker
injects (single source of truth).

- `macros/boundaries_to_rdb.py` — core + CLI. Builds a KLayout report database
  (`.lyrdb`) with one marker per placed chiplet, grouped under a
  `chiplet_boundary` category with a sub-category per instance. Importable from
  both the standalone klayout python module and the in-KLayout `pya`.
- `macros/show_boundaries.lym` — thin GUI macro over the same `build_rdb`.

### CLI

    python klayout/macros/boundaries_to_rdb.py <assembly.gds | manifest.json> [-o out.lyrdb]

Auto-discovers `<gds-stem>.boundaries.json` next to a GDS (or takes the manifest
directly) and writes the `.lyrdb`; load it via *Tools > Marker Browser*.

### GUI

Make the macro discoverable, then open an assembly GDS and run
*Tools > Show chiplet boundaries (ADK boundary manifest)* — one click loads the
markers into the Marker Browser:

    export KLAYOUT_PATH=$KLAYOUT_PATH:/path/to/adk/klayout   # or export ADK_ROOT=/path/to/adk
    # or: ln -s /path/to/adk/klayout/macros/show_boundaries.lym ~/.klayout/macros/

Markers carry micron coordinates (a `.lyrdb` value is in micron user units), so
they align on the layout regardless of its DBU.
