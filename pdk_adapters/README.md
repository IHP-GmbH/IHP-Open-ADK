# PDK adapters

The ADK assembly DRC is written against abstract layer names, never PDK
fabrication layer numbers. Adapters are the one place that mapping is allowed to
live, so the same rule deck runs on any interposer and any bumping method without
edits. Add a PDK by adding an adapter file; the rules stay untouched.

There are two independent adapter axes.

## Structure

- `interposer/`, one file per supported interposer PDK. An interposer adapter is
  **required** for every ADK run. It declares *where* chiplet attachment lands,
  for example `chiplet_attachment_input` built from IHP layers in
  `interposer/intm4tm2.drc`. Selected via the runner's `--interposer-adapter
  <name>` flag, or, through the orchestrator, the `.chiplet` YAML's
  `interposer.adapter` field.

- `interconnect/`, one file per bumping method (`ihp_cupillar`, `ihp_sbump`,
  `vendorx_microbump`). An interconnect adapter is **optional**. It declares
  *how* attachment is checked, the bump-to-bump pitch and spacing (IXN rules).
  Selected via `--interconnect-adapter <name>`, or the `.chiplet`
  `interconnect.adapter` field. Omit it for interposer-only checking, byte
  identical to runs before this axis existed. See `interconnect/README.md`.

## No chiplet adapter

There is intentionally no chiplet adapter. Chiplet internals are not present in
the assembled GDS; each chiplet is represented only by `chiplet_boundary`, a
mechanical outline carried by a per-assembly boundary manifest
(`<gds-stem>.boundaries.json`) that the runner injects into the deck. The current
ASM rules treat every chiplet uniformly through that boundary and need nothing
more. A chiplet-adapter mechanism will be designed only if a real consumer
appears. See `docs/architecture.md` for the rationale and `docs/layer_registry.md`
for the boundary's migration off the legacy `exchange0` fab layer.

## Contract

See `docs/adapter_contract.md` for the formal contract. The KLayout deck
(`adk_assembly.drc`) holds the required-input list
(`required_inputs = ['chiplet_attachment_input']`) and, right after the adapter
runs and before the rule decks, appends a generated check into the single eval
chain. A missing input aborts the run with an explicit error that names it.

The interconnect axis is purely additive, introduced in v0.2.0. Interposer
adapters that predate it run unchanged.
