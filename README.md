# ADK: Assembly Design Kit

Assembly-level Design Kit for heterogeneous chiplet integration. The ADK lives
one abstraction layer above all PDKs: it owns the rules and tooling that govern
*how chiplets are placed and connected* on an interposer. The interposer and the
chiplet dies remain ordinary PDKs.

## Status

`v0.1.0`. The KLayout assembly DRC, its runner, the KiCad DRU generator, and the
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

## Current scope

- KLayout assembly DRC deck, split into per-axis rule decks under
  `klayout/drc/rule_decks/` (placement and interconnect).
- Boundary-manifest model: the DRC obtains chiplet boundaries from a
  `<design>.boundaries.json` sidecar, not from the GDS. See
  `docs/boundary_manifest.md`.
- Interposer and interconnect adapter mechanisms under `pdk_adapters/`, bridging
  abstract ADK inputs to PDK-specific fabrication layers.
- Shared layer, rule-parameter, and interconnect registries in `config/`, with
  JSON Schemas under `config/schema/`.
- KiCad DRU generator. It covers `ASM.b` (courtyard_clearance); `ASM.a` is left
  to KiCad's native courtyard collision check and `ASM.e`/`ASM.f` are
  post-layout only. It is a partial mirror of the KLayout deck, not a full one.
- Stub directories for future ADK domains: `thermal/`, `power/`, `timing/`.

## Layout

```
config/                  Layer, rule-param, interconnect registries + schema/
klayout/                 DRC deck (adk_assembly.drc), runner, rule_decks/, macros/
kicad/                   KiCad DRU generator and templates
pdk_adapters/            interposer/ and interconnect/ adapters per PDK
thermal/ power/ timing/  Reserved for future ADK domains (README stubs only)
tests/                   Regression fixtures and golden references
docs/                    Architecture, layer registry, adapter contract,
                         boundary manifest, integration
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

## License

Apache 2.0; see `LICENSE`.
