# Tests

Regression suite for the ADK assembly DRC runner, the KiCad DRU generator, the
interconnect (IXN) axis, and the boundary-manifest viewer.

The suite is deliberately **interposer-agnostic** and **PDK-free**. The DRC
tests use a synthetic adapter (`fixtures/test_interposer_adapter.drc`) that
declares `chiplet_attachment_input = polygons(999, 0)`, and every GDS fixture
is synthesized on the fly by `conftest.py` (plus its `<stem>.boundaries.json`
sidecar), so nothing depends on committed binary layouts or a real IHP PDK
install. A handful of tests do exercise the real IHP interposer adapter
`intm4tm2`, but that adapter is committed in-repo (`pdk_adapters/interposer/intm4tm2.drc`),
so the no-PDK guarantee still holds.

## Running

Plain pytest, run from the ADK root (`tools/adk`):

```
pytest tests/
```

A specific module or test:

```
pytest tests/test_dru_generator.py
pytest tests/test_drc_regression.py::test_assembly_overlap_triggers_asm_a
```

### Requirements

- `test_dru_generator.py` and `test_chiplet_pads.py` are pure Python; they run
  on a bare checkout with nothing but pytest.
- `test_drc_regression.py` and `test_interconnect_axis.py` shell `klayout -b`
  for the deck; both modules skip entirely when the `klayout` CLI is not on
  `PATH`.
- `test_boundaries_to_rdb.py` (and `conftest.py`) need the `klayout.db` /
  `klayout.rdb` Python modules; `test_boundaries_to_rdb.py` `importorskip`s them.

On a bare checkout you will see the KLayout-dependent modules skip; that is
expected. The in-image verify gate has the binary and runs everything.

## What the suite covers

- `conftest.py`: the harness. Session fixtures `fixture_layouts` (synthesizes
  every assembly GDS once per session) and `test_adapter`. The six builders in
  `_BUILDERS` cover `assembly_ok`, `assembly_overlap`, `assembly_too_close`,
  `assembly_too_small`, `assembly_pad_outside`, and `collision_internal_exchange0`.
  Each fixture is written with a matching `<stem>.boundaries.json` manifest that
  the runner auto-discovers.

- `fixtures/test_interposer_adapter.drc`: synthetic adapter
  (`chiplet_attachment_input = polygons(999, 0)`) used by all KLayout tests.

- `golden/assembly_rules.dru.golden`: byte-comparison baseline for the KiCad
  DRU generator. Regenerate with
  `python ../kicad/dru/generate_assembly_dru.py > golden/assembly_rules.dru.golden`.

- `test_drc_regression.py`: runs the runner via subprocess against each fixture
  and asserts the expected ASM rule set (ASM.a overlap, ASM.b spacing, ASM.e min
  area, ASM.f pad-outside). Also gates: manifest-native vs legacy-exchange0
  equivalence (`test_legacy_and_manifest_modes_agree`), collision-proofness
  (manifest mode ignores chiplet-internal exchange0 geometry that legacy mode
  flags), loud aborts on a missing/unparseable/wrong-version manifest, abort
  when the adapter omits a required input, and `intm4tm2` adapter-id resolution
  with no stale aliases.

- `test_interconnect_axis.py`: the optional second adapter axis. Without
  `--interconnect-adapter` no IXN rule runs; with one, bump-to-bump IXN.b/IXN.e
  run over the attachment pads. Method modularity: the same 20 um-spacing
  geometry fails under `ihp_cupillar` and passes under `vendorx_microbump`.
  Per-method mode (`--interconnect-methods`) scopes each method's numbers to its
  dies' pads, applies a conservative cross-method spacing (IXN.x), and falls
  back to the assembly-global adapter numbers for unclaimed pads.

- `test_boundaries_to_rdb.py`: the `boundaries_to_rdb` core that turns a
  boundary manifest into a KLayout `.lyrdb` marker database (shared by the
  `show_boundaries.lym` GUI macro). Covers sidecar discovery, manifest
  accept/reject, one sub-category per chiplet, the micron-not-DBU marker
  invariant, and the `SUPPORTED_MANIFEST_VERSION` pin between runner and viewer.

- `test_chiplet_pads.py`: validates `config/chiplet_pads.json`, the canonical
  black-box chiplet layer registry (`pad_drawing` / `pad_text` / `outline`),
  against the registry schema.

## Migration equivalence gate

The load-bearing invariant is that switching the boundary source must not change
results: for every standard fixture the manifest-native path and the legacy
exchange0 path must report the identical violation set
(`test_legacy_and_manifest_modes_agree`). The companion
`test_collision_internal_exchange0_ignored_in_manifest_mode` proves the
manifest-native deck never reads the fab exchange0 layer, so chiplet-internal
overlaps that legacy mode flags (ASM.a) stay clean in manifest mode.
