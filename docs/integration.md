# Integration

How other tools in the ecosystem consume the ADK.

## `hyp_to_gds.py` (`chiplet_kicad_plugin/`)

The HYP-to-GDS converter stamps `exchange0` polygons for each chiplet.
The layer number is read from `adk/config/layers.json` via the
`ADK_ROOT` environment variable (default: `../adk`). A hardcoded
fallback to `(190, 0)` is kept with a one-time warning if the config
is missing.

No label-stamping is added: chiplets remain generic in the GDS.

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
