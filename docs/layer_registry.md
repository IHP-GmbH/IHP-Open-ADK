# Layer registry

The chiplet boundary is **ADK assembly metadata carried by a per-assembly
manifest** (`config/schema/boundary_manifest.schema.json`), outside any PDK
layer namespace. `config/layers.json` only records the **legacy** fabrication
location of the boundary, consulted by the assembly DRC's `--legacy-exchange0`
compat path for pre-migration GDS.

For the normative manifest spec, see [`docs/boundary_manifest.md`](boundary_manifest.md).

## Why the boundary left the fab namespace

`exchange0` was bound to GDS `190/0`, which is IHP SG13G2's real `Exchange0`
layer. Pinning the ADK's cross-PDK assembly contract to an IHP fab number was
an abstraction leak (the ADK sits above all PDKs) and risked colliding with a
chiplet's own geometry on that layer. The boundary now lives in the manifest,
so it is PDK-agnostic by construction and cannot alias any process or chiplet
geometry.

## Boundary manifest (default)

Producers (`chiplet_kicad_plugin/hyp_to_gds.py`,
`gds_to_kicad/blackbox_chiplet.py`) write a `<gds>.boundaries.json` sidecar:
one polygon per placed chiplet, with per-chiplet identity (instance,
source-die, transform) and the contour in DBU and microns. The assembly DRC
runner auto-discovers the sidecar next to the GDS when `--manifest` is omitted
and injects the polygons into the deck as the `chiplet_boundary` layer.
Schema: `config/schema/boundary_manifest.schema.json`.

## exchange0 (190 / 0): legacy compat only

Historical fabrication-layer location of the boundary. Read by
`klayout/drc/rule_decks/layers_def.drc` only when the deck runs with
`legacy_exchange0` set (runner flag `--legacy-exchange0`), to check a
pre-migration GDS that still carries boundaries on `190/0`.

## Consumers

- `klayout/drc/adk_assembly.drc`: parses the manifest and injects it.
- `klayout/drc/rule_decks/layers_def.drc`: builds `chiplet_boundary` from the
  injected manifest (default), or from
  `polygons(exchange0.gds_layer, exchange0.gds_datatype)` read from
  `config/layers.json` (190/0) in legacy mode.
- `klayout/macros/boundaries_to_rdb.py`: the viewer half of the contract;
  renders the same manifest as a `.lyrdb` for the Marker Browser.
- `chiplet_kicad_plugin/hyp_to_gds.py`, `gds_to_kicad/blackbox_chiplet.py`:
  emit the manifest.
