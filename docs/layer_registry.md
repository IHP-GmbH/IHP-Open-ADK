# Layer registry

`config/layers.json` is the authoritative source for every layer number
the ADK or any consuming tool needs.

## exchange0 (190 / 0)

Mechanical outline of one chiplet die, drawn face-down on the
interposer. One polygon per placed chiplet.

The outline is the *die mechanical footprint* — not a bond-pad region
and not an attachment-area region. Bond-pad and attachment regions are
handled by `chiplet_attachment_input` (an abstract input declared by
the interposer adapter). Spacing, overlap, and area tolerances are
encoded in rule **parameter values** (`ASM_b`, `ASM_e`, …); never by
stamping a larger geometry.

## exchange1 (191 / 0)

Reserved slot for future per-chiplet metadata. Not consumed by any
v0.1.0 rule.

## Consumers

- `klayout/drc/rule_decks/layers_def.drc` — defines
  `exchange0_drw = polygons(190, 0)` and `exchange1_drw = polygons(191, 0)`.
- `klayout/lyp/adk_layers.lyp` — KLayout layer-properties fragment.
- `chiplet_kicad_plugin/hyp_to_gds.py` — reads
  `layers.json["exchange0"]["gds_layer"]` instead of the historical
  hardcoded `190`.

## Adding a new layer

1. Append an entry to `layers.json` (lowercase snake_case name).
2. Document its semantics here.
3. Add the corresponding `polygons(layer, datatype)` line to
   `klayout/drc/rule_decks/layers_def.drc`.
4. Add the equivalent entry to `klayout/lyp/adk_layers.lyp`.
