# Pillar manifest (`<gds-stem>.pillars.json`)

The pillar manifest is the sidecar file that records where the
Cu-pillar/bump producer actually drew each pillar in an assembly GDS. It is
the alignment ground truth for manifest-level checks: a checker can compare
die pad positions against the **as-drawn** pillar centers -- including any
shifts applied by the collision auto-resolver -- without re-deriving bump
placement from the GDS artwork.

The normative JSON Schema lives at
`config/schema/pillar_manifest.schema.json`; this doc explains it and
records the runtime contract.

## File location

Written next to the assembly GDS it describes, named after its stem, in the
same producer pass that writes `<stem>.boundaries.json`:

```
board_complete.gds
board_complete.boundaries.json
board_complete.pillars.json
```

The manifest is written whenever the Cu-pillar/bump generation path runs
(per-die connection methods resolved, `die_connections` non-empty). A
bump-path run that ends with zero bumps still writes the manifest with an
empty `pillars` array -- "checked, nothing drawn" is distinct from "the bump
path never ran", which writes nothing.

## Producers and consumers

| Role | Component | Notes |
|---|---|---|
| Producer | `chiplet_kicad_plugin/hyp_to_gds.py` | one entry per drawn pillar, post auto-resolve |
| Consumer | `adk/checks/pads_vs_pillars.py` | validates schema + version, checks pad/pillar alignment |

## Schema

Top-level object:

| Field | Required | Type | Meaning |
|---|---|---|---|
| `schema` | yes | string | literal `"adk-pillar-manifest"` |
| `version` | yes | string | exact `"1.0.0"` (see version policy) |
| `units` | yes | string | literal `"um"`; `x_um`/`y_um` are a coordinate contract |
| `pillars` | yes | array | one entry per drawn pillar; may be empty |
| `generator` | no | string | producing tool name, for provenance (the producer writes `"hyp_to_gds.py"`) |
| `assembly_gds` | no | string | basename of the assembly GDS this sidecar describes |

Each `pillars[]` entry:

| Field | Required | Type | Meaning |
|---|---|---|---|
| `device_ref` | yes | string (non-empty) | KiCad reference of the die the pillar belongs to (e.g. `U1`) |
| `pin_name` | yes | string | pad/pin name; `""` when unknown |
| `method` | yes | string | interconnect method id (e.g. `cupillar_opt1`) |
| `x_um`, `y_um` | yes | number | pillar **center** in the canonical interposer GDS-bbox-corner frame — the same frame `.chiplet` die positions and io_pads use, so the two sidecars compare directly (the producer rebases its raw drawing coordinates by the interposer top-cell bbox lower-left corner); y-up, micrometers, **post** collision auto-resolve (the as-drawn position up to that constant frame translation) |
| `diameter_um` | yes | number > 0 | bump body diameter for the method |
| `moved_by_auto_resolve` | no | boolean | `true` when auto-resolve shifted this bump; the key is **omitted** when unknown, never written as a guess |
| `auto_resolve_shift_um` | no | number ≥ 0 | distance the bump was shifted by auto-resolve (frame-invariant); present only on shifted bumps. Lets a checker bound the excused deviation to shift + tolerance instead of accepting any distance on the boolean alone |

## Authority

`x_um`/`y_um` are authoritative for manifest-level checks (they are what the
producer drew, after auto-resolve); the assembly GDS remains the fabrication
ground truth. A consumer must never re-derive pillar positions from pin
lists or `.chiplet` placement and prefer that derivation over the manifest.

## Version policy

Readers pin the version with an **exact string match** against `"1.0.0"`;
any other value (including a missing field) is a hard error, never a
warning -- the same policy as the boundary manifest
(`docs/boundary_manifest.md`). A stale manifest silently interpreted under
new semantics could pass an assembly whose pillars no longer sit where the
checker thinks they do.

When the schema changes, bump `version` in the producer and the reader in
the same change set:

- `chiplet_kicad_plugin/hyp_to_gds.py` (pillar-manifest writer)
- `adk/checks/pads_vs_pillars.py` (`SUPPORTED_PILLAR_MANIFEST_VERSION`)

## Example

```json
{
  "schema": "adk-pillar-manifest",
  "version": "1.0.0",
  "generator": "hyp_to_gds.py",
  "assembly_gds": "board_complete.gds",
  "units": "um",
  "pillars": [
    {
      "device_ref": "U1",
      "pin_name": "VDD",
      "method": "cupillar_opt1",
      "x_um": 1210.5,
      "y_um": 806.25,
      "diameter_um": 75.0,
      "moved_by_auto_resolve": true
    },
    {
      "device_ref": "U1",
      "pin_name": "",
      "method": "cupillar_opt1",
      "x_um": 1290.5,
      "y_um": 806.25,
      "diameter_um": 75.0
    }
  ]
}
```
