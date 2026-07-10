# ADK: Assembly Design Kit

Assembly-level Design Kit for heterogeneous chiplet integration. The ADK lives
one abstraction layer above all PDKs: it owns the rules and tooling that govern
*how chiplets are placed and connected* on an interposer. The interposer and the
chiplet dies remain ordinary PDKs.

## Status

`v0.2.0`. The KLayout assembly DRC, its runner, the KiCad DRU generator, and the
IHP interposer adapter are implemented and exercised by the regression suite.
The migration out of the interposer-coupled implementation is still settling, so
expect the layer registry and rule set to keep moving. See `docs/architecture.md`
for the role model and `CHANGELOG.md` for what has landed.

## What it checks

Two independent rule axes, each driven by a PDK adapter:

- **Assembly placement** (`ASM.*`): courtyard clearance and collision between
  chiplet boundaries on the interposer. Always on; selected with
  `--interposer-adapter`.
- **Interconnect** (`IXN.*`): bump-to-bump pitch and spacing for the
  attachment layer (CuPillar, solder bump, microbump). Optional; selected with
  `--interconnect-adapter`.

Both axes are interposer-agnostic: the deck consumes abstract layer names that
an adapter maps to PDK-specific fabrication layers. Chiplet boundaries are not
read from a fab layer; they come from a boundary manifest sidecar (see below).

On top of the DRC deck, a manifest-level check (`checks/pads_vs_pillars.py`)
verifies that each die's pad positions line up with the interposer's as-drawn
Cu-pillar/bump positions, read from the `<gds-stem>.pillars.json` sidecar
(see `docs/pillar_manifest.md`).

## Current scope

- KLayout assembly DRC deck, split into per-axis rule decks under
  `klayout/drc/rule_decks/` (placement and interconnect).
- Boundary-manifest model: the DRC obtains chiplet boundaries from a
  `<design>.boundaries.json` sidecar, not from the GDS. See
  `docs/boundary_manifest.md`.
- Interposer and interconnect adapter mechanisms under `pdk_adapters/`, bridging
  abstract ADK inputs to PDK-specific fabrication layers.
- OpenROAD 3Dblox exporter (`openroad/chiplet2dbx.py`) that derives a
  geometric `.3dbv`/`.3dbx` view from a finalized `.chiplet` assembly so
  OpenROAD's `check_3dblox` can lint the assembly geometry.
- Shared layer, rule-parameter, and interconnect registries in `config/`, with
  JSON Schemas under `config/schema/`.
- KiCad DRU generator. It covers `ASM.b` (courtyard_clearance); `ASM.a` is left
  to KiCad's native courtyard collision check and `ASM.e`/`ASM.f` are
  post-layout only. It is a partial mirror of the KLayout deck, not a full one.
- Stub directories for future ADK domains: `thermal/`, `power/`, `timing/`.

## Layout

```
checks/                  Manifest-level checks (pads_vs_pillars)
config/                  Layer, rule-param, interconnect registries + schema/
klayout/                 DRC deck (adk_assembly.drc), runner, rule_decks/, macros/
kicad/                   KiCad DRU generator and templates
openroad/                chiplet2dbx exporter (.chiplet -> 3Dblox)
pdk_adapters/            interposer/ and interconnect/ adapters per PDK
vendor/                  Vendored chiplet_format_io reference reader
thermal/ power/ timing/  Reserved for future ADK domains (README stubs only)
tests/                   Regression fixtures and golden references
docs/                    Architecture, layer registry, adapter contract,
                         boundary manifest, pillar manifest, integration
```

## Running the assembly DRC

By default the runner requires a `<design>.boundaries.json` manifest next to the
GDS; it is auto-discovered and validated before the deck runs. The manifest is
emitted by the producer (`hyp_to_gds` / `blackbox_chiplet`).

```bash
python klayout/drc/run_drc.py \
    --path <design.gds> \
    --interposer-adapter intm4tm2 \
    --run_dir /tmp/adk_drc
```

Add the interconnect axis with an interconnect adapter:

```bash
python klayout/drc/run_drc.py \
    --path <design.gds> \
    --interposer-adapter intm4tm2 \
    --interconnect-adapter ihp_cupillar \
    --run_dir /tmp/adk_drc
```

Other flags: `--manifest` to point at a non-default sidecar,
`--interconnect-methods` to scope IXN checks per attachment method, `--topcell`
(auto-detected if omitted), `--threads`, `--run_mode` (`tiling`/`deep`/`flat`),
and `--report`. Pass `--legacy-exchange0` to check a pre-migration GDS that
carries boundaries on the exchange0 fab layer instead of a manifest.

The `--interposer-adapter` flag selects which adapter under
`pdk_adapters/interposer/` satisfies the contract; `--interconnect-adapter`
selects one under `pdk_adapters/interconnect/`. Each accepts a shortname or an
explicit `.drc` path. Every adapter MUST declare the abstract inputs documented
in `docs/adapter_contract.md`; the deck validates this before evaluating rules.

## Checking pad-to-pillar alignment

```bash
python checks/pads_vs_pillars.py --chiplet <design.chiplet> \
    --pillars <assembly>.pillars.json \
    [--pins U1=chiplets/die_a.pins.json ...] [--gds-pads U2 ...] \
    [--tolerance-um 1.0] [--json report.json] [--strict]
```

Transforms each die's pad centers through its `.chiplet` placement
(position, rotation, flip-chip mirror) and matches them against the as-drawn
pillar centers in the pillar manifest, by pin name where available and by
nearest-unique fallback otherwise. Exit codes: 0 clean, 1 findings, 2
usage/validation errors. See `checks/README.md` and
`docs/pillar_manifest.md`.

## Exporting to OpenROAD 3Dblox

```bash
python openroad/chiplet2dbx.py --chiplet <design.chiplet> --out-dir build/3dblox \
    [--pins U1=chiplets/die_a.pins.json ...]
```

Produces `<name>.3dbv` (black-box ChipletDefs), `<name>.3dbx` (the placed
assembly) and one minimal technology LEF per `.chiplet` technology. In
OpenROAD, `read_3dbx <name>.3dbx` loads the assembly and runs the
`check_3dblox` geometry linter. With per-die `--pins` lists (the
`*.pins.json` artifact gds_to_kicad extracts from a die's GDS or footprint)
the affected dies also get a `.bmap` bump map plus a per-method bump macro
LEF rendered by the interconnect PDK's manifest-driven generator
(`INTERCONNECT_PDK_ROOT` or sibling checkout), which activates the linter's
bump-alignment check. The export stays lossy by declaration (no artwork,
no parasitics); the mapping conventions follow the chiplet-spec
interoperability appendix, and the `.chiplet` file stays the authoritative
placement source.

## License

Apache 2.0; see `LICENSE`.
