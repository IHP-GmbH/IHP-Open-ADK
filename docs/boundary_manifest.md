# Boundary manifest (`<gds-stem>.boundaries.json`)

The boundary manifest is the sidecar file that carries chiplet mechanical
boundaries for an assembly GDS. It is the single source of truth for the ADK
assembly DRC and the boundary viewer: boundaries live in **no fabrication-layer
namespace**, so the assembly contract stays PDK-agnostic (this is what replaced
the legacy exchange0 190/0 convention).

The normative JSON Schema lives at
`config/schema/boundary_manifest.schema.json`; this doc explains it and records
where the runtime contract is looser than the schema.

## File location

Written next to the GDS it describes, named after its stem:

```
board_complete.gds
board_complete.boundaries.json
```

Both readers auto-discover the sidecar by this rule; an explicit path can be
passed instead (`run_drc.py --manifest`, `boundaries_to_rdb.py <manifest.json>`).

## Producers and consumers

| Role | Component | Notes |
|---|---|---|
| Producer | `chiplet_kicad_plugin/hyp_to_gds.py` | one entry per placed chiplet; adds `assembly_gds_sha256` |
| Producer | `gds_to_kicad/blackbox_chiplet.py` | one entry: the standalone die outline |
| Consumer | `adk/klayout/drc/run_drc.py` | validates schema + version before the deck runs |
| Consumer | `adk/klayout/macros/boundaries_to_rdb.py` | viewer (`.lyrdb` markers); validates schema + version |
| Consumer | `adk/klayout/drc/adk_assembly.drc` | parses blind; relies on the runner's pre-validation |

## Schema

What is actually enforced is narrower than what a producer typically writes, so
"Required" below means *required by the JSON Schema and the consumers*, not
"always present in real output". The schema requires only `schema`, `version`,
and `boundaries` at the top level; the consumers check even less. Both readers
validate `schema` and `version` only (`run_drc.py`, `boundaries_to_rdb.py
load_manifest`); everything else is read with `.get()` and a default.

Top-level object:

| Field | Required | Type | Meaning |
|---|---|---|---|
| `schema` | yes | string | literal `"adk-boundary-manifest"` |
| `version` | yes | string | exact `"1.0.0"` (see version policy) |
| `boundaries` | yes | array | one entry per chiplet; may be empty |
| `generator` | no | string | producing tool, for provenance |
| `assembly_gds` | no | string | filename of the GDS this sidecar describes |
| `assembly_gds_sha256` | no | string | hash of the GDS at write time (staleness hint) |
| `dbu_um` | no | number | database unit in microns (typically `0.001`); reader default `0.001` |
| `top_cell` | no | string | top cell the boundaries are placed in; reader default `"TOP"` |

Producers always emit `generator`, `assembly_gds`, `dbu_um`, and `top_cell`, so
real manifests carry them; they are listed "no" because no consumer fails when
they are absent.

Each `boundaries[]` entry:

| Field | Required | Type | Meaning |
|---|---|---|---|
| `polygon_dbu` | yes | array of `[x, y]` int pairs (>= 3) | boundary contour in DBU; **authoritative** for the DRC |
| `instance` | no | string | placed-instance name (e.g. `U1`); used as the viewer label |
| `source_die` | no | string | die/library the instance came from |
| `class` | no | string | `"chiplet"` (reserved for future classes) |
| `polygon_um` | no | array of `[x, y]` float pairs (>= 3) | same contour in microns; derived from `polygon_dbu * dbu_um` when absent |
| `transform` | no | object | placement provenance: `origin_um`, `rotation_deg`, `mirror_x`, `magnification` |
| `kgd` | no | boolean \| null | declared by the schema, written and read by nobody; see below |
| `bbox_dbu` | no | array of 4 integers | declared by the schema, written and read by nobody; see below |

`polygon_dbu` is the only per-boundary field the schema requires and the only
one the DRC deck reads (`layers_def.drc` builds `chiplet_boundary` from it
unconditionally). `instance`, `source_die`, and `class` are optional even for
the viewer: `boundaries_to_rdb.py` labels each marker `instance` else
`source_die` else `boundary_{index}`, and reads `source_die` / `class` with
`.get()`.

`polygon_dbu` is the authoritative geometry. `polygon_um` and `transform` are
identity/provenance only; consumers must not let them disagree with
`polygon_dbu` in any check.

### No z coordinate: single-tier by design

Boundaries are 2D placement polygons; the schema has no z, tier, or height
field (`transform.origin_um` is exactly `[x, y]`). Every consumer, including
the ASM rules, therefore treats all chiplets as sitting on one attachment
plane. Adding a z/tier field is a version-bump event and must land together
with tier-aware consumers (see `architecture.md`, Known limitations).

### Vestigial schema fields: `kgd`, `bbox_dbu`

The schema declares `kgd` (`boolean | null`) and `bbox_dbu` (array of 4
integers) on each boundary, but no producer writes them and no consumer reads
them. Treat them as reserved/vestigial: they are valid if present and ignored
either way. Drop them from the schema or wire them into a producer and reader
before relying on them.

## Version policy

Readers pin the version with an **exact string match** against `"1.0.0"`; any
other value (including a missing field) is a hard error, never a warning. A
stale manifest silently interpreted under new semantics could pass an assembly
that should fail; the whole point of the sidecar is that the DRC trusts it.

When the schema changes, bump `version` in both producers and both reader
constants in the same change set:

- `chiplet_kicad_plugin/hyp_to_gds.py` (`_write_boundary_manifest`)
- `gds_to_kicad/blackbox_chiplet.py` (`_write_blackbox_manifest`)
- `adk/klayout/drc/run_drc.py` (`SUPPORTED_MANIFEST_VERSION`)
- `adk/klayout/macros/boundaries_to_rdb.py` (`SUPPORTED_MANIFEST_VERSION`)

The two reader constants are intentionally separate copies (the viewer macro
does not import the runner's module tree); `tests/test_boundaries_to_rdb.py`
pins them equal, and the plugin/gds_to_kicad suites pin their writers to the
same string.

## Example

```json
{
  "schema": "adk-boundary-manifest",
  "version": "1.0.0",
  "generator": "hyp_to_gds.py",
  "assembly_gds": "board_complete.gds",
  "dbu_um": 0.001,
  "top_cell": "INTERPOSER",
  "assembly_gds_sha256": "9f2c...",
  "boundaries": [
    {
      "instance": "U1",
      "source_die": "ACME_PHY",
      "class": "chiplet",
      "transform": {
        "origin_um": [1200.0, 800.0],
        "rotation_deg": 0.0,
        "mirror_x": true,
        "magnification": 1.0
      },
      "polygon_dbu": [[1100000, 725000], [1300000, 725000],
                      [1300000, 875000], [1100000, 875000]],
      "polygon_um": [[1100.0, 725.0], [1300.0, 725.0],
                     [1300.0, 875.0], [1100.0, 875.0]]
    }
  ]
}
```
