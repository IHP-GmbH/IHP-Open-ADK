# IHP Open Assembly Design Kit (ADK)

Assembly-level Design Kit for heterogeneous chiplet integration. The ADK lives
one abstraction layer above all PDKs: it owns the rules and tooling that govern
*how chiplets are placed and connected* on an interposer. The interposer and the
chiplet dies remain ordinary PDKs.

## Status

> [!WARNING]
> ADK is currently a preview release only!

`v0.3.0`. The KLayout assembly DRC, its runner, the KiCad DRU generator, and the
IHP interposer adapter are implemented and exercised by the regression suite.

## What it checks

Two independent rule axes, each driven by a PDK adapter:

- **Assembly placement** (`ASM.*`): courtyard clearance and collision between
  chiplet boundaries on the interposer, and die edge to interposer wire-bond
  pad distance when the adapter declares wire-bond pads. Always on; selected
  with `--interposer-adapter`.
- **Interconnect** (`IXN.*`): bump-to-bump pitch and spacing for the
  attachment layer (CuPillar, solder bump, microbump). Optional; selected with
  `--interconnect-adapter`.

Both axes are interposer-agnostic: the deck consumes abstract layer names that
an adapter maps to PDK-specific fabrication layers. Chiplet boundaries are not
read from a fab layer; they come from a boundary manifest sidecar.

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
- KiCad DRU generator. It covers `ASM.b` (courtyard_clearance between die
  footprints); `ASM.a` is left to KiCad's native courtyard collision check
  and `ASM.e`/`ASM.f`/`ASM.g` are post-layout only. It is a partial mirror of the KLayout deck, not a full one.
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
selects one under `pdk_adapters/interconnect/`. Each takes a REGISTRY ID, never
a path: the adapter is evaluated as code, so it is resolved only through the
anchored registry (`--list-adapters` prints what is registered). An
unregistered deck is made reachable by registering an id for it, not by naming
its path; see `docs/adapter_contract.md`. Every adapter MUST declare the
abstract inputs documented there; the deck validates this before evaluating
rules.

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
usage/validation errors. See `checks/README.md`.

## Exporting to OpenROAD 3Dblox

```bash
python openroad/chiplet2dbx.py --chiplet <design.chiplet> --out-dir build/3dblox \
    [--pins U1=chiplets/die_a.pins.json ...] \
    [--views U2=views/u2/eda-views.json --contract U2=contracts/die_b ...] \
    [--corner <name>] [--top-verilog build/top.v]
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

A die given an eda-views record (`--views`, `chiplet-flow-eda-views/v1`) and
the die contract the record must be bound to (`--contract`) is exported with
its recorded views instead: its ChipletDef names the record's technology LEF,
cell LEFs, Liberty files at one corner and DEF in place, relative to the
`.3dbv`, and its bump map lists the DEF pad instances at their centres in the
DEF frame with the die's own ports and nets. Every file used is re-hashed, and
each pad centre, the DEF units and the die area are checked against the DEF
before anything is written; a pad must be placed `N` on a one-pin master, one
pad per port. Such a die declares `anchor: bbox_center`; a
flipped one is placed `MZ_MY` (OpenROAD's `MZ` mirrors z only). A
`--top-verilog` system top is named as the Design's Verilog after its module,
instances and ports are checked against the records. The records are read,
never qualified: the export says nothing about the timing of the assembly.

## Bridging an assembly-lvs resolved v2 document

`bridges/assembly_lvs/resolved_v2_entry.py` builds the ADK view of an assembly
described by an assembly-lvs resolved v2 document: assembled GDS (carrier
geometry preserved, ADK die layouts instanced), boundary manifest, IXN and
pillar sidecars, `.chiplet` and an entry manifest with the frame constants and
the exhaustive terminal correspondence. The sidecar it needs is derived from an
accepted `.chiplet` by `bridges/assembly_lvs/derive_attachment_sidecar.py`.

```
python bridges/assembly_lvs/derive_attachment_sidecar.py --chiplet <design.chiplet> \
    --pillars <assembly.pillars.json> --document <resolved.json> --dies-root <dies dir> \
    --out <resolved stem>.adk_attachment.json
python bridges/assembly_lvs/resolved_v2_entry.py --sidecar <resolved stem>.adk_attachment.json \
    --out build/entry
```

The entry runs neither DRC nor LVS; its manifest lists the `run_drc.py` and
`pads_vs_pillars.py` commands to run next. See `docs/resolved_v2_entry.md` for
the frames, the accepted subset of v2 and the corpus-binding controls.

## License

Apache 2.0; see `LICENSE`.
