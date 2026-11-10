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

## Optional abstract input

An adapter MAY declare wire-bond pads, which the deck checks against `ASM.g`
(die edge to interposer wire-bond pad):

| Name                  | Type                                   | Description |
|-----------------------|----------------------------------------|-------------|
| `wire_bond_pad_input` | lambda taking a layout source, returning a KLayout layer expression | The interposer's wire-bond pads, derived from the source it is given. |

It is a lambda, not a layer, because the die layouts in an assembly GDS may
draw their own bond pads on the same layers. The deck calls it once, on a source
with every die cell the boundary manifest names pruned from the hierarchy, so a
die's pads are never read and a carrier pad drawn under a die still is. Inside
the lambda, read layers from the source argument (`src.polygons(...)`), never
with a bare `polygons(...)`, which reads the full layout. An adapter that does
not declare it runs `ASM.g` as not applicable, and the report says so.

The shipped `intm4tm2` adapter uses the PDK's own pad derivation:

```ruby
wire_bond_pad_input = lambda do |src|
  src.polygons(9, 0).and(src.polygons(41, 0)).and(src.polygons(134, 0))
end
```

## Optional rule overrides

An adapter MAY override entries of the shared `drc_rules` dict, loaded from
`config/rule_params.json` before the adapter is eval'd:

```ruby
# This interposer's own chiplet spacing rule (the default ASM_b is 200.0)
drc_rules['ASM_b'] = 250.0
```

An override is the technology's own rule, not a relaxation of the default.
Write it as one literal assignment per line, as above, at most once per name,
above any other code in the adapter (only blank lines, comments and other
overrides before it): a decimal number whose integer part has no leading
zero (Ruby reads `0250` as octal), optionally followed by a comment that
names neither table. The deck evals any Ruby, but `adk_rule_params.py`, which
the DRU generator and other tools read overrides with, cannot know the value
the deck would apply for anything else. So it refuses any line naming
`drc_rules` or `interconnect_rules` other than such an override of the table
being read: `2.5e2`, `1_000`, a trailing `;`, `store`, `merge!` and a
computed value are all refused, and so is the other adapter kind's table.
Comment lines naming a table are refused too, anywhere in the file: below
code a line that looks like a comment may sit inside a string and run as
code, and one rule for the whole file is simpler to follow. Write a
commented-out override without the table name. It also refuses a repeated
name, a line that continues an override (led by `.`, `&.`, `&&`, `||`, `and`
or `or`) and an override below other code: the parser does not read Ruby,
and code above an override can make it text, a branch, or part of a
multiple assignment that stores nil.
Overrides apply to both the KLayout deck and the KiCad DRU generator, provided
the generator is invoked with `--interposer-adapter <name>`. Placement and
packaging tools that read `config/rule_params.json` directly do not see an
override, and may refuse an adapter that overrides a rule they place by.

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
