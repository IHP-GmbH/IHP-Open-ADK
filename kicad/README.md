# KiCad assets

KiCad DRU generator that produces assembly rules mirroring the KLayout deck.

## Files (populated in subsequent commits)

- `dru/generate_assembly_dru.py` — generator. Reads `../config/rule_params.json`
  for defaults and accepts `--interposer-adapter <name>` for per-interposer
  overrides. Exposes `render_assembly_rules(params: dict) -> str` for
  downstream import (used by Gustavo's SP-031 generator).
- `dru/templates/assembly_rules.dru.jinja` — Jinja2 template.

Output is meant to be concatenated into a KiCad project's `.kicad_dru` file,
or imported as a section by another generator (the typical case once SP-031
lands).
