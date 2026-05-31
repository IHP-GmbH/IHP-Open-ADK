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
- Stub directories for future ADK domains: `thermal/`, `power/`, `timing/`.

## Layout

```
config/                  Shared layer + rule-parameter registries
klayout/                 KLayout assembly DRC deck, runner, layer fragment
kicad/                   KiCad DRU generator and templates
pdk_adapters/            Per-PDK adapters that satisfy the ADK contract
thermal/ power/ timing/  Reserved for future ADK domains
tests/                   Regression fixtures (interposer-agnostic)
docs/                    Architecture, layer registry, adapter contract
```

## Running the assembly DRC

```bash
python klayout/drc/run_drc.py \
    --path <design.gds> \
    --interposer-adapter ihp_sg13g2_interposer \
    --run_dir /tmp/adk_drc
```

The `--interposer-adapter` flag selects which adapter under
`pdk_adapters/interposer/` satisfies the contract. Every adapter MUST declare
the set of abstract inputs documented in `docs/adapter_contract.md`.

## License

Apache 2.0 — see `LICENSE`.
