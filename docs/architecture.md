# ADK architecture

## Role model

```
                +----------------------------------+
                |  ADK (this repo)                 |
                |  assembly rules + tooling        |
                |  interposer-agnostic by design   |
                +---------------+------------------+
                                | uses (via adapter)
        +-----------------------+-----------------------+
        |                       |                       |
        v                       v                       v
  +-----------+         +--------------+         +-----------+
  |  Chiplet  |   ...   |  Interposer  |   ...   |  Chiplet  |
  |   PDK A   |         |     PDK      |         |   PDK B   |
  +-----------+         +--------------+         +-----------+
```

Each PDK owns its own fabrication rules (metal, vias, pads). The ADK governs
only the *interactions* between chiplets and the interposer, never the internal
manufacturing of any single PDK.

The interposer is **just another PDK** from the ADK's point of view. Its
fabrication rules (`Padc_*`, `TM*`, `TV*`) live inside the interposer repo; the
ADK never touches them.

A note on versions: the adapter contract, the `config/*.json` registries, the
package `VERSION` file, and `README.md` all read **0.2.0** (the interconnect axis
landed there). The boundary-manifest schema version (`1.0.0`) is a separate,
independently pinned axis. See `CHANGELOG.md` for what has landed.

## Abstraction boundary

Rules under `klayout/drc/rule_decks/` and `kicad/dru/templates/` reference
**abstract input names only**. The mapping from abstract names to PDK-specific
fabrication layers lives in adapters under `pdk_adapters/`.

Abstract inputs the rule decks reference:

| Name                       | Status | Source | Semantics |
|----------------------------|--------|--------|-----------|
| `chiplet_attachment_input` | required (runner-validated) | interposer adapter | Region the interposer offers for chiplet attachment. `adk_assembly.drc` raises if the adapter does not declare it (`required_inputs = ['chiplet_attachment_input']`). |
| `chiplet_boundary`         | injected | boundary manifest | Mechanical outline of each placed chiplet, built from the manifest's `polygon_dbu`. Not a fab layer, not adapter-declared. |
| `interconnect_region`      | optional | interconnect adapter | Region the interconnect method's pitch/spacing rules check. Defaults to `chiplet_attachment_input` when not narrowed by an adapter. |

`chiplet_attachment_input` is **semantic, not technology-specific**. On the IHP
IntM4TM2 interposer it is derived from Cu pillars (`passiv_pillar AND
dfpad_pillar`, layers 9/35 AND 41/35; see `pdk_adapters/interposer/intm4tm2.drc`).
On a hypothetical TSMC interposer it might be solder-bump regions; on hybrid
bonding it might be metal-pad arrays. The rule never cares which.

## Why no chiplet adapter

The tempting symmetry, "if there is an interposer adapter, there should be a
chiplet adapter", is rejected.

The asymmetry is real: the interposer's fabrication geometry IS in the assembled
GDS (the substrate metal stack), so rules need a way to talk about it abstractly.
Chiplet internals are NOT in the assembled GDS; they are represented only by
`chiplet_boundary`, a mechanical outline carried by the boundary manifest. The
four current ASM rules treat all chiplets uniformly through `chiplet_boundary`
and need nothing more: ASM.a (no overlap), ASM.b (min spacing), ASM.e (min area),
ASM.f (attachment geometry overlapping a chiplet must sit fully inside it).

If a future use case emerges for per-chiplet-type checks (e.g. "chiplet of type X
must expose its declared bump pattern"), the right mechanism will be designed
then; it could be chiplet adapters, `.chiplet` YAML metadata, sidecar files, or
something else informed by the actual need. Do not preemptively scaffold.

## Multi-interposer extension

Future interposers plug in by adding ONE adapter file under
`pdk_adapters/interposer/`. Selection happens at the runner via
`--interposer-adapter <name>` (resolved against that directory, or an absolute
path; or, through the orchestrator, via the `.chiplet` YAML's
`interposer.adapter` field). ADK rules are not modified when a new interposer is
added.

## Interconnect axis

A second, orthogonal adapter axis. Where the interposer axis says *where*
attachment lands, the interconnect axis says *how dense, by what method*: the
bump-to-bump pitch/spacing rules that are a property of the bumping method (Cu
pillar, microbump), not of the interposer.

An interconnect adapter under `pdk_adapters/interconnect/` is a parameter pack
(it sets `interconnect_rules['IXN_*']`); the `8_2_interconnect.drc` deck applies
IXN.b and IXN.e over the abstract attachment region the interposer adapter
exposes (`ixn_region`, which defaults to `chiplet_attachment_input`). Composing
the two axes gives full modularity: swap the interposer adapter to change the
substrate, swap the interconnect adapter to change the bumping vendor; neither
touches the other, and ADK rules are unchanged either way.

The axis is optional and additive. With no interconnect adapter selected, the
deck, the eval chain, and every output byte are identical to the interposer-only
path (the chain is `[layers_def, adapter, validation, 8_1_assembly]`;
`8_2_interconnect.drc` is appended only when an interconnect adapter or a
per-method file is loaded). This keeps "Why no chiplet adapter" intact: chiplet
internals still are not in the GDS; the interconnect axis governs attachment
*method*, not chiplet contents.

The axis refines per method when the exporter supplies
`<gds-stem>.ixn_methods.json` (derived from the `.chiplet`'s per-die
`connection:` ids and the interconnect PDK manifest). Each method's numbers run
on the attachment pads under its dies' boundaries (`IXN.b.<method>` /
`IXN.e.<method>`), with a conservative cross-method spacing between methods
(`IXN.x.<m1>.<m2>`, the max of the two spacings), and the adapter numbers
covering pads outside every method region (`IXN.b.unclaimed` / `IXN.e.unclaimed`).
One interconnect PDK per assembly; what varies per die is the method within it.
See `adapter_contract.md`, "Per-method refinement".

## Cross-tool consistency

`config/rule_params.json` and `config/interconnect.json` are shared between the
KLayout deck and the KiCad DRU generator: both read these registries and apply
adapter overrides identically, so the two toolchains agree on what they are
checking. `config/layers.json` is **legacy-compat only**; the deck always loads
it but only consults its `exchange0` entry on the `--legacy-exchange0` path for
pre-migration GDS, and the KiCad generator does not read it at all.

The chiplet boundary is carried by a per-assembly manifest
(`config/schema/boundary_manifest.schema.json`) that producers (`hyp_to_gds.py`,
`blackbox_chiplet.py`) emit and the DRC runner injects (`-rd manifest=...`). It
lives outside any fabrication-layer namespace, so the assembly contract is
PDK-agnostic and no `(190, 0)` literal survives in the default path.

A related but distinct registry is `config/chiplet_pads.json`: the canonical
chiplet-internal pad vocabulary (`pad_drawing` 205/0, `pad_text` 205/25,
`outline` 206/0) the black-box generator emits. It sits on the producer side, not
the assembly check, but reinforces the "Why no chiplet adapter" reasoning: a
black-box chiplet's outline is recorded in the boundary manifest, not mirrored
onto a fab layer.

## Known limitations

- **ASM rules assume a single tier.** `chiplet_boundary` is a flat 2D region:
  the boundary manifest records placement polygons with no z coordinate, and
  `layers_def.drc` merges every polygon into one region with no per-die
  identity. ASM.a therefore flags any XY overlap between chiplet boundaries,
  including the (currently unsupported) case of dies legally stacked at
  different z in a multi-tier assembly. The assumption is baked into the
  manifest data model, not just the rule; lifting it would need a z-aware
  manifest schema (version bump), per-die identity in `layers_def.drc`, and a
  tier-aware ASM.a. The KiCad-side mirror (native courtyard collision) is
  equally 2D. Not a practical constraint today: nothing in the toolchain
  produces multi-tier assemblies.
- **Die z consistency is checked at export, not in the deck.** The KLayout
  deck sees only 2D geometry plus the manifest. The rule "per-die
  `position.z` == mounting surface + connection-stack height" is enforced by
  the 3Dblox exporter (`openroad/chiplet2dbx.py`, exact within 1e-6 um),
  which refuses to export inconsistent assemblies; downstream, OpenROAD's
  `check_3dblox` re-verifies gap/thickness equality, floating dies, and 3D
  overlap on the exported model. Assemblies that are never exported get no
  automated z check.
