# Changelog

## Unreleased

Added `openroad/chiplet2dbx.py`: exports a finalized `.chiplet` assembly to
3Dblox (`.3dbv` + `.3dbx` + minimal per-technology LEFs) for OpenROAD's
`read_3dbx` / `check_3dblox`. Geometric and lossy by declaration; verifies
the z-mounting rule and fails loudly rather than emit inconsistent geometry.
Ships with the vendored `chiplet_format_io` reference reader (`vendor/`) and
a three-tier test suite (goldens, semantics, live linter answer-key).

## 0.1.0 — 2026-05-31

Initial scaffolding. Directory layout, shared registries (`config/layers.json`,
`config/rule_params.json`), adapter contract documentation, and reserved
stubs for future ADK domains (`thermal/`, `power/`, `timing/`) are in place.

The KLayout deck, KiCad DRU generator, standalone runner, and IHP interposer
adapter will be populated in subsequent commits following the migration plan
(promotion of assembly DRC out of the interposer PDK).
