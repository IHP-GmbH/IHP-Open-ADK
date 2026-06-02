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
`interposer.adapter` field (default: `ihp_sg13g2_interposer` for
backward compatibility) and propagates it to the ADK runner invocation
as `--interposer-adapter <value>`.

## Gustavo's SP-031 (`interposer/scripts/generate_kicad_dru.py`)

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
    --interposer-adapter ihp_sg13g2_interposer \
    --run_dir /tmp/adk_drc
```

The runner produces a KLayout `.lyrdb` results file under `--run_dir`,
identical in structure to the interposer-PDK runner's output.
