# checks/

Manifest-level assembly checks: tools that verify an assembly against its
sidecar manifests (boundary manifest, pillar manifest) without re-deriving
geometry from the GDS. They complement the KLayout DRC deck under
`klayout/drc/`, which owns the geometric fab-layer rules.

## pads_vs_pillars.py

Verifies die pad positions against the interposer's as-drawn Cu-pillar/bump
positions recorded in the assembly's pillar manifest
(`<gds-stem>.pillars.json`, see `docs/pillar_manifest.md`).

```bash
python checks/pads_vs_pillars.py \
    --chiplet demo.chiplet \
    --pillars build/demo_interposer.pillars.json \
    --pins U1=chiplets/die_a.pins.json \
    --gds-pads U2 \
    [--tolerance-um 1.0] [--json report.json] [--strict]
```

Per-die pad sources: a gds_to_kicad `*.pins.json` pin list (`--pins`), or
extraction from the die's `.chiplet` layout GDS (`--gds-pads`; pad layers
from `config/chiplet_pads.json`, requires the `klayout.db` Python module).
Findings: `MISALIGNED`, `PAD_WITHOUT_PILLAR`, `PILLAR_WITHOUT_PAD`,
`AMBIGUOUS_MATCH` (and `NO_PAD_SOURCE` with `--strict`). Exit codes: 0
clean, 1 findings, 2 usage/validation/tooling errors.
