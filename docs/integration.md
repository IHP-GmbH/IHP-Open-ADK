# Integration

How other tools in the ecosystem consume the ADK.

## `hyp_to_gds.py` (`chiplet_kicad_plugin/`)

The HYP-to-GDS converter writes a `<gds>.boundaries.json` manifest beside
the assembly GDS: one mechanical-boundary polygon per placed chiplet, with
per-chiplet identity (instance, source-die, transform). The boundary is ADK
assembly metadata and is NOT stamped on any GDS layer, so it cannot alias a
PDK fabrication layer. The assembly DRC runner auto-discovers the manifest.

By default no annotation is added: chiplets remain generic in the production
GDS. The converter's opt-in `--annotate-boundaries` can paint the boundaries
(with instance labels) onto a viewer-only layer (default 1000/0, outside IHP's
fab range) for eyeball inspection, but no DRC rule reads it -- the manifest
remains the sole assembly contract.

## `orchestrator.py` (`chiplet_kicad_plugin/pipeline/`)

The orchestrator reads each loaded `.chiplet` YAML's
`interposer.adapter` field (default: `intm4tm2` for
backward compatibility) and propagates it to the ADK runner invocation
as `--interposer-adapter <value>`.

## Gustavo's SP-031 (`interposer/libs.tech/klayout/python/generate_kicad_dru.py`)

His DRU generator `import`s
`adk.kicad.dru.generate_assembly_dru.render_assembly_rules` and appends
its output as a `# === ADK assembly rules ===` section to the unified
`.kicad_dru` it emits. The `--interposer-adapter <name>` flag is
forwarded.

Provenance comments emitted in the unified DRU include sha256 of every
input JSON *and* the active adapter `.drc`.

## Standalone DRC

```bash
python adk/klayout/drc/run_drc.py \
    --path <assembled.gds> \
    --interposer-adapter intm4tm2 \
    --run_dir /tmp/adk_drc
```

The runner produces a KLayout `.lyrdb` results file under `--run_dir`,
identical in structure to the interposer-PDK runner's output.

## Ecosystem discovery convention

Cross-repo lookups never use fixed-depth path arithmetic
(`parents[N]`, `../..`). Every consumer resolves its dependency with
the same chain, first hit wins:

1. **Environment variable** naming the dependency root.
2. **KiCad project text variable** of the same name, where a board is
   in scope (plugin contexts only).
3. **Upward walk** from the consumer's own file looking for the
   conventional sibling directory name.
4. **Loud failure.** If the dependency is required for correctness the
   tool aborts with the probed locations and the variable to set; a
   silent degraded mode is only acceptable when the output remains
   correct without the dependency (e.g. blackbox fallback layer table).

| Variable | Dependency root | Consumers |
|---|---|---|
| `ADK_ROOT` | adk/ | plugin DRC step, gds_to_kicad canonical layers |
| `INTERPOSER_PDK_ROOT` | interposer PDK (`libs.tech/klayout/python/`) | hyp_to_gds Cu-pillar generation |
| `INTERCONNECT_PDK_ROOT` | interconnect_pdk/ | manifest readers, chiplet-studio fragments, bump3d |
| `GDS_TO_KICAD_ROOT` | gds_to_kicad/ | bump_mirror PinList import |
| `KICAD_CHIPLET_PYTHON` | worker interpreter (not a root) | plugin orchestrator |

Precedent for the hard-fail rule: a complete GDS emitted without its
requested Cu pillars looks fabricable and no downstream DRC can flag
the absent geometry -- hyp_to_gds therefore aborts when pillars are
requested and the interposer PDK is unreachable.
