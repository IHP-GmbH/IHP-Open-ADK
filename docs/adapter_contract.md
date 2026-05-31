# Adapter contract

What an interposer adapter must do to be loadable by the ADK runner.

## Location

`pdk_adapters/interposer/<basename>.drc`

The runner's `--interposer-adapter <name>` flag takes the *basename*
(without `.drc` extension). `ihp_sg13g2_interposer` resolves to
`pdk_adapters/interposer/ihp_sg13g2_interposer.drc`.

## Required abstract inputs

Every interposer adapter MUST declare these as KLayout layer
expressions in its top-level scope:

### `chiplet_attachment_input`

Region representing the area on the interposer through which chiplet
attachment happens. The exact physical mechanism is unspecified: Cu
pillars (IHP), solder bumps (TSMC), hybrid-bonding pad arrays — all
acceptable as long as the resulting region accurately models *where
attachment can land*.

Example (IHP):

```ruby
passiv_pillar = polygons(9, 35)
dfpad_pillar  = polygons(41, 35)
chiplet_attachment_input = passiv_pillar.and(dfpad_pillar)
```

The runner validates this is defined as a non-nil layer expression
after the adapter is `eval`-ed. Failing this check aborts the run with
an explicit error that names the missing input.

## Optional rule-parameter overrides

Adapters MAY override entries in the `drc_rules` dict (loaded from
`config/rule_params.json` before adapter eval):

```ruby
drc_rules['ASM_b'] = 30.0  # tighter chiplet spacing for this interposer
```

Overrides are picked up by both the KLayout deck and the KiCad DRU
generator, provided the generator is invoked with
`--interposer-adapter <name>`.

## What adapters must NOT do

- Declare rules. Rules belong exclusively to the ADK deck.
- Modify `exchange0_drw` / `exchange1_drw`. These are ADK-owned.
- Import other `.drc` files. Adapters are self-contained.

## Evolving the contract

- New required inputs: bump `config/layers.json`'s version and update
  *every* shipped adapter in the same commit. The runner's validator
  ensures stale adapters fail loudly.
- New optional inputs: guard the consuming rule with `if defined?(...)`
  in the deck so adapters without the optional input still run.
