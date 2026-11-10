# Changelog

All notable changes to the ADK are recorded here. Versions track the adapter
contract; the `VERSION` file and the `version` keys in `config/*.json` should
match the latest released entry.

## 0.3.0 - 2026-11-06

The assembly rules change verdicts: `ASM.b` moves from 50 to 200 um, and a new
rule, `ASM.g`, holds every interposer wire-bond pad at least 200 um from a die
edge. All four `config/*.json` and `VERSION` move to 0.3.0.

- `config/rule_params.json`: `ASM_b` 200.00, new `ASM_g` 200.00. A layout that
  passed with dies 50 to 200 um apart now raises `ASM.b`. A reader of
  `rule_params.json` that pins 0.2 sees a higher minor.
- `ASM.g`, die edge to interposer wire-bond pad, euclidean, edge to edge; a pad
  that touches or overlaps a boundary is flagged. It is checked only when the
  interposer adapter declares the new optional input `wire_bond_pad_input`, a
  lambda over a layout source. The deck calls it on a source with every die
  cell the boundary manifest names pruned by exact name (the record's optional
  `cell`, else `<instance>_<source_die>`, `_flipped` for a mirrored record), so
  a die's own bond pads are never read and a carrier pad under a die is. A
  record whose die cell is not in the layout stops the deck. `intm4tm2`
  declares its pads as Passiv 9/0 AND dfpad 41/0 AND TopMetal2 134/0; an
  attachment sidecar derived against the previous `intm4tm2.drc` no longer
  matches its `binds.adapter_deck` and is derived again.
- The resolved-v2 entry writes `cell` into each boundary record: it shares one
  die cell between instances of a die, which the exporter's naming cannot
  express. The schema already admits the property.
- An adapter without `wire_bond_pad_input` runs `ASM.g` as not applicable: the
  report gets an `ASM.g` category with no item, described
  `ASM.g : not applicable: <reason>`, and `run_drc.py` logs
  `ADK DRC not checked: ASM.g (<reason>)` after its banner. A reader that takes
  every category without items for a passed rule must read this one as not
  checked.
- The KiCad DRU `ASM.b` rule holds only die footprints (both with a `GDS_FILE`
  field), so a wire-bond pad footprint with a courtyard no longer counts as a
  chiplet. `ASM.g` has no DRU mapping and is deck-only.
- The KiCad DRU writes the `ASM.b` clearance in mm (`0.2mm`). KiCad applies
  no clearance written in `um` and reports no error, so the rule never fired
  with the KiCad in the ADK image.
- The adapter README no longer shows an override below the default; an
  override is the technology's own rule.
- New `adk_rule_params.py` at the ADK root: `load_rule_params(adk_root)`,
  `read_rule_table(path)`, `parse_adapter_overrides(path)` and
  `parse_interconnect_overrides(path)`, moved out of the KiCad DRU generator,
  which imports them from there. Standard library only, so a consumer pinning
  an ADK tree loads it by file, as `adk_registry.py` loads, and resolves an
  adapter id through `adk_registry.resolve_adapter`.
- The override parsers refuse what they cannot read instead of skipping it:
  a rule table named anywhere but at the start of a literal `<table>['NAME']
  = NUMBER` of the table being read (comment lines included, so a
  commented-out override, which used to be ignored, is refused), a name
  assigned twice, a line continuing an override or an override below other
  code raises `UnreadOverrideError` with every such line and its reason.
  Overrides therefore sit above any other code in the adapter, where no code
  can turn one into text, a branch or part of a larger statement. A number
  whose integer part has a leading zero (`0250`, octal in Ruby) is refused
  as well, and lines split on newlines only, as Ruby's do. The DRU
  generator therefore exits 1 on such an adapter where it used to emit the
  default.
- `read_rule_table` refuses a NaN or infinite rule value.

Also in this release, landed since 0.2.0:

- Re-vendored the `chiplet_format_io` reader 1.5.0 -> 1.21.0, byte-identical
  to the chiplet-spec reference (`vendor/README.md` names the commit). The four
  schemas under `config/schema/` already carry the bytes of that commit and do
  not move. The reader now refuses more on load: `assembly.units` other than
  `um`, a top-level `system` block (a source document), a root `unplaced` key,
  and vocabulary members stamped below their minor version. No ADK fixture or
  example carries any of them; a `.chiplet` that does is now refused by
  chiplet2dbx and pads_vs_pillars before their own checks run.
  The vendored copy now names a commit that the chiplet-spec release carries.
- `checks/assembly_admission.py` admits the dies of a finalized `.chiplet`
  through the installed reader's `die_contract.admit_component` (`--chiplet`,
  `--profile legacy|strict`, `--json`). Legacy asks nothing and imports no
  `die_contract`; strict exits 0 admitted, 1 refused or 2 undecidable, and never
  degrades to a warning. Strict needs the installed `chiplet-format-io`
  distribution with strict admission API revision 4 (its schema ships inside the
  package, so the vendored copy is refused, as is a package that resolves partly
  into `vendor/`).
- The pytest configuration moved unchanged from `pytest.ini` into
  `pyproject.toml`, which also gains a `[tool.tierrun]` table and
  two markers, `klayout_cli` and `openroad_image`, for the cases that shell
  KLayout or run the OpenROAD image. A bare `pytest` still selects every case.
- `bridges/assembly_lvs/resolved_v2_entry.py` checks a die's declared
  `dimensions` against its 206/0 outline in both directions, per axis, in the
  die-local frame: the two must be equal in DBU, where a declared size larger
  than the outline used to pass. The synthetic tests declare each die at its
  outline, and the corpus test expects 2000 and 1600 um, the outline sizes,
  instead of the courtyard's 2020 and 1620; it holds once the corpus `.chiplet`
  is re-finalized by a plugin that sizes dies from their outline. The check
  reads 206/0 for every die, never the layer a die was sized from, so it admits
  any die whose single 206/0 polygon has a bounding box equal to the declared
  size. The `expect_chiplet` round trip now compares each die's width and height
  too, where it used to record them only; the carrier's dimensions and die
  thickness stay recorded. A compared position or die size that is not a finite
  number is a refusal naming its path, where it used to raise. The accepted
  file's `rotation.z` and its `connection_stacks` layers are not yet read that
  way: a non-number there still raises instead of refusing.
- `openroad/chiplet2dbx.py` exports a die from its eda-views record
  (`--views INST=RECORD`, `chiplet-flow-eda-views/v1`) bound to a named die
  contract (`--contract INST=DIR`), at one corner (`--corner`, default the
  records' shared default). The ChipletDef names the record's technology LEF,
  cell LEFs, Liberty and DEF in place; the bump map lists the DEF pad
  instances and masters at their DEF-frame centres with the die's ports and
  nets (`- -` for supply pads). Every file used is re-hashed; the record must
  be bound to the contract; pad centres, DEF units and the die area are
  checked against the DEF. A pad must be placed `N` on a one-pin master, and a
  port may have one pad, because OpenROAD places a bump by its unrotated master,
  ties it to the first pin and binds a port to one bump. The system top must
  have the generated shape (one module of wires and dies with named
  connections). Record dies declare `anchor: bbox_center` and a
  flipped one is placed `MZ_MY`. Technology or LEF files that share a stem
  with different bytes are refused (OpenROAD would load only the first), and a
  Liberty file two records name is listed once. `--top-verilog` names a system
  top in the Design block after checking it against the records. The docstring
  now says the target dedupes by file stem, not filename. Exports without the
  new options are byte-identical. The live tests run the OpenROAD container
  with 2 CPUs, 6 GiB and no network.
- Added the ADK-side entry for assembly-lvs resolved v2 documents:
  `bridges/assembly_lvs/resolved_v2_entry.py` reads one
  `assembly-lvs.resolved/v2` document plus an `adk-resolved-attachment` 1.0.0
  sidecar (`config/schema/resolved_attachment.schema.json`) and writes the ADK
  view: an assembled GDS whose carrier cell is a geometry-preserved flattened
  copy of the resolved interposer layout with the ADK die layouts instanced on
  it, the boundary manifest, the per-method IXN sidecar, the pillar manifest
  (1.1.0) written from the attachment bodies measured on the carrier, a
  `.chiplet` with real `connection_stacks` and z computed in Decimal, and an
  entry manifest with every input and output hash, the frame constants, the
  per-layer preservation proof and the exhaustive terminal, proxy, pad, label,
  opening and body correspondence tables. The decoder is a strict nominal subset
  of v2 (duplicate keys refused at the syntax level, every number a Decimal from
  its text, identity carrier, empty equivalences, zero uncertainty bounds, one
  terminal per port). `derive_attachment_sidecar.py` derives the sidecar from an
  accepted `.chiplet` plus pillar manifest, recomputing z and refusing a
  disagreement. Profile `nominal-identity-carrier/1.0.0`; the attachment shape
  and contact predicates are corpus-binding controls, not process rules. Two are
  authored predicates that form the nominal bbox/correspondence profile: the
  exact bbox-diameter relation on concentric shapes (no regular-polygon geometry
  and no numerical local-clearance bound is enforced) and the whole dfpad
  covered by one merged contact conductor with shared boundaries allowed; the
  literal physical predicates (Euclidean `enclosing_check` at the manifest
  enclosure, strict containment in the conductor) are measured on every body and
  recorded as data, never as a clearance (`docs/resolved_v2_entry.md` keeps the
  finding visible: 400 edge pairs at 7.5 um on the first reference-design body).
  Six LVS review findings on an earlier version of the entry are closed with
  controls: `assembly.id` is admitted only as one plain path component and every
  output name must stay inside the fresh directory; every declared interface
  contact is inspected in either role order (one contact per die-side
  conductor); a holed 206/0 outline is refused, not filled; every consumed input
  including `chiplet_pads.json` is bound and re-hashed after the outputs exist;
  the derive tool validates before atomic publication and requires the pillar
  method inventory, recording the derivation inputs as `derived_from`; a
  leading-zero numeric scalar is refused as ambiguous and the differential
  compares normalized values, not only verdicts, with numeric strings an
  explicit supported-subset difference; the grid power-of-ten test is numeric,
  not textual; strict containment is interior containment (no implied one-DBU
  clearance); the vertex-count invariant is enforced across dies. The second LVS
  review closes seven more with controls: the differential's two projections
  canonicalize numbers by value (not spelling) over an explicit denominator that
  now includes carrier io_pads and logical nets; output names are checked with
  their own 255 rule so every valid id (up to 64) produces every output; the
  `derived_from` sources are opened and authenticated, the sidecar, the expected
  chiplet and the derivation inputs join the closure, every parsed input is
  parsed from the bytes that were hashed and layouts are hashed before and after
  the KLayout read; the derive tool publishes with a hard link that never
  replaces a destination, even one that appears during validation; the label
  predicate is point-interior; the method-global vertex control discriminates.
  The corpus run and the frozen-parser differential are gated tests that never
  count as unit evidence.
- Re-vendored the chiplet-spec contract files from the chiplet-spec reader
  1.5.0 integration: the four schemas under
  `config/schema/` (boundary_manifest, interconnect, layers, rule_params) and
  the `chiplet_format_io` reader, 1.3.0 -> 1.5.0, byte-identical to the
  reference. Schema changes: every key and name pattern now ends in
  `(?![\s\S])` instead of `$`, so a value with a trailing line break is
  refused instead of matched, and `layers` caps `gds_layer` / `gds_datatype`
  at 65535 (the 16-bit stream field; the intm4tm2 interposer maps datatypes
  300 and 301) instead of 255. `tests/test_chiplet_pads.py`, which mirrors the
  layers schema by hand, follows both; `tests/test_config_consistency.py`
  gains controls that prove the anchors and the cap are live in the copies
  here. The shipped data files are unchanged and still validate. Reader
  1.4.0/1.5.0 refuse to write a top-level key the emitter cannot spell and
  expose the top-level key sequence as a public verdict;
  `SUPPORTED_FORMAT_VERSION` stays 1.0, which only says the accepted format
  version did not move; whether a given existing `.chiplet` passes the 1.5.0
  reader's other checks is decided by that reader on that file, not here.
- `klayout/drc/run_drc.py` exits 2 with an "ADK DRC engine failure" diagnostic
  when the deck aborts or writes no report. It used to pass KLayout's
  own exit through, and KLayout exits 1 on a deck raise, the runner's code for
  findings, while the aborted run leaves an empty report that reads as clean;
  a caller could take an engine failure for either verdict. Exit 0 and 1 keep
  their meaning, and they are verdicts only for a run that reached the deck and
  came back with a valid report: the preflight, tooling and parser paths
  (argument validation, adapter and registry resolution through `adk_registry`'s
  table, report parsing) still exit 1 or their own codes, so the 0/1/2 mapping
  is not a global classifier of every exit 1. `tests/test_interconnect_axis.py`
  asserts the abort exit plus the deck's own diagnostic
  (`test_abort_exits_2_with_an_engine_diagnostic`, a method naming an unknown
  die); `tests/test_run_drc_exit_codes.py` pins the mapping with no KLayout
  spawn, so it stays covered on a bare checkout.
- Fixed a physical false clean in per-method interconnect DRC.
  `klayout/drc/rule_decks/8_2_interconnect.drc` never checked a NAMED method's
  pads against the pads OUTSIDE every method region: `IXN.b.<m>` sees only its
  own method's pads, `IXN.b.unclaimed` only unclaimed pairs and
  `IXN.x.<m1>.<m2>` only two named methods, so a claimed pad 20 um from an
  unclaimed pad under a 40 um rule signed off clean (exit 0) in per-method
  mode while assembly-global mode flagged it. New `IXN.x.<method>.unclaimed`
  checks each named method against the unclaimed pads with the stricter of the
  method's and the adapter's spacing, mirroring the existing cross-method
  rule (the larger-of-two is the deck's compatibility convention for pads
  under unequal rules, not a manufacturing rule from either PDK). Without an
  adapter the unclaimed pads have no rule at all; they are now reported as
  `IXN.unassociated` (a coverage finding, exit 1: the pad was not checked,
  not a geometric violation) instead of a log line and a clean exit. No number
  is invented for that case. Assembly-global mode is unchanged.
- Vendored `chiplet_format_io` reader bumped 1.1.0 -> 1.3.0 (verbatim from
  chiplet-spec `0d850ca`). 1.3.0 rejects the non-LF/CRLF line breaks a smuggled
  value can hide behind (NEL U+0085, U+2028, U+2029); `SUPPORTED_FORMAT_VERSION`
  is unchanged (1.0), which only says the accepted format version did not move;
  a file that carries one of those line breaks is now refused. The vendored
  adapter oracle (`tests/fixtures/adapter_id_cases.json`) is unchanged (same
  sha256); its provenance note now points at chiplet-spec `origin/dev`.
- BREAKING: adapters are selected by REGISTRY ID ONLY. `run_drc.py` and
  `kicad/dru/generate_assembly_dru.py` no longer accept a path for
  `--interposer-adapter` / `--interconnect-adapter`, and no longer join a
  shortname onto `pdk_adapters/<axis>/`; both resolve through `adk_registry`
  and nothing else. The old resolvers accepted any existing `.drc` (absolute or
  CWD-relative) and let a shortname like `../interconnect/ihp_cupillar` escape
  the vetted directory, which mattered because the adapter is `eval`'d Ruby and
  the field naming it comes from an untrusted `.chiplet`. Removed with no
  deprecation window: a window keeps the hole open for its length. Shipped ids
  (`intm4tm2`, `ihp_cupillar`, `ihp_sbump`, `vendorx_microbump`) are unchanged.
  To use an unregistered deck, register it in a registry layer
  (`$ADK_REGISTRY_DIR/registry.json` or
  `$XDG_CONFIG_HOME/adk-tools/registry.json`) and name the id; see the new
  `docs/adapter_contract.md`, which the code has referenced since 0.2.0 without
  it existing. `run_drc.py` gains `--list-adapters`, resolves adapters before
  probing the KLayout binary, and reports the registry that answered; the DRU
  generator maps registry failures onto exit 1/5/6 instead of a traceback. The
  deck splices the adapter path into eval'd Ruby as an inspected literal.
  `adk_registry.validate_adapter_id` extends the id check with the adapter
  field's `.drc` negative (an `evil.drc`-shaped id is refused at validation, not
  only at lookup), matching `chiplet.schema.json`'s `*.adapter` pair; a parity
  test runs the shared chiplet-spec oracle
  (`tests/fixtures/adapter_id_cases.json`) so the ADK, the KiCad plugin,
  chiplet-studio and the Mosaic engine reject the same set.
- Hardened the frame-contract handling shared with the interposer pipeline.
  `checks/pads_vs_pillars.py` now validates each die's `anchor:`: it supports
  only `gds_origin` (die-local pads + `position`, its existing transform); a die
  declaring `bbox_center`, an unknown anchor, or NO `anchor:` is a hard error
  (exit 2) instead of a silent bbox-corner misplacement that could flip a match
  to open. Absent is rejected rather than assumed because the frame contract
  (`coord_frame_contract.md` 2.2) defaults an absent anchor to `bbox_center`
  (which this check cannot consume), so a die must declare `anchor: gds_origin`
  explicitly -- which every gds_to_kicad die and the plugin writer already
  emit. It also guards the die `rotation.z` parse (a non-numeric/malformed
  rotation is exit 2, not an uncaught exception leaking to exit 1 and colliding
  with the findings tier), and `main()` runs the whole pipeline -- check AND
  report emission -- inside one guard with an unexpected-exception catch-all, so
  exit 1 is structurally reachable only via the deliberate findings return.
  `openroad/chiplet2dbx.py` gets the same die-anchor guard (`bbox_center`/
  unknown/absent -> `ExportError`) and wraps its `rotation.z` parse into a clean
  `ExportError` (keeps its 0/1 exporter contract). Both tools reject the
  non-canonical `face_down` orientation token pointing at `flip_chip` (the
  frame contract defines only `face_up` and `flip_chip`).
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
  `--strict`). Pillars the producer's collision auto-resolve moved are
  demoted to warnings, bounded by the optional `auto_resolve_shift_um`
  magnitude the manifest records (a deviation beyond shift + tolerance stays
  `MISALIGNED`). Rejects a non-`um` assembly and non-finite/negative
  tolerances rather than passing silently.
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
