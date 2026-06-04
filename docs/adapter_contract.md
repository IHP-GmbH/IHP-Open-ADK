# Adapter contract

What an adapter must do to be loadable by the ADK runner. Two adapter axes:
the **interposer** adapter (required) declares *where* chiplet attachment lands;
the **interconnect** adapter (optional) declares the *method* rules (pitch /
spacing). The interconnect axis is fully backward-compatible — with no
interconnect adapter, behaviour is identical to v0.1.0.

## Interposer adapter

### Location

`pdk_adapters/interposer/<basename>.drc`

The runner's `--interposer-adapter <name>` flag takes the *basename*
(without `.drc` extension). `intm4tm2` resolves to
`pdk_adapters/interposer/intm4tm2.drc`.

### Required abstract inputs

Every interposer adapter MUST declare these as KLayout layer
expressions in its top-level scope:

#### `chiplet_attachment_input`

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

### Optional rule-parameter overrides

Adapters MAY override entries in the `drc_rules` dict (loaded from
`config/rule_params.json` before adapter eval):

```ruby
drc_rules['ASM_b'] = 30.0  # tighter chiplet spacing for this interposer
```

Overrides are picked up by both the KLayout deck and the KiCad DRU
generator, provided the generator is invoked with
`--interposer-adapter <name>`.

### What adapters must NOT do

- Declare rules. Rules belong exclusively to the ADK deck.
- Declare or modify the chiplet boundary. It is injected by the ADK from the
  boundary manifest (`config/schema/boundary_manifest.schema.json`), not a
  layer adapters can touch.
- Import other `.drc` files. Adapters are self-contained.

## Interconnect adapter

The optional second axis: the bump-to-bump *method* rules (pitch / spacing).
Selected via `--interconnect-adapter <name>` (runner) or the `.chiplet`
`interconnect.adapter` field (orchestrator). Omitting it disables the axis and
the run is identical to interposer-only.

### Location

`pdk_adapters/interconnect/<basename>.drc`

### Required parameters

An interconnect adapter is a **parameter pack**. It MUST set these keys on the
`interconnect_rules` dict (defaults loaded from `config/interconnect.json`
before the adapter is eval'd):

| Key            | Meaning                                                 |
|----------------|---------------------------------------------------------|
| `IXN_spacing`  | Min edge-to-edge spacing between attachment points (um) |
| `IXN_pitch`    | Min centre-to-centre pitch (um)                         |
| `IXN_pad_size` | Representative pad size (um) for pitch->space convert   |

```ruby
interconnect_rules['IXN_spacing'] = 40.0
interconnect_rules['IXN_pitch']   = 80.0
interconnect_rules['IXN_pad_size'] = 40.0
```

The deck validates these keys are set after the adapter is `eval`-ed; a missing
key aborts the run naming the missing key.

### Optional region narrowing

An interconnect adapter MAY declare `interconnect_region` to narrow the checked
region. If omitted, the deck uses `chiplet_attachment_input` (exposed by the
interposer adapter) — no fab layer is duplicated.

### What interconnect adapters must NOT do

- Redeclare `chiplet_attachment_input` or any fab layer — those belong to the
  interposer axis.
- Declare rules. The IXN rules live in `8_2_interconnect.drc`.
- Import other `.drc` files.

## Evolving the contract

- New required interposer inputs: bump `config/layers.json`'s version and update
  *every* shipped adapter in the same commit. The runner's validator
  ensures stale adapters fail loudly.
- New optional inputs: guard the consuming rule with `if defined?(...)`
  in the deck so adapters without the optional input still run.
- The interconnect axis (v0.2.0) is purely additive: runs without an
  interconnect adapter, and interposer adapters without an interconnect
  counterpart, behave exactly as v0.1.0.
