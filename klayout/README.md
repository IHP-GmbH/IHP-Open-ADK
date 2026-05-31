# KLayout assets

Assembly DRC deck and standalone runner.

## Files (populated in subsequent commits)

- `drc/adk_assembly.drc` — top-level deck. JSON loader + eval-with-shared-locals
  pattern modelled on `interposer/.../interposer_ihp.drc`.
- `drc/rule_decks/layers_def.drc` — abstract layer definitions (only
  `exchange0_drw`, `exchange1_drw`). Everything else comes from the interposer
  adapter.
- `drc/rule_decks/8_1_assembly.drc` — the four ASM rules (a, b, e, f),
  expressed in terms of abstract names.
- `drc/run_drc.py` — standalone runner. `--interposer-adapter` is REQUIRED
  and is validated before deck evaluation; missing required inputs abort
  the run with an explicit error.
- `lyp/adk_layers.lyp` — LYP fragment for `exchange0` / `exchange1`. Loaded
  alongside the interposer LYP when both are needed.
