# Adapter contract

A PDK or interconnect vendor ships an adapter so the ADK can check an
assembly without baking that vendor's layer numbers into the deck. There are
two axes:

- **Interposer** adapter (required): declares *where* chiplet attachment lands
  on the interposer.
- **Interconnect** adapter (optional): supplies the *method* rules (pitch and
  spacing) for the bump-to-bump checks.

The interconnect axis is purely additive. With no interconnect adapter loaded,
the eval chain and output are byte-identical to the interposer-only deck (see
the eval-chain comment in `klayout/drc/adk_assembly.drc`).

## Interposer adapter

### Location

`pdk_adapters/interposer/<basename>.drc`

The runner's `--interposer-adapter` flag accepts either form:

- a **shortname** resolved against `pdk_adapters/interposer/`, so `intm4tm2`
  resolves to `pdk_adapters/interposer/intm4tm2.drc`; or
- an **explicit path** to a `.drc` file (absolute or relative), used as-is.

The same resolution applies in the KiCad DRU generator
(`kicad/dru/generate_assembly_dru.py`).

### Required abstract inputs

Every interposer adapter MUST declare these as KLayout layer expressions in its
top-level scope.

#### `chiplet_attachment_input`

Region representing the area on the interposer through which chiplet attachment
happens. The physical mechanism is unspecified: Cu pillars (IHP), solder bumps
(TSMC), hybrid-bonding pad arrays, all acceptable as long as the resulting
region accurately models *where attachment can land*.

Example (IHP):

```ruby
passiv_pillar = polygons(9, 35)
dfpad_pillar  = polygons(41, 35)
chiplet_attachment_input = passiv_pillar.and(dfpad_pillar)
```

The KLayout deck (not the runner) validates this. After the adapter is
`eval`-ed, the deck raises unless every required input is `defined?` and
non-nil, aborting the run with an error that names the missing input. The
runner only resolves the adapter path and shells out to KLayout.

### Optional rule-parameter overrides

Adapters MAY override entries in the `drc_rules` dict, loaded from
`config/rule_params.json` before the adapter is `eval`-ed:

```ruby
drc_rules['ASM_b'] = 30.0  # tighter chiplet spacing for this interposer
```

Both consumers pick up the override: the KLayout deck reads it from the shared
`drc_rules`, and the KiCad DRU generator parses literal numeric
`drc_rules[...]` assignments from the adapter, but only when invoked with
`--interposer-adapter <name>`.

### What adapters must NOT do

- Declare rules. Rules belong exclusively to the ADK deck.
- Declare or modify the chiplet boundary. It is injected by the ADK from the
  per-assembly boundary manifest (`config/schema/boundary_manifest.schema.json`),
  built in `layers_def.drc` before the adapter runs, not a layer adapters can
  touch.
- Import other `.drc` files. Adapters are self-contained.

## Interconnect adapter

The optional second axis: the bump-to-bump *method* rules (pitch and spacing).
Selected via `--interconnect-adapter <name>` (runner) or the `.chiplet`
`interconnect.adapter` field (orchestrator). Omitting it disables the axis, and
the run is identical to interposer-only.

### Location

`pdk_adapters/interconnect/<basename>.drc`

Like the interposer flag, `--interconnect-adapter` accepts a shortname
(resolved against `pdk_adapters/interconnect/`) or an explicit path to a `.drc`
file.

### Required parameters

An interconnect adapter is a **parameter pack**. It MUST set these keys on the
`interconnect_rules` dict (defaults loaded from `config/interconnect.json`
before the adapter is `eval`-ed):

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

The deck validates these keys after the adapter is `eval`-ed; a missing key
aborts the run naming the missing key.

### Optional region narrowing

An interconnect adapter MAY declare `interconnect_region` to narrow the checked
region. If omitted, the deck falls back to `chiplet_attachment_input` (exposed
by the interposer adapter), so no fab layer is duplicated.

### What interconnect adapters must NOT do

- Redeclare `chiplet_attachment_input` or any fab layer; those belong to the
  interposer axis.
- Declare rules. The IXN rules live in `rule_decks/8_2_interconnect.drc`.
- Import other `.drc` files.

### Per-method refinement (optional)

Assemblies can mix interconnect *methods* of the active PDK (each die's
`connection:` selects its method). `--interconnect-methods
<gds-stem>.ixn_methods.json` scopes the IXN checks per method: the exporter
derives the file from the `.chiplet`'s per-die connections plus the
interconnect PDK manifest (the single source of truth for the numbers), and the
deck checks each method's pitch/spacing on the attachment pads under that
method's die boundaries (`IXN.b.<method>` / `IXN.e.<method>`). Pads of different
methods near each other get a conservative cross-method spacing
(`IXN.x.<m1>.<m2>`, the larger of the two spacings); pads outside every method
region keep the assembly-global adapter numbers (`IXN.b.unclaimed` /
`IXN.e.unclaimed`), and only when an adapter is loaded.

Each method entry MUST carry `IXN_spacing`, `IXN_pitch`, `IXN_pad_size`, and
`dies` (the boundary-manifest instance names using it); the boundary manifest is
required in this mode. Without the file, the axis stays assembly-global, so
adapter-only runs are unchanged.

## Evolving the contract

- **New required interposer input.** The required list is hardcoded in the
  deck: `required_inputs = ['chiplet_attachment_input']` in
  `klayout/drc/adk_assembly.drc`. Add the new input there and update *every*
  shipped adapter in the same commit. The deck's post-`eval` validator then
  makes stale adapters fail loudly. (Bumping `config/layers.json`'s version has
  no effect: that file is legacy-compat, and only its `layers` key is read, by
  the `--legacy-exchange0` path.)
- **New optional input.** Guard the consuming rule with `if defined?(...)` in
  the deck so adapters without the optional input still run. This is the same
  pattern `8_2_interconnect.drc` uses for `interconnect_region` and
  `layers_def.drc` uses for the boundary manifest.
- **Versioning.** The interconnect axis is additive: runs without an
  interconnect adapter, and interposer adapters with no interconnect
  counterpart, behave exactly as the pre-interconnect deck. See `CHANGELOG.md`
  for the per-release contract history. The `VERSION` file and the `config/*.json`
  `version` keys all track the latest released `CHANGELOG.md` entry (currently
  `0.2.0`); `CHANGELOG.md` remains the authoritative record.
