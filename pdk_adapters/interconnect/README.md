# Interconnect adapters

The second, optional adapter axis. Where an interposer adapter
(`../interposer/`) says *where* chiplet attachment lands, an interconnect
adapter says *how* — the bump-to-bump method rules (pitch/spacing).

## Location & selection

`pdk_adapters/interconnect/<basename>.drc`. Selected at the runner via
`--interconnect-adapter <name>` (or, through the orchestrator, the
`.chiplet` YAML's `interconnect.adapter` field). Omitting it is fully
backward-compatible: no interconnect rules run and the assembly DRC behaves
exactly as without this axis.

## Contract

An interconnect adapter is a **parameter pack**. It MUST set these keys on the
`interconnect_rules` dict (defaults loaded from `config/interconnect.json`
before the adapter is eval'd):

| Key            | Meaning                                                     |
|----------------|-------------------------------------------------------------|
| `IXN_spacing`  | Min edge-to-edge spacing between attachment points (um)     |
| `IXN_pitch`    | Min centre-to-centre pitch (um)                             |
| `IXN_pad_size` | Representative pad size (um) for the pitch->spacing convert |

An adapter MAY declare `interconnect_region` to narrow the checked region; if
omitted, the deck uses `chiplet_attachment_input` (exposed by the interposer
adapter). An adapter MUST NOT redeclare `chiplet_attachment_input` or any fab
layer — those belong to the interposer axis.

## Shipped adapters

- `ihp_cupillar` — IHP/PacTech Cu pillar (Table 6.1 Option 2 defaults).
- `ihp_sbump` — IHP/PacTech SAC305 solder bump.
- `vendorx_microbump` — fictional non-IHP fine-pitch microbump demo.

See `../../docs/adapter_contract.md` and `../../docs/architecture.md`.
