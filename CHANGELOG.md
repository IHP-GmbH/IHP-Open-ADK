# Changelog

All notable changes to the ADK are recorded here. Versions track the adapter
contract; the `VERSION` file and the `version` keys in `config/*.json` should
match the latest released entry.

## Unreleased

Landed since 0.2.0, not yet tied to a contract bump.

- Added `checks/pads_vs_pillars.py`, a manifest-level check that verifies die
  pad positions against the interposer's as-drawn Cu-pillar/bump positions
  from the new `<gds-stem>.pillars.json` sidecar (pillar manifest, schema
  `config/schema/pillar_manifest.schema.json`, documented in
  `docs/pillar_manifest.md`; produced by hyp_to_gds alongside the boundary
  manifest, exact-version pinned by the reader). Pad sources: gds_to_kicad
  `*.pins.json` pin lists or extraction from the die layout GDS using the
  `config/chiplet_pads.json` layer vocabulary. Matching by pin name with a
  nearest-unique fallback; findings `MISALIGNED`, `PAD_WITHOUT_PILLAR`,
  `PILLAR_WITHOUT_PAD`, `AMBIGUOUS_MATCH` (plus `NO_PAD_SOURCE` under
  `--strict`).
- Added `openroad/chiplet2dbx.py`: exports a finalized `.chiplet` assembly to
  3Dblox (`.3dbv` + `.3dbx` + minimal per-technology LEFs) for OpenROAD's
  `read_3dbx` / `check_3dblox`. Geometric and lossy by declaration; verifies
  the z-mounting rule and fails loudly rather than emit inconsistent geometry.
  Ships with the vendored `chiplet_format_io` reference reader (`vendor/`) and
  a three-tier test suite (goldens, semantics, live linter answer-key).
  Bump-aware mode: per-die `--pins` lists (gds_to_kicad `*.pins.json`) add
  `.bmap` bump maps and per-method bump macro LEFs generated from the
  interconnect PDK manifest, activating the linter's bump-alignment check.
- Interposer adapter renamed to `intm4tm2` (was the SG13G2-named adapter), with
  no aliases. Use `--interposer-adapter intm4tm2`. This is a contract-visible
  change to the adapter id.
- Chiplet boundaries now come from a per-assembly boundary manifest
  (`config/schema/boundary_manifest.schema.json`, documented in
  `docs/boundary_manifest.md`) instead of the `exchange0` 190/0 layer. The old
  layer path is legacy-only, reachable via `run_drc.py --legacy-exchange0` for
  pre-migration GDS; `config/layers.json` is now marked legacy-compat.
- Boundary-viewer KLayout macros (`klayout/macros/boundaries_to_rdb.py`,
  `klayout/macros/show_boundaries.lym`) turn a manifest into a `.lyrdb` marker
  database so chiplet outlines can be inspected. Both manifest readers validate
  the manifest version.
- Generic chiplet pad vocabulary `config/chiplet_pads.json` is now the single
  source of truth for the black-box pad layers; the standalone `generic.lyp`
  generator was dropped.
- Ecosystem discovery convention documented (including `${VAR}` path
  references), and a GitHub Actions test workflow added
  (`.github/workflows/tests.yml`).

## 0.2.0 - 2026-06-03

Added an optional second adapter axis for interconnect (bumps, pillars,
microbumps) on top of the existing interposer axis. The change is purely
additive: with no interconnect adapter, behaviour is identical to 0.1.0.

- New `run_drc.py --interconnect-adapter` flag (alongside `--interposer-adapter`).
  Shipped adapters: `pdk_adapters/interconnect/ihp_cupillar.drc`,
  `ihp_sbump.drc`, `vendorx_microbump.drc`.
- Interconnect rule defaults in `config/interconnect.json` with schema
  `config/schema/interconnect.schema.json`; consumed by the KLayout deck's
  interconnect rule block. Defaults match the IHP cu-pillar Option 1 numbers
  (35 um opening, 40 um space, 75 um pitch).
- IXN checks are scoped per interconnect method.
- Adapter contract updated to v0.2.0 (`docs/adapter_contract.md`).

## 0.1.0 - 2026-05-31

Initial scaffolding: directory layout, shared registries
(`config/layers.json`, `config/rule_params.json`), adapter contract
documentation, and reserved stubs for future ADK domains (`thermal/`,
`power/`, `timing/`).

The first working pieces landed in the commits that followed the scaffold:

- KLayout assembly DRC deck (`klayout/drc/adk_assembly.drc`),
  interposer-agnostic.
- IHP interposer adapter (now `intm4tm2`,
  `pdk_adapters/interposer/intm4tm2.drc`).
- Standalone DRC runner and regression harness
  (`klayout/drc/run_drc.py`).
- KiCad DRU generator (`kicad/dru/generate_assembly_dru.py`) with
  per-interposer override support.
