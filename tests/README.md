# Tests

Regression harness. Tests must remain **interposer-agnostic**: they use a
synthetic adapter (`fixtures/test_interposer_adapter.drc`) that declares
`chiplet_attachment_input = polygons(999, 0)`, so the suite never depends on
the IHP PDK being installed.

## Files (populated in subsequent commits)

- `fixtures/test_interposer_adapter.drc` — synthetic adapter used by all
  KLayout tests.
- `fixtures/assembly_*.gds` — minimal layouts engineered to trigger or pass
  each ASM rule (overlap, too-close, too-small, pad-outside, ok).
- `golden/assembly_rules.dru.golden` — byte-comparison baseline for the
  KiCad DRU generator.
- `test_drc_regression.py` — runs the ADK runner against each fixture and
  asserts the expected ASM violations.
- `test_dru_generator.py` — byte-diff against the golden DRU.

## Acceptance criterion (load-bearing)

Combined post-migration violation count on the `interposer_wire_bonding_demo`
reference design equals the pre-migration baseline (currently 0 violations
across 27 rules).
