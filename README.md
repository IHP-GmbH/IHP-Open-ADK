# ADK — Assembly Design Kit

Assembly-level Design Kit for heterogeneous chiplet integration. The ADK lives
one abstraction layer above all PDKs: it owns the rules and tooling that
govern *how chiplets are placed* on an interposer. The interposer and the
chiplet dies remain ordinary PDKs.

## Status

`v0.1.0` — scaffolding. Migration from the interposer-coupled implementation
is in progress. See `docs/architecture.md` for the role model and `CHANGELOG.md`
for what has landed.

## Scope (v0.1)

- KLayout assembly DRC deck, interposer-agnostic (consumes abstract layer
  names declared by an interposer adapter).
- Shared layer and rule-parameter registries in `config/`.
- KiCad DRU generator that mirrors the KLayout deck.
- Interposer adapter mechanism (`pdk_adapters/interposer/`) that bridges
  abstract ADK inputs to PDK-specific fabrication layers.
- OpenROAD 3Dblox exporter (`openroad/chiplet2dbx.py`) that derives a
  geometric `.3dbv`/`.3dbx` view from a finalized `.chiplet` assembly so
  OpenROAD's `check_3dblox` can lint the assembly geometry.
- Stub directories for future ADK domains: `thermal/`, `power/`, `timing/`.

## Layout

```
config/                  Shared layer + rule-parameter registries
klayout/                 KLayout assembly DRC deck, runner, layer fragment
kicad/                   KiCad DRU generator and templates
openroad/                chiplet2dbx exporter (.chiplet -> 3Dblox)
pdk_adapters/            Per-PDK adapters that satisfy the ADK contract
vendor/                  Vendored chiplet_format_io reference reader
thermal/ power/ timing/  Reserved for future ADK domains
tests/                   Regression fixtures (interposer-agnostic)
docs/                    Architecture, layer registry, adapter contract
```

## Running the assembly DRC

```bash
python klayout/drc/run_drc.py \
    --path <design.gds> \
    --interposer-adapter intm4tm2 \
    --run_dir /tmp/adk_drc
```

The `--interposer-adapter` flag selects which adapter under
`pdk_adapters/interposer/` satisfies the contract. Every adapter MUST declare
the set of abstract inputs documented in `docs/adapter_contract.md`.

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

Apache 2.0 — see `LICENSE`.
