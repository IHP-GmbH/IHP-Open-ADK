# ADK architecture

## Role model

```
                ┌──────────────────────────────────┐
                │  ADK (this repo)                 │
                │  assembly rules + tooling        │
                │  interposer-agnostic by design   │
                └────────────┬─────────────────────┘
                             │ uses (via adapter)
        ┌────────────────────┼────────────────────┐
        │                    │                    │
        ▼                    ▼                    ▼
  ┌─────────┐         ┌────────────┐       ┌──────────┐
  │ Chiplet │   ...   │ Interposer │   ... │ Chiplet  │
  │  PDK A  │         │    PDK     │       │  PDK B   │
  └─────────┘         └────────────┘       └──────────┘
```

Each PDK owns its own fabrication rules (metal, vias, pads, …). The ADK
governs only the *interactions* between chiplets and the interposer; never
the internal manufacturing of any single PDK.

The interposer is **just another PDK** from the ADK's point of view. Its
fabrication rules (`Padc_*`, `TM*`, `TV*`, …) live inside the interposer
repo; the ADK never touches them.

## Abstraction boundary

Rules under `klayout/drc/rule_decks/` and `kicad/dru/templates/` reference
**abstract input names only**. The mapping from abstract names to
PDK-specific fabrication layers lives in adapters under `pdk_adapters/`.

Required abstract inputs (v0.2.0):

| Name                       | Axis | Semantics                                            |
|----------------------------|------|------------------------------------------------------|
| `chiplet_attachment_input` | interposer | Region the interposer offers for chiplet attachment  |
| `chiplet_boundary`         | manifest | Mechanical outline of each placed chiplet, injected from the boundary manifest (not a fab layer, not adapter-declared) |
| `interconnect_region`      | interconnect (optional) | Region the interconnect method's pitch/spacing rules check; defaults to `chiplet_attachment_input` |

Note `chiplet_attachment_input` is **semantic, not technology-specific**.
On IHP it is derived from Cu pillars (`passiv_pillar AND dfpad_pillar`).
On a hypothetical TSMC interposer it might be solder-bump regions; on
hybrid bonding it might be metal-pad arrays. The rule never cares which.

## Why no chiplet adapter

The tempting symmetry — "if there is an interposer adapter, there should
be a chiplet adapter" — is rejected for v0.1.0.

The asymmetry is real: the interposer's fabrication geometry IS in the
assembled GDS (the substrate metal stack), so rules need a way to talk
about it abstractly. Chiplet internals are NOT in the assembled GDS;
they are represented only by `chiplet_boundary` (a mechanical outline
carried by the boundary manifest). The four current ASM rules treat all
chiplets uniformly through `chiplet_boundary` and need nothing more.

If a future use case emerges for per-chiplet-type checks (e.g. "chiplet
of type X must expose its declared bump pattern"), the right mechanism
will be designed then — could be chiplet adapters, `.chiplet` YAML
metadata, sidecar files, or something else informed by the actual need.
Do not preemptively scaffold.

## Multi-interposer extension

Future interposers plug in by adding ONE adapter file under
`pdk_adapters/interposer/`. Selection happens at the runner via
`--interposer-adapter <name>` (or, through the orchestrator, via the
`.chiplet` YAML's `interposer.adapter` field). ADK rules are not
modified when a new interposer is added.

## Interconnect axis

A second, orthogonal adapter axis (v0.2.0). Where the interposer axis says
*where* attachment lands, the interconnect axis says *how dense / by what
method*: the bump-to-bump pitch/spacing rules that are a property of the bumping
method (Cu pillar, microbump, …), not of the interposer.

An interconnect adapter under `pdk_adapters/interconnect/` is a parameter pack
(`interconnect_rules`); the `8_2_interconnect.drc` deck applies IXN.b/IXN.e over
the abstract attachment region the interposer adapter exposes. Composing the two
axes gives full modularity: swap the interposer adapter to change the substrate,
swap the interconnect adapter to change the bumping vendor — neither touches the
other, and ADK rules are unchanged either way.

The axis is optional and additive. With no interconnect adapter selected, the
deck, the eval chain, and every output byte are identical to v0.1.0. This keeps
"Why no chiplet adapter" intact: chiplet internals still are not in the GDS; the
interconnect axis governs attachment *method*, not chiplet contents.

The axis refines per method when the exporter supplies
`<gds-stem>.ixn_methods.json` (derived from the `.chiplet`'s per-die
`connection:` ids and the interconnect PDK manifest): each method's numbers
run on the attachment pads under its dies' boundaries, with a conservative
cross-method spacing between methods and the adapter numbers covering pads
outside every method region. One interconnect PDK per assembly; what varies
per die is the method within it. See `adapter_contract.md`,
"Per-method refinement".

## Cross-tool consistency

`config/layers.json`, `config/rule_params.json`, and (v0.2.0)
`config/interconnect.json` are shared between the KLayout deck and the
KiCad DRU generator. Both must read these registries and apply adapter
overrides identically, so the two toolchains agree on what they are
checking.

The chiplet boundary is carried by a per-assembly manifest
(`config/schema/boundary_manifest.schema.json`) that producers
(`hyp_to_gds.py`, `blackbox_chiplet.py`) emit and the DRC runner injects.
It lives outside any fabrication-layer namespace, so the assembly contract
is PDK-agnostic and no `(190, 0)` literal survives in the default path.
