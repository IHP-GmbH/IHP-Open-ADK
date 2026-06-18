# Interconnect adapters

The second, optional adapter axis. Where an interposer adapter
(`../interposer/`) says *where* chiplet attachment lands, an interconnect
adapter says *how*; it carries the bump-to-bump method rules (pitch/spacing).
Swap the adapter to retighten or relax those rules without touching the
interposer geometry: same layout, different verdict.

## Location & selection

An adapter is a `.drc` file under `pdk_adapters/interconnect/`; its id is the
file basename (e.g. `ihp_cupillar`). Select it at the runner with
`--interconnect-adapter <id>` (a shortname resolved against
`pdk_adapters/interconnect/<id>.drc`, or an absolute path), or through the
orchestrator via the `.chiplet` YAML's `interconnect.adapter` field.

Omitting it is fully backward-compatible: no interconnect rules run and the
assembly DRC behaves exactly as without this axis. The axis also activates on
a per-method file alone (`--interconnect-methods`, below), even with no
adapter.

## Contract

An interconnect adapter is a **parameter pack**. It MUST set these keys on the
`interconnect_rules` dict (defaults loaded from `config/interconnect.json`
before the adapter is eval'd):

| Key            | Meaning                                                        |
|----------------|----------------------------------------------------------------|
| `IXN_spacing`  | Min edge-to-edge spacing between attachment points (um)        |
| `IXN_pitch`    | Min centre-to-centre pitch (um)                                |
| `IXN_pad_size` | Representative pad size (um); used for the pitch-to-space convert |

The deck has no native pitch check, so it tests pitch as a spacing of
`pitch - pad_size`.

An adapter MAY declare `interconnect_region` to narrow the checked region; if
omitted, the deck uses `chiplet_attachment_input` (exposed by the interposer
adapter). An adapter MUST NOT redeclare `chiplet_attachment_input` or any fab
layer; those belong to the interposer axis.

## Per-method refinement (optional)

When an assembly mixes interconnect methods (each die's `connection:` selects
its method), pass `--interconnect-methods <gds-stem>.ixn_methods.json` to scope
the checks per method instead of assembly-globally. The exporter derives that
file from the `.chiplet`'s per-die connections plus the interconnect PDK
manifest. Each method then runs its own numbers on the attachment pads under
its dies' boundaries (`IXN.b.<method>` / `IXN.e.<method>`), plus a conservative
cross-method spacing between different methods' pads (`IXN.x.<m1>.<m2>`, the
larger of the two spacings). Pads outside every method region keep the
assembly-global adapter numbers (`IXN.b.unclaimed` / `IXN.e.unclaimed`) when an
adapter is also loaded. This mode requires the boundary manifest. See
`../../docs/adapter_contract.md` for the full per-method spec.

## Shipped adapters

- `ihp_cupillar`: IHP/PacTech Cu pillar (Table 6.1 Option 1 defaults; 35 um
  opening, 40 um space, 75 um pitch).
- `ihp_sbump`: IHP/PacTech SAC305 solder bump.
- `vendorx_microbump`: fictional non-IHP fine-pitch microbump demo.

See `../../docs/adapter_contract.md` and `../../docs/architecture.md`.
