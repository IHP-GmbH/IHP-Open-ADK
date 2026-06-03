# Interposer adapters

One Ruby file per supported interposer PDK. Each file declares the abstract
inputs the ADK rule deck consumes.

## Required abstract inputs (v0.1.0)

| Name                       | Type                       | Description |
|----------------------------|----------------------------|-------------|
| `chiplet_attachment_input` | KLayout layer expression   | Region geometry representing the chiplet-attachment mechanism on the interposer. Cu pillars on IHP, but adapters are free to model solder bumps, hybrid bonding, etc. |

## Optional abstract inputs

None in v0.1.0. `interposer_outline` (currently `prBoundary` in IHP) is a
candidate for promotion in v0.2 if any rule needs the interposer boundary.

## Optional rule overrides

Adapters MAY override entries of the shared `drc_rules` dict in Ruby:

```ruby
# Per-interposer override example
drc_rules['ASM_b'] = 30.0
```

Overrides take effect for both the KLayout deck and the KiCad DRU generator,
provided the generator is invoked with `--interposer-adapter <name>`.

## Naming

Filename: `<product>.drc`, where `<product>` is the interposer's formal
short name. The basename (without extension) is what gets passed to
`--interposer-adapter`. The MVP adapter is `intm4tm2` (IHP 130-nm
IntM4TM2 aluminum BEOL interposer).
