# Boundary manifest (`<gds-stem>.boundaries.json`)

The boundary manifest is the sidecar file that carries chiplet mechanical
boundaries for an assembly GDS. It is the single source of truth for the ADK
assembly DRC and the boundary viewer: boundaries live in **no fabrication-layer
namespace**, so the assembly contract stays PDK-agnostic (this is what replaced
the legacy exchange0 190/0 convention).

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

Top-level object:

| Field | Required | Type | Meaning |
|---|---|---|---|
| `schema` | yes | string | literal `"adk-boundary-manifest"` |
| `version` | yes | string | exact `"1.0.0"` (see version policy) |
| `generator` | yes | string | producing tool, for provenance |
| `assembly_gds` | yes | string | filename of the GDS this sidecar describes |
| `dbu_um` | yes | number | database unit in microns (typically `0.001`) |
| `top_cell` | yes | string | top cell the boundaries are placed in |
| `boundaries` | yes | array | one entry per chiplet; may be empty |
| `assembly_gds_sha256` | no | string | hash of the GDS at write time (staleness hint) |

Each `boundaries[]` entry:

| Field | Required | Type | Meaning |
|---|---|---|---|
| `instance` | yes | string | placed-instance name (e.g. `U1`) |
| `source_die` | yes | string | die/library the instance came from |
| `class` | yes | string | `"chiplet"` (reserved for future classes) |
| `polygon_dbu` | yes | array of `[x, y]` int pairs | boundary contour in DBU -- **authoritative** for the DRC |
| `polygon_um` | no | array of `[x, y]` float pairs | same contour in microns; derived from `polygon_dbu * dbu_um` when absent |
| `transform` | no | object | placement provenance: `origin_um`, `rotation_deg`, `mirror_x`, `magnification` |

`polygon_dbu` is the authoritative geometry. `polygon_um` and `transform` are
identity/provenance only; consumers must not let them disagree with
`polygon_dbu` in any check.

## Version policy

Readers pin the version with an **exact string match** against `"1.0.0"`; any
other value (including a missing field) is a hard error, never a warning. A
stale manifest silently interpreted under new semantics could pass an assembly
that should fail -- the whole point of the sidecar is that the DRC trusts it.

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
