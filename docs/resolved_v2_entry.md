# Resolved v2 entry

`bridges/assembly_lvs/resolved_v2_entry.py` turns one assembly-lvs resolved v2
document (`assembly-lvs.resolved/v2`) plus an ADK attachment sidecar into the
ADK view of the same assembly. `bridges/assembly_lvs/derive_attachment_sidecar.py`
derives that sidecar from an accepted `.chiplet` and its pillar manifest.

```
bridges/assembly_lvs/derive_attachment_sidecar.py \
    --chiplet <corpus>/design/outputs/design.chiplet \
    --pillars <corpus>/design/outputs/design.pillars.json \
    --document <corpus>/reference/resolved.json --dies-root <corpus>/design/chiplets \
    --out design.adk_attachment.json
bridges/assembly_lvs/resolved_v2_entry.py --sidecar design.adk_attachment.json --out build/entry
```

The entry is a nominal identity, frame and correspondence adapter. It runs no
DRC and no LVS, invents no z, resolves no collision and certifies no process. The
scope statement is written into every manifest: the bridge establishes
reproducible nominal cross-tool binding; it does not prove clean DRC or LVS,
uncertain-placement preservation, contact completeness, manufacturing
qualification, or any novelty from combining two checkers.

## Profile

Profile id: `nominal-identity-carrier/1.0.0`. The profile is the documented
subset of valid v2 the entry accepts, plus the corpus-binding attachment
predicates it applies. Widening or relaxing any of it is a new profile version
with its own review.

Subset restrictions on top of the nominal v2 semantics (all refusals, exit 2):

- the carrier is placed at identity (`rotation_deg 0`, `mirror_x false`);
- `contracts.internal_equivalences` is empty (no component certificate is read);
- every `uncertainty.component_translation_um` bound is exactly 0 (a nominal
  entry must not present a bounded placement as if the bound did not exist);
- exactly one terminal per `(die, pin)`: a multi-geometry port needs an
  equivalence this profile does not read;
- each die maps exactly one dedicated die-contact conductor (the die-side layer
  of an interface contact), and every terminal of the die uses it;
- one interposer contact conductor across all interface contacts, and one
  contact per die-side conductor: every declared contact is inspected in either
  authored role order, so a second pairing of the same die-side conductor
  (`DIE_CONTACT -> TM1` beside `DIE_CONTACT -> TM2`, or the same pairing
  repeated reversed) is a refusal, never a first-match lookup;
- integers in the YAML form must be plain decimal (`0x`, `0o`, sexagesimal are
  refused), and a numeric scalar with a leading zero (`012`, `012.5`) is refused
  in both forms because YAML 1.1 reads `012` as octal 10 while a decimal reader
  gives 12 (policy: the entry refuses the ambiguity instead of choosing; JSON
  forbids the form anyway); a layout reached through a symlink is refused;
- `assembly.id` must be one plain path component (`[A-Za-z0-9_.-]`, 1 to 64
  characters, starting alphanumeric): it is the output file stem, so a
  separator, a leading dot or an empty string is refused before any output
  exists. The completed output names (`<id>_assembled.boundaries.json` and the
  rest) are checked separately with the same alphabet and the filesystem limit
  of 255, so every valid id produces every output, and a second gate rejects any
  output name that would leave the fresh output directory.

Two explicit supported-subset differences from the frozen parser, both applying
with and without the profile; the differential lists each by name as a
documented entry-side difference and neither relaxes the contract. First,
numeric tokens only: a numeric string (`"50"`) is a number for the frozen
parser's `_decimal` and is refused by the entry. Second, the exact-safe numeric
domain: every coordinate and size, converted from Decimal micrometres through
`technology.grid_um`, must lie within plus or minus 1073741823 grid units
(`SAFE_GRID_COORD`, so that DBU arithmetic stays exact); the frozen parser
aligns a Decimal to the grid but has no magnitude limit, so a document with a
terminal at 2 to the 30 micrometres is accepted there and refused here in both
modes (`outside_safe_range`; measured by the frozen-parser differential, the
bound is kept on purpose, not weakened to mimic the parser). Controls prove the
boundary: plus or minus the bound is accepted by both modes, one DBU outside is
refused by both. Numeric values are compared by value, not spelling: `grid_um`
0.0010 or 1E-3 is the same power of ten as 0.001 and is rendered normalized in
the manifest.

Everything else follows the frozen parser: required and unknown keys, duplicate
keys at the syntax level (JSON object or YAML mapping), numbers read as Decimal
from their scalar text (booleans and strings are not numbers, NaN and infinities
refused), grid alignment on `technology.grid_um`, even window extents, portable in-root
layout paths, conductor and via declarations, layer mappings to a declared
conductor or via with each GDS source and each technology layer mapped once,
interface contacts as `(role, layer)` pairs in either order, the carrier endpoint
identity `(io_pad.id, io_pad.pin)` unique as a tuple (a repeated `pin` string is
not a refusal), the complete partition of every physical endpoint into exactly
one logical net.

## Frames

- `L(die)`: die-local GDS frame (the `gds_origin` anchor), integer DBU.
- `A`: the resolved assembly frame.
- `G(int)`: the interposer's own GDS frame (the resolved carrier bytes).
- `C`: the ADK canonical frame, `G(int)` rebased to the lower-left corner of the
  interposer top-cell bbox (`.chiplet` positions, `pillars[].x_um/y_um`).

With mirror-before-rotation on both sides (assembly-lvs `_orient_xy`, ADK
`pads_vs_pillars`), and `theta_i = 0`, `m_i = false`:

```
c(p) = pos_d + R(theta_d) M_y^m_d (p - anchor_d) - pos_i + anchor_i - corner
g(p) = c(p) + corner
```

`R(t) M_y = R(t + 180) M_x`, so a resolved die `(theta, mirror_x true)` is the
ADK die `rotation.z = theta + 180 mod 360`, `orientation flip_chip`; without
mirror `rotation.z = theta`, `face_up`. The `.chiplet` die position is `c(0)`
(the image of the die-local origin), never a copy of `pos_d`. The die instance in
the assembled GDS is `Trans(rot = theta_d // 90, mirror = m_d, disp = g(0))`. The
entry checks on every run that the placed 205/0 pad centres read back through
the instance equal the integer chain, and that the boundary `transform` (legacy
convention) reproduces `polygon_dbu`.

All layouts in play (resolved carrier and proxies, ADK die layouts, the
assembled output) must have dbu equal to `grid_um`; nothing is rescaled.

## Sidecar `adk-resolved-attachment` 1.0.0

Schema: `config/schema/resolved_attachment.schema.json`. `document` and
`dies_root` are root pointers relative to the sidecar directory; every other
path is in-root. `binds` is an exact inventory: the document, the layout of every
component, the ADK die layout of every die, the methods manifest and the
interposer adapter deck, each by sha256; a stale hash, an extra entry or a
missing one is a refusal. Bytes parsed are bytes hashed: the document, the
sidecar, the adapter deck, the methods manifest, the expected chiplet and the
pad-layer config are each read once and parsed from the bytes that were hashed;
a layout (KLayout reads from a path) is hashed immediately before and after its
read and both hashes must equal the bound value. The recorded binds also carry
the sidecar itself, the expected chiplet (bound when the sidecar is loaded,
re-checked when it is parsed for the round trip) and the derivation inputs. After
the outputs exist every bound input is hashed again
(`inputs_rechecked_after_outputs` in the manifest); an input that changed during
the run is a refusal and the outputs are discarded. `derive_attachment_sidecar.py`
reads its two inputs once, validates its candidate with the entry's loader beside
the destination, then publishes with a hard link that creates the destination
atomically and fails if any destination exists, including one that appeared
during validation; a refusal leaves no candidate behind and no destination is
ever replaced. The derive tool requires the pillar manifest's method inventory
(every placed die's method present, every declared method known to the methods
manifest with the same numbers) and writes `derived_from`, the paths and sha256
identities of the accepted `.chiplet` and of the pillar manifest; the entry
resolves both paths relative to the sidecar, opens and authenticates both files
against the recorded identities (a missing file or changed bytes is a refusal),
refuses a `derived_from.chiplet` that is not the `expect_chiplet` file, records
them in the manifest and includes them in the closure, so the accepted-source
provenance is preserved to the round trip.
`interposer.attachment_layers` names opening, dfpad,
body, body2 and mark; opening and dfpad must equal the pillar branch of the
`chiplet_attachment_input` line parsed literally from the adapter deck (the deck
is data here, never executed). Adapter ids go through `adk_registry`; the methods
manifest is an in-root path under the registry root `INTERCONNECT_PDK`.

## Bodies and the corpus-binding controls

On the carrier, `attach = merged(opening and dfpad)` gives the bodies; each of
the five shapes per body must be one polygon without holes, square bbox with an
integer centre, point-symmetric, and the six centre sets (attach plus the five
roles) must be identical. Per body: opening strictly inside dfpad, body strictly
inside dfpad, opening strictly inside body, mark bbox equal to the dfpad bbox,
the sbump branch of the deck empty. Per body and method (the die's method from
the sidecar): `opening = passiv_opening_um`, `dfpad = passiv_opening_um + 2 *
tm2_enclosure_um`, `body = body_diameter_um`, exact in DBU; vertex counts equal
across the bodies of one method.

Two of these are authored predicates of profile 1.0.0 that differ from a
literal physical reading of the attachment geometry. They form the nominal
bbox/correspondence profile, explicitly not a literal 7.5 um physical enclosure
and not an equivalent discretization tolerance; they are not relaxations of
anything, and the literal physical finding stays visible here:

- Enclosure, authored predicate: concentricity (the six centre sets coincide)
  plus the exact nominal bbox-diameter relation to the method manifest (`dfpad =
  opening + 2 x tm2_enclosure_um`, `opening = passiv_opening_um`, `body =
  body_diameter_um`, in DBU) plus strict containment of the opening in the dfpad
  and in the body. The guards enforce neither a regular 256-gon geometry nor any
  numerical local-clearance bound. Literal physical finding, kept visible: the
  literal predicate `dfpad.enclosing_check(opening, tm2_enclosure_um)` is not
  empty on the reference design; the artwork is concentric 256-gons whose bbox
  extents meet the relation exactly (50 = 35 + 2 x 7.5), but a polygon's apothem
  is below its circumradius, so the Euclidean edge distance at the outer edge
  midpoints is about 7.497 um and KLayout reports 400 edge pairs at 7.5 um on
  the first body (168 under the projection metric). A 7.497 um polygonal
  enclosure is not a 7.5 um minimum clearance and the entry never reports it as
  one.
- Carrier contact, authored predicate: the whole dfpad covered by the contact
  conductor (`dfpad - TM2` empty, shared boundaries allowed) and one merged
  conductor polygon meeting it; a dfpad on no copper is a refusal (a covered
  dfpad lies inside one connected merged region, so the polygon count is a
  consistency record, not an independent predicate). Literal physical finding,
  kept visible: the TM2 polygon under each reference-design dfpad is the dfpad
  itself (same 256-gon, same bbox), so the literal "dfpad strictly inside TM2"
  is false on every body of that design.

Both literal outcomes are measured on every body and written as data beside
the authored predicates, in the body records and in every correspondence row
(`predicates.profile` and `predicates.literal`:
`enclosing_check_edge_pairs_at_manifest_value`,
`enclosing_check_empty_at_manifest_value`, `dfpad_strictly_inside_contact`).
They are not bound and not clearances.

Two definitions. "Strictly inside" is interior containment, exact on integer
coordinates: covered, and no boundary point in common; it implies no clearance,
so a slanted boundary at a positive sub-DBU distance is inside (an earlier
version grew the inner shape by one DBU, which was a one-DBU Chebyshev
clearance, not strict containment). The label predicate has the same interior
semantics for a point: a label position is inside its pad when it is inside the
polygon and on none of its edges (the integer centre of a unit diamond is
interior; a point on an edge or a vertex is not); the earlier 3 x 3 DBU box was
a one-DBU label clearance and is gone. The vertex-count invariant is method-wide
across all dies: every body of one method has the same vertex counts per role,
checked over the whole carrier after the per-die correspondence.

Documented limits of the nominal profile (controls in the test suite): a 35 x 35
um square opening (same bbox) is refused, but by the strict-containment guard
(its corners lie outside the 44 um body), not by any shape rule; the accepted
square control is the 50 x 50 um dfpad with a matching square contact pad
(same bbox, 4 vertices, accepted); a dfpad with two point-symmetric notches on
the diagonals (same bbox, local clearance to the opening well below the
manifest enclosure) is accepted with a larger literal diagnostic recorded; a
same-bbox shape with one vertex pulled inward is refused by the point-symmetry
guard. The entry claims no general
artwork conformity; a failure of an authored predicate means the artwork
differs from what the profile binds, not that a process rule is violated.
Electrical and process verification stay in their own gates (the ADK DRC decks,
the LVS-side contact audit).

## Correspondence

Per die, six finite sets enumerated completely: terminals T, proxies X (the
resolved die layout on the die-contact source layer), real pads P (ADK die
layout, merged 205/0), labels L (texts on 205/25), openings O and bodies B on
the carrier. Maps, each a bijection: `T -> P` (window strictly inside exactly one
pad, each pad hosting one window), `P -> L`, `T -> L` (label text equals the
pin), `T -> X`, `T -> O` (window through `g(p)` strictly inside one opening),
`T -> B` (window centre through `g(p)` equal to a body centre, exact), `X -> B`
(proxy bbox through `g(p)` equal to the body bbox), `O -> B`; `T -> O -> B` must
agree with `T -> B`. Every pad must lie strictly inside the single 206/0 outline
and carry a terminal; every body on the carrier must host exactly one terminal.
The tables are written to `entry_manifest.json` per terminal.

## Outputs

A fresh directory (refused if it exists; nothing is left behind on a refusal).
`<id>` is `assembly.id`, admitted only as one plain path component (see the
profile); every output name is checked to stay inside the directory. The single
206/0 outline of each die must carry no hole: the boundary export writes the
hull only, so a holed outline is refused rather than filled.

- `<id>_assembled.gds`: top cell `<assembly.id>` holding a geometry-preserved
  flattened copy of the resolved carrier top cell (every layer, every shape
  kind, texts included) and one instance per die of the ADK die cell. The proof
  of preservation (per layer: shape and text counts, raw area, merged XOR empty,
  bbox equal) is in the manifest; the assembled file is never byte-identical to
  its input and the input hashes are recorded separately.
- `<id>_assembled.boundaries.json` (1.0.0): the 206/0 outline vertices through
  `g(p)`; `transform.origin_um = g(0) * grid_um`, `rotation_deg` in the legacy
  convention, `mirror_x = m_d`.
- `<id>_assembled.ixn_methods.json` (1.0.0) and `<id>_assembled.pillars.json`
  (1.1.0 with `methods`): `IXN_spacing`/`IXN_pitch` from the manifest's
  `pitch_rules`, `IXN_pad_size = passiv_opening_um`; pillars from the measured
  bodies in C, `moved_by_auto_resolve false` on every record.
- `<id>.chiplet`: interposer with explicit `anchor: bbox_center` and position
  `(width/2, height/2)` of the carrier bbox, io pads rebased to C, dies with
  `anchor: gds_origin`, `position = c(0)` and `z = interposer.z +
  attachment_surface_z + sum(stack heights)` in Decimal, `connection_stacks`
  copied verbatim from the manifest, `netlist.nets` from the logical nets. The
  file is validated with the vendored reader and re-read; the chiplet2dbx z
  identity is checked exactly. Die `dimensions` are the sidecar's declared
  values, and they must equal the 206/0 outline's size exactly in DBU, per axis
  in the die-local frame, with no tolerance: a declared size larger or smaller
  than the outline is refused. The entry reads 206/0 for every die (the outline
  pair of `config/chiplet_pads.json`) and never sees the layer a die was sized
  from upstream: it admits any die whose single 206/0 polygon has a bounding box
  equal to the declared size, and refuses one whose 206/0 bounding box differs,
  e.g. a die sized from 189/4 or 39/4 that also carries a 206/0 of another size.
  The outline size and the (zero) difference are recorded. The carrier
  `top_cell` is the resolved layout's own cell name (a variant may rename it);
  the `expect_chiplet` round trip records it as `carrier_top_cell` and does not
  compare it, while a die `top_cell` is compared. The round trip also compares
  each die's `width` and `height` as decimals, so an accepted file that sizes a
  die from anything but its outline is refused; the carrier's `dimensions` and a
  die's `thickness` are recorded, not compared.
- `entry_manifest.json` (`adk-resolved-v2-entry` 1.0.0): every input and output
  hash, the frame constants, the preservation record, the body measurements, the
  correspondence tables, the z arithmetic, the recorded uncertainty and
  provenance, the adapter resolutions, the ADK revision and the next commands.

Number rendering: JSON and YAML numbers are the integer when integral, else the
nearest double of the exact decimal; the DBU integers beside them are
authoritative.

## Tests and gated controls

`tests/test_resolved_v2_entry.py` synthesizes the whole world (document, carrier
artwork, proxies, ADK dies, manifest, decks, registry layer) and computes
expectations with an independent matrix-form transform: the positive case at
180 with mirror and 90 without, transform controls at every quarter turn with
and without mirror, non-zero anchors, `anchor_i - pos_i != corner`, both
interface-contact orders, one control per refusal, shape and contact controls,
correspondence controls per leg, preservation XOR on a mutated copy, the z
identity, the consumers (`pads_vs_pillars`, `chiplet2dbx`) reading the emitted
files, and the derive round trip.

Two controls are gated, run only where an explicit root names their inputs and
never count as ordinary unit evidence:

- Corpus run: `ADK_RESOLVED_V2_CORPUS_ROOT` names one directory that holds the
  design project under `design/` (`outputs/design.chiplet`,
  `outputs/design.pillars.json` and the die tree `chiplets/`, so die layout
  references such as `../chiplets/<die>.gds` resolve from `outputs/` into it)
  and one directory per variant (`reference`, `equivalent`, `narrow_trace`,
  `route_cut`), each with its `resolved.json`; a variant's carrier layout is the
  path its document names, relative to the variant directory. The control
  asserts the corpus counts (48, 36, 84, 25, 35 nets, 109 endpoints), the worked
  numbers, the `expect_chiplet` round trip and the TM2 XOR areas 0, 0, 900, 40
  um2 against the reference carrier. Under `ADK_RESOLVED_V2_ACCEPTANCE=1` an
  unreachable corpus fails instead of skipping. The control expects the die
  sizes 2000 and 1600 um, the 206/0 outlines, so it holds only once
  `outputs/design.chiplet` is re-finalized by a plugin that sizes dies from
  their outline (`hyp_to_gds.py --update-chiplet-file`), with the positions,
  stacks and pillars unchanged; a courtyard-sized file (2020, 1620) is refused.
- Frozen-parser differential: `ADK_RESOLVED_V2_FROZEN_PARSER_ROOT` (an
  assembly-lvs checkout, optionally bound by
  `ADK_RESOLVED_V2_FROZEN_PARSER_SHA256`); every mutated
  document is fed to the frozen parser and to the entry with and without the
  profile; no document accepted by the entry may be refused by the parser, and
  the documents refused by the entry only are exactly the listed profile
  restrictions and entry-side output-safety refusals (the module constant
  `PROFILE_ONLY_DIFFERENCES` in the test file, a documented list, not blanket
  parity). For every document both readers accept, the normalized values they
  hand on must agree by value, not by spelling (equal Decimals render
  identically whatever their exponent or trailing zeros); the projection's
  denominator is explicit and limited to assembly id and units, grid,
  conductors, component role, top cell, placement and layer map, carrier
  io_pads, terminals, interface contacts and logical nets with their
  connections (not vias, uncertainty or provenance). The YAML leading-zero
  case records the frozen parser's octal reading beside the entry's refusal in
  `verdicts.json`.
