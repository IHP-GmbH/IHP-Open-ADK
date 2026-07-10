# Interposer adapters

The ADK assembly deck checks chiplet placement against the interposer, but it
must not know any one PDK's layer numbers. An interposer adapter bridges that
gap: one self-contained `.drc` file per supported interposer PDK that declares
the ADK's abstract inputs in terms of that PDK's real layers. Swap interposers
by passing a different adapter, with no change to the deck or the rules.

The authoritative, normative spec is `docs/adapter_contract.md`. This README is
the quick reference for adapter authors; when the two disagree, the contract
wins.

## Required abstract input

Every adapter MUST declare this as a KLayout layer expression in its top-level
scope:

| Name                       | Type                     | Description |
|----------------------------|--------------------------|-------------|
| `chiplet_attachment_input` | KLayout layer expression | Region where chiplet attachment can land on the interposer. The physical mechanism is unspecified: Cu pillars (IHP), solder bumps, hybrid-bonding pad arrays; any of them is fine as long as the region models where attachment happens. |

The KLayout deck (not the runner) validates this after the adapter is `eval`-ed. If it is undefined or
nil, the run aborts with an error naming the missing input (see
`adk_assembly.drc`, `required_inputs`).

The shipped `intm4tm2` adapter models the Cu pillar as the overlap of the
passivation opening and the final Cu pad:

```ruby
passiv_pillar = polygons(9, 35)
dfpad_pillar  = polygons(41, 35)
chiplet_attachment_input = passiv_pillar.and(dfpad_pillar)
```

## Optional rule overrides

An adapter MAY override entries of the shared `drc_rules` dict, loaded from
`config/rule_params.json` before the adapter is eval'd:

```ruby
# Tighter chiplet spacing for this interposer (default ASM_b is 50.0)
drc_rules['ASM_b'] = 30.0
```

Overrides apply to both the KLayout deck and the KiCad DRU generator, provided
the generator is invoked with `--interposer-adapter <name>`.

## What adapters must NOT do

- Declare rules. Rules belong exclusively to the ADK deck.
- Declare or modify the chiplet boundary. It is injected by the ADK from the
  per-assembly boundary manifest (`config/schema/boundary_manifest.schema.json`),
  not a layer adapters can touch. See `docs/layer_registry.md`.
- Import other `.drc` files. Adapters are self-contained.

## Naming and selection

Filename: `<basename>.drc`, where `<basename>` is the interposer's short name.
That basename is what gets passed to `--interposer-adapter`; the runner resolves
it against this directory, so `--interposer-adapter intm4tm2` loads
`pdk_adapters/interposer/intm4tm2.drc`. An absolute path to a `.drc` is also
accepted.

The shipped adapter is `intm4tm2` (IHP 130-nm IntM4TM2 aluminum BEOL
interposer).

## The interconnect axis (v0.2.0)

This directory covers the interposer axis only: *where* attachment lands. The
optional interconnect adapter axis (`pdk_adapters/interconnect/`) layers the
bump-to-bump *method* rules (pitch / spacing) on top. It is purely additive:
with no interconnect adapter, behaviour is identical to the interposer-only
path. See `docs/adapter_contract.md` for that axis.
