# KLayout assets

The assembly DRC deck, its standalone runner, and a boundary viewer.

The chiplet boundary the DRC checks lives in the per-assembly boundary manifest
(`<gds-stem>.boundaries.json`, see `config/schema/boundary_manifest.schema.json`),
outside any fabrication-layer namespace. So it can never alias a PDK layer or a
chiplet's internal geometry. The historical exchange0 fab layer (190/0) is
legacy-compat only, reached via `--legacy-exchange0` for GDS produced before the
manifest migration; see `config/layers.json` (marked LEGACY).

## DRC deck

- `drc/adk_assembly.drc`: top-level wrapper. Interposer-agnostic. It loads the
  shared registries (`config/layers.json`, `config/rule_params.json`, and the
  optional `config/interconnect.json`), reads the boundary manifest passed as
  `-rd manifest=<path>` (required unless `legacy_exchange0` is set), and selects
  a required interposer adapter (`-rd adapter=<path>`) plus an optional
  interconnect adapter and per-method interconnect file. It validates that the
  adapter declares every required abstract input, then concatenates the rule
  decks and runs them in a single `eval` so all locals are shared. See
  `docs/adapter_contract.md`.
- `drc/rule_decks/layers_def.drc`: builds the single abstract layer
  `chiplet_boundary`. By default it is populated from the injected boundary
  manifest; in `legacy_exchange0` mode it reads the exchange0 fab layer
  (`config/layers.json`) instead.
- `drc/rule_decks/8_1_assembly.drc`: the four ASM placement rules (`ASM.a`,
  `ASM.b`, `ASM.e`, `ASM.f`), expressed over the abstract inputs
  `chiplet_boundary` and `chiplet_attachment_input`.
- `drc/rule_decks/8_2_interconnect.drc`: the optional interconnect axis,
  appended to the eval chain only when an interconnect adapter and/or a
  per-method file is loaded. Assembly-global mode (adapter only) emits `IXN.b`
  and `IXN.e` over the whole attachment region; per-method mode (an
  `ixn_methods` file) scopes each method to the pads under its dies' boundaries,
  emitting `IXN.b.<method>` / `IXN.e.<method>`, a cross-method spacing check
  `IXN.x.<m1>.<m2>`, and `IXN.b.unclaimed` / `IXN.e.unclaimed` for pads outside
  every method region.

## Runner

`drc/run_drc.py` drives the deck through `klayout -b`, resolving adapters,
auto-discovering the boundary manifest, and reporting pass/fail. Required inputs
are validated fail-loud before the deck runs, so an assembly with no boundary
source never passes vacuously.

    python klayout/drc/run_drc.py --path design.gds --interposer-adapter intm4tm2

Flags:

- `--path` (required): input GDS/OAS file.
- `--interposer-adapter` (required): shortname resolved against
  `pdk_adapters/interposer/<name>.drc`, or an absolute path to a `.drc`.
- `--interconnect-adapter`: optional shortname (`pdk_adapters/interconnect/<name>.drc`)
  or absolute path; adds the bump-to-bump pitch/spacing axis (IXN rules).
- `--interconnect-methods`: optional `<gds-stem>.ixn_methods.json`; scopes the
  IXN checks per method. Requires the boundary manifest.
- `--manifest`: boundary manifest sidecar; auto-discovered as
  `<gds-stem>.boundaries.json` next to `--path` if omitted. Its schema
  (`adk-boundary-manifest`) and version (`1.0.0`) are validated before the deck
  runs.
- `--legacy-exchange0`: compat mode; read boundaries from the exchange0 fab
  layer in the GDS instead of a manifest.
- `--topcell`: top cell name (auto-detected if omitted).
- `--run_dir`: output directory (default: timestamped subdir in cwd).
- `--report`: explicit report path (overrides default naming).
- `--threads`: threads per KLayout invocation (default: 4).
- `--run_mode`: `tiling` (default), `deep`, or `flat`.

## Macros (boundary viewer)

These render the boundary manifest back for eyeball inspection. Viewer only:
nothing is written into the GDS and no DRC rule reads them. They consume the
*same* `<gds-stem>.boundaries.json` sidecar `run_drc.py` does, so what you see is
exactly what the checker injects (single source of truth).

- `macros/boundaries_to_rdb.py`: core plus CLI. Builds a KLayout report database
  (`.lyrdb`) with one marker per placed chiplet, grouped under a
  `chiplet_boundary` category with a sub-category per instance. Importable from
  both the standalone `klayout` python module and the in-KLayout `pya`.
- `macros/show_boundaries.lym`: thin GUI macro over the same `build_rdb`.

### CLI

    python klayout/macros/boundaries_to_rdb.py <assembly.gds | manifest.json> [-o out.lyrdb]

Auto-discovers `<gds-stem>.boundaries.json` next to a GDS (or takes the manifest
directly) and writes the `.lyrdb`; load it via *Tools > Marker Browser*.

### GUI

Make the macro discoverable, then open an assembly GDS and run
*Tools > Show chiplet boundaries (ADK boundary manifest)*; one click loads the
markers into the Marker Browser:

    export KLAYOUT_PATH=$KLAYOUT_PATH:/path/to/adk/klayout   # or export ADK_ROOT=/path/to/adk
    # or: ln -s /path/to/adk/klayout/macros/show_boundaries.lym ~/.klayout/macros/

Markers carry micron coordinates (a `.lyrdb` value is in micron user units), so
they align on the layout regardless of its DBU.
