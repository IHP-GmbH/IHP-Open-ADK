# Integration

How the rest of the ecosystem consumes the ADK: the HYP-to-GDS converter,
the KiCad plugin orchestrator, downstream DRU generators, and the shared
discovery convention that lets every tool find its dependencies the same way.

## `hyp_to_gds.py` (`chiplet_kicad_plugin/`)

The HYP-to-GDS converter is the producer of the assembly contract. Alongside
the assembly GDS it writes a `<gds>.boundaries.json` manifest: one
mechanical-boundary polygon per placed chiplet, each carrying its identity
(`instance`, `source_die`, `transform`). The boundary lives only in the
manifest; it is never stamped on a GDS layer, so it cannot alias a PDK
fabrication layer. The assembly DRC runner auto-discovers this sidecar.

By default no annotation is added and chiplets stay generic in the production
GDS. Pass the opt-in `--annotate-boundaries` to paint the boundaries (with
instance labels) onto a viewer-only layer (default `1000/0`, override with
`--boundary-viz-layer`, outside IHP's fab range) for eyeball inspection. No
DRC rule reads that layer; the manifest remains the sole assembly contract.

## `orchestrator.py` (`chiplet_kicad_plugin/pipeline/`)

The orchestrator turns a loaded `.chiplet` design into ADK runner flags. It
reads two independent adapter axes from the YAML and forwards each only when
present:

- **Interposer.** `interposer.adapter` (default `intm4tm2` for backward
  compatibility) becomes `--interposer-adapter <value>`. Always emitted.
- **Interconnect.** `interconnect.adapter` (default `""`, meaning no
  interconnect axis) becomes `--interconnect-adapter <value>`, emitted only when
  an adapter is declared, so a legacy design never silently gains the
  assembly-global IXN pitch/spacing checks. The per-method file is emitted
  independently as `--interconnect-methods <path>` whenever the exporter derives
  a per-method sidecar; either flag activates the IXN axis (the deck appends
  `8_2_interconnect.drc` when an interconnect adapter or a methods file is
  present). This axis is configured by `config/interconnect.json` (schema v0.2.0).

## Downstream DRU generators

A per-project DRU generator can reuse the ADK's assembly-rule renderer instead
of duplicating the rule logic. There is no installed `adk` package (no
`__init__.py`), so import the bare module after putting its directory on
`sys.path`, the same way the ADK's own test does:

```python
import sys
sys.path.insert(0, "/path/to/adk/kicad/dru")
from generate_assembly_dru import render_assembly_rules

section = render_assembly_rules(rules, adapter_name="intm4tm2")
```

`render_assembly_rules` returns a string ready to append to a `.kicad_dru`.
The rendered block opens with the banner
`# ADK assembly DRC rules (auto-generated)`. The CLI exposes the same renderer
with `--interposer-adapter <name>` (and an optional `--interconnect-adapter`).

The signature is append-only:

```python
render_assembly_rules(rules, adapter_name=None, template_dir=None,
                      interconnect_rules=None, interconnect_adapter_name=None)
```

New options arrive as trailing kwargs with defaults, so existing importers
stay source-compatible across ADK releases. With no interconnect adapter the
output is byte-identical to before that axis existed.

For provenance, importers are encouraged to emit comments carrying the sha256
of every input JSON and of the active adapter `.drc`.

## Standalone DRC

```bash
python adk/klayout/drc/run_drc.py \
    --path <assembled.gds> \
    --interposer-adapter intm4tm2 \
    --run_dir /tmp/adk_drc
```

The runner produces a KLayout `.lyrdb` results file under `--run_dir`,
identical in structure to the interposer-PDK runner's output.

It needs the chiplet boundaries: by default it auto-discovers the
`<gds-stem>.boundaries.json` sidecar next to `--path` (the same manifest the
`hyp_to_gds.py` section describes, overridable with `--manifest`). If that
sidecar is absent the runner aborts. To check a pre-migration GDS that carries
boundaries on the historical `exchange0` fab layer, pass `--legacy-exchange0`
instead. The optional interconnect axis adds `--interconnect-adapter
<name>` (IXN bump pitch/spacing rules) and `--interconnect-methods <path>`.

## Ecosystem discovery convention

Cross-repo lookups never use fixed-depth path arithmetic (`parents[N]`,
`../..`). Every consumer resolves its dependency with the same chain, first
hit wins:

1. **Environment variable** naming the dependency root.
2. **KiCad project text variable** of the same name, where a board is in scope
   (plugin contexts only).
3. **Upward walk** from the consumer's own file looking for a conventional
   sibling directory. Each root accepts more than one candidate name: the
   canonical ecosystem name and the upstream GitHub repo name a default clone
   produces (for example `adk` or `ADK`, `gds_to_kicad` or `gds-to-kicad`,
   `interposer` or `OpenIntM4TM2`).
4. **Loud failure.** If the dependency is required for correctness the tool
   aborts with the probed locations and the variable to set. A silent degraded
   mode is acceptable only when the output stays correct without the
   dependency (for example the blackbox fallback layer table).

| Variable | Dependency root | Consumers |
|---|---|---|
| `ADK_ROOT` | `adk/` | plugin DRC step, gds_to_kicad canonical layers |
| `INTERPOSER_PDK_ROOT` | interposer PDK (`libs.tech/klayout/python/`) | hyp_to_gds Cu-pillar generation |
| `INTERCONNECT_PDK_ROOT` | `interconnect_pdk/` | manifest readers, chiplet-studio fragments, bump3d |
| `GDS_TO_KICAD_ROOT` | `gds_to_kicad/` | bump_mirror PinList import |
| `PDK_ROOT` | base IHP SG13G2 PDK (`ihp-sg13g2/libs.tech/klayout/`) | hyp_to_gds via_stack PCells, chiplet-studio |
| `KICAD_CHIPLET_PYTHON` | worker interpreter (not a root) | plugin orchestrator |

Precedent for the hard-fail rule: a complete GDS emitted without its requested
Cu pillars looks fabricable, and no downstream DRC can flag the absent
geometry, so hyp_to_gds aborts when pillars are requested and the interposer
PDK is unreachable.

### `${VAR}` references in path inputs

Path inputs may reference the discovery variables above with `${VAR}` syntax:
in KiCad board text variables (`INTERPOSER_LYP`), footprint fields
(`GDS_FILE`, `LYP_FILE`), hyp_to_gds CLI arguments, and the `.chiplet` entries
those flow into (`technologies.*.layer_properties`, `components[].layout`):

```yaml
technologies:
  intm4tm2:
    layer_properties: ${INTERPOSER_PDK_ROOT}/libs.tech/klayout/tech/intm4tm2.lyp
components:
  - id: U1
    layout: ${GDS_TO_KICAD_ROOT}/gds_files/dies/my_die.gds
```

Writers copy these values **verbatim**; the KiCad C++ exporter and the Python
plugin writer stay byte-identical with zero emission logic. Readers
(hyp_to_gds, chiplet-studio) expand each `${VAR}` on read with the discovery
chain: environment, then sibling-checkout walk, then loud failure naming the
variable. A set-but-invalid environment value falls through to the walk.

Plain absolute and relative paths pass through untouched. Absolute paths
remain the default emission, since a `.chiplet` is machine-local unless the
board data opts into `${VAR}` forms; consumers of a foreign `.chiplet` adjust
paths themselves. Relative paths resolve against the `.chiplet` file's
directory.
