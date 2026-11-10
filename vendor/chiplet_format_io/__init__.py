# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""chiplet_format_io -- permissive reference reader/writer for the .chiplet format.

Apache-2.0. Dependency-clean: depends only on PyYAML. It deliberately does NOT
import ``pcbnew`` or ``klayout`` (or any GPL library), so it can be embedded in
tools under any license, open-source or proprietary.

This is an INDEPENDENT reference implementation of the format described in
``docs/CHIPLET_FORMAT_SPEC.md``. It is intentionally *not* the byte-exact writer
used inside the GPL host tools (the KiCad plugin / KiCad fork exporter): those are
locked to each other by a byte-exact parity gate. Output here is canonical YAML,
semantically equivalent, not byte-identical to those hosts.

Typical use::

    import chiplet_format_io as cfio
    assembly = cfio.load("design.chiplet")      # -> dict, validated
    assembly["assembly"]["name"] = "renamed"
    cfio.dump(assembly, "design.chiplet")

Version policy (docs/VERSION_POLICY.md). One rule governs every versioned
artifact in the format family: a consumer declares the SET of majors it accepts
with one MAJOR.MINOR floor each (a single string is the one-element set); a
declared version whose major is in the set is accepted when its minor is at or
below that major's floor and accepted with a warning when it is higher; a major
outside the set or a malformed value is refused; PATCH is ignored.
:func:`check_format_version` applies it to a ``.chiplet``'s own
``format_version``; :func:`check_contract_version` applies it to any governed
sidecar (``io_pads.json``, ``pins.json``, the black-box padmap, the boundary
manifest, ``interconnect_methods.json``)::

    cfio.check_contract_version(doc["version"], "1.0", name="io_pads.json")

The warn channel, stated once because two references have to mean the same thing
by it. ``on_warn`` is the single NORMATIVE channel: every event, undeduplicated,
in the order it happened, and it is what a consumer counts or gates on.
Everything else this library does with a note is non-normative CONVENIENCE and
may change without a format version moving: the stdlib ``warnings`` emission
here (deduplicated per version, and a process-global the host configures) and
``ChipletDocument::warnings`` on the C++ side. A consumer that needs the events
sets ``on_warn``.

Version constants, and they answer different questions.
:data:`ACCEPTED_FORMAT_VERSIONS` holds the highest ``.chiplet`` ``format_version``
this reader was written for, one floor per major, i.e. a fact about documents.
What a writer STAMPS depends on the kind of document (docs/VERSION_POLICY.md,
"Stamps per document kind"): :data:`SUPPORTED_FORMAT_VERSION` on an assembly
document and :data:`SOURCE_FORMAT_VERSION` on a source document, the one that
carries a ``system`` block. :data:`__version__` is the release of the reader
itself, and it is the value a VENDORED copy of this
file carries into the tool that embedded it. That is what makes byte identity
unnecessary: a consumer requires a reader release (``cfio.__version__``) and a
document version, and never has to hash a copied file to find out what it has.
"""
from __future__ import annotations

import math
import re
import warnings
from collections.abc import Sequence as _Sequence
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple, Union

import yaml

__all__ = [
    "SUPPORTED_FORMAT_VERSION",
    "SOURCE_FORMAT_VERSION",
    "ACCEPTED_FORMAT_VERSIONS",
    "DERIVED_BLOCKS",
    "KNOWN_INTERFACE_TYPES",
    "MEMBER_SINCE",
    "SOURCE_KEY_SINCE",
    "KNOWN_UNITS",
    "check_member_since",
    "content_format_floor",
    "IO_CLASS_INTERFACE_TYPES",
    "__version__",
    "ChipletFormatError",
    "ContractVersionError",
    "ContractVersionWarning",
    "FormatVersionWarning",
    "SourceDocumentError",
    "SystemBlockError",
    "UNPLACED_SINCE",
    "accepted_minors",
    "below_minor",
    "parse_format_version",
    "placed_by_both",
    "unplaced_names_profile",
    "unplaced_refusal",
    "check_contract_version",
    "check_format_version",
    "loads",
    "load",
    "dumps",
    "dump",
    "validate",
    "PREAMBLE_KEY",
    "top_level_key",
    "top_level_blocks",
    "top_level_block",
    "TopLevelKeys",
    "split_top_level_keys",
    "CapturedTopLevelBlock",
    "TopLevelBlockCapture",
    "capture_top_level_blocks",
]

#: The ``format_version`` a writer stamps on an ASSEMBLY document, the kind every
#: format consumer reads (design.chiplet, the KiCad and studio exports). It stays
#: "1.0" while 1.1 adds only what an assembly does not carry, so a reader release
#: never moves the stamp of an assembly that did not change. A single exported
#: string constant, never re-derived from a bump.
SUPPORTED_FORMAT_VERSION = "1.0"

#: The ``format_version`` a writer stamps on a SOURCE document: one with a
#: top-level ``system`` block, which only exists from 1.1 (docs/CHIPLET_FORMAT_SPEC.md,
#: "System"). Two stamps, because the two kinds of document have different
#: readers; see docs/VERSION_POLICY.md, "Stamps per document kind".
SOURCE_FORMAT_VERSION = "1.1"

#: The SET of majors this reader accepts, one ``MAJOR.MINOR`` floor per major
#: (docs/VERSION_POLICY.md, "Changing the major"). The floor is the highest minor
#: this reader implements. A one-element tuple is the ordinary state; a second
#: entry appears only while a major transition is open, and it is a PROMISE that
#: the code path for that major exists, never a wish. Every stamp above must be
#: READABLE here, same major and a minor at or below the floor (conformance
#: asserts it): what this reader writes has to be something it can read back.
ACCEPTED_FORMAT_VERSIONS = ("1.5",)

#: The top-level blocks a source document never carries. Each one is DERIVED
#: from the ``system`` block by the source's finalizer into the assembly document,
#: so a source that also wrote one would hold two answers to one question.
DERIVED_BLOCKS = ("technologies", "connection_stacks", "components",
                  "interconnect", "interfaces", "netlist")

#: The release of THIS reader, and the value a vendored copy carries with it.
#: Distinct from :data:`SUPPORTED_FORMAT_VERSION`, which is about the documents:
#: one says what is on disk, the other says which reader is in the tree. It is the
#: distribution version too (``pyproject.toml`` reads it from here), so a consumer
#: that installed the package and one that vendored the file agree on the number.
#: Bumped under the same policy as everything else (docs/VERSION_POLICY.md).
__version__ = "1.22.0"

#: The closed ``interfaces[].type`` vocabulary (spec validation rule 4). One list
#: lives in four places -- here, ``schemas/chiplet.schema.json``, the spec prose
#: and the C++ ``kKnownInterfaceTypes`` -- and conformance/test_interface_types.py
#: reads all four, so a member added to one alone fails there instead of
#: travelling. It is the vocabulary a WRITER is bound to and the list a consumer
#: reads to decide what it can act on; this reader does not refuse a string
#: outside it, it carries the value and reports the event on ``on_warn``.
#: ``solder_bump`` is the C4-class reflowed solder ball (the interconnect
#: manifest's ``sbump_sac305``); producers emit it only from format 1.1.
KNOWN_INTERFACE_TYPES = ("micro_bump", "copper_pillar", "tsv", "wire_bond",
                         "solder_bump")

#: Per closed vocabulary, the members added after 1.0 and the format minor that
#: added each. A member absent from its map is since 1.0, which is why the map
#: holds only later members. A document stamped below a member's minor is out of
#: contract and is refused by the validator, whether or not a reader knows the
#: member; one stamped at or above it reads by the ordinary rules. The map is
#: schema data first (an ``x-since`` object beside each enum in
#: ``schemas/chiplet.schema.json``); this copy and the C++ ``kMemberSince`` are
#: held to it by conformance/test_member_since.py. Writers read it too: a writer
#: never stamps below :func:`content_format_floor`.
MEMBER_SINCE: Dict[str, Dict[str, str]] = {
    "interfaces[].type": {"solder_bump": "1.1"},
    "io_pads[].io_class": {},
    "components[].anchor": {},
    "components[].orientation": {},
    "assembly.units": {},
}

#: Per key of a SOURCE document added after the ``system`` block's own minor
#: (1.1), the format minor that added it, by its path with ``[]`` for a list
#: item and its bare name for a root key. The rule is :data:`MEMBER_SINCE`'s
#: for a key: a source stamped below the key's minor is refused
#: (:func:`_validate_source`), and a writer never stamps below the highest minor
#: among the keys it writes (:func:`content_format_floor`), so a source that
#: writes none stays at :data:`SOURCE_FORMAT_VERSION`. Schema data first, an
#: ``x-since`` on the property in ``schemas/chiplet.schema.json``, held to this
#: map by conformance/test_member_since.py. The C++ reader has no source path,
#: so it carries no copy.
SOURCE_KEY_SINCE: Dict[str, str] = {
    "system.rails[].volts": "1.2",
    "unplaced": "1.3",
    "system.dies[].admission": "1.4",
    "system.rails[].width_um": "1.5",
    "system.rails[].amps": "1.5",
    "system.rails[].current_density_ma_per_um": "1.5",
}

#: The keys a rail states its routing requirement with (format 1.5), in the
#: order a rail writes them: a width in um, or a current in A with the density
#: in mA/um it is drawn at. The source never computes the width from the pair;
#: the router does.
_RAIL_REQUIREMENT_KEYS = ("width_um", "amps", "current_density_ma_per_um")

#: The minor from which a source may leave its placement to ``packaging-flow
#: place`` (docs/CHIPLET_FORMAT_SPEC.md, "Format 1.3: the unplaced source"): no
#: dies yet, a die with no ``at``, a carrier with no ``size``, ``external`` with
#: no ``edges``. These are omissions, not keys, so they are not in
#: :data:`SOURCE_KEY_SINCE`; the root key ``unplaced`` that comes with them is.
#: A source stamped below it that leaves any of them out is refused with
#: :func:`unplaced_refusal`, and :func:`content_format_floor` reads them. The
#: schema states the same as an ``allOf`` branch on the stamp.
UNPLACED_SINCE = "1.3"

#: The refusal of a document that carries the root ``unplaced`` and no
#: ``system``: the key is a source's, and its pins go on the system's external
#: edges (the schema says the same). The C++ reader refuses with this sentence.
_UNPLACED_IN_ASSEMBLY = ("the root key unplaced belongs to a source (format 1.3), "
                         "beside its system block; this document has no system block")

#: ``assembly.units``, closed (format 1.1). Unlike every other closed vocabulary
#: here this one is MUST-UNDERSTAND: a length read in a unit the reader does not
#: convert is wrong rather than incomplete, so the validator refuses any other
#: value. Absent means ``um``, the normative default. Mirrors the C++
#: ``kKnownUnits``; conformance/test_units.py reads both and the schema enum.
KNOWN_UNITS = ("um",)

#: Validation rule 8's table (spec, "Usage class and interface type"): which
#: ``interfaces[].type`` a pad of a given ``io_class`` may take part in. Two
#: closed vocabularies about one physical joint, so not every pairing exists.
#: The same table is written in the spec and in the C++ ``kPadUsageTable``, and
#: conformance/test_pad_usage_compatibility.py reads all three.
IO_CLASS_INTERFACE_TYPES = {
    "wire_bond": ("wire_bond",),
    "flipped_bump": ("micro_bump", "copper_pillar", "solder_bump"),
    "tsv_bump": ("tsv",),
}


#: The code points a YAML parser breaks a line on and THIS GRAMMAR does not,
#: mapped to the name a refusal quotes. That criterion is what GENERATES the set,
#: and it is executable: conformance/test_top_level_blocks.py derives the members
#: by running PyYAML over U+0000..U+21FF instead of trusting this list, because a
#: hand-written derived list is exactly how CR came to be missing from it for one
#: release. The grammar is what the repeated-key scan, flow rule 4,
#: top_level_blocks() and every splitting host read, so a character that moves a
#: line for the parser and not for the grammar hides a top-level key from all of
#: them at once.
#:
#: For these three, membership is unconditional. CR is the fourth member and the
#: only conditional one, so it is kept out of this mapping and checked on its own:
#: CRLF is the format's other line break, so a CR is ill-formed exactly when the
#: byte after it is not LF. Every consumer of the mapping below means "anywhere".
#:
#: Measured in both directions on PyYAML 6.0.3 and yaml-cpp 0.8.0: with
#: `name: demo<LS>` followed by `format_version: "9.0"`, PyYAML reads a second
#: top-level key and returns format_version '9.0' while yaml-cpp throws "illegal
#: map value"; with the same separator followed by ordinary text, yaml-cpp loads
#: the bytes inside the scalar and PyYAML throws a ScannerError. CR behaves the
#: same way in both shapes, except that yaml-cpp folds it to a space rather than
#: keeping it, so neither reader can be made to imitate the other and the format
#: refuses the bytes instead.
_FORBIDDEN_LINE_BREAKS = {
    "\u0085": "Unicode next line",
    "\u2028": "Unicode line separator",
    "\u2029": "Unicode paragraph separator",
}

#: The conditional fourth member. Not in the mapping above, and not because CR is
#: a lesser defect: it has the WIDEST blast radius of the four, since a CR is one
#: keystroke away from every editor and every CRLF file that lost half a
#: terminator in transit.
_CARRIAGE_RETURN = "\r"


class ChipletFormatError(ValueError):
    """Raised when a .chiplet document is malformed or unsupported."""


class ContractVersionError(ChipletFormatError):
    """A governed artifact declares a version this consumer cannot read.

    Raised by :func:`check_contract_version` for a malformed version string or a
    different major. A subclass of :class:`ChipletFormatError` so a consumer that
    already catches that keeps working; catch this one to tell "the version is
    wrong" apart from "the document is wrong".
    """


class SourceDocumentError(ChipletFormatError):
    """The document is a SOURCE (it has a top-level ``system`` block).

    A source document describes a system to be elaborated, not an assembly, so a
    reader that wants an assembly refuses it whatever ``allow_intermediate``
    says: read as an assembly it has no components and no netlist, and nothing
    downstream could tell it from an empty design. Only a reader of sources
    (``chiplet_system_description.source``) passes ``allow_source=True``. A
    subclass of :class:`ChipletFormatError`, so a caller that already catches
    that keeps working.
    """


class SystemBlockError(ChipletFormatError):
    """A source document whose ``system`` block, or the root ``unplaced`` that
    goes with it, the reader refuses: its structure, or what its stamp lets it
    leave to placement.

    Raised only after everything else in the document is accepted, so a caller
    that catches it knows the rest holds. A reader that walks the block line by
    line (``chiplet_system_description.source``) states these rules on their
    lines, which is why they are told apart. A subclass of
    :class:`ChipletFormatError`, so a caller that already catches that keeps
    working.
    """


class ContractVersionWarning(UserWarning):
    """A governed artifact declares a newer same-major minor than is supported.

    The artifact is still read (as the supported version, ignoring unknown
    additions), but a same-major higher minor may carry fields this consumer does
    not understand, so the event is surfaced.
    """


class FormatVersionWarning(ContractVersionWarning):
    """The .chiplet-specific spelling of :class:`ContractVersionWarning`.

    Kept as its own class because callers filter on it by name; a subclass so a
    consumer can catch the general case for every governed artifact at once.
    """


def parse_format_version(fv: Any) -> Optional[Tuple[int, int]]:
    """Parse a ``format_version`` value into ``(major, minor)`` or ``None``.

    Public so that a reader built on this one compares stamps with the same
    parse (``chiplet_system_description.source`` does).

    The spec says ``format_version`` MUST be a quoted ``"MAJOR.MINOR"`` string.
    An int/float is coerced through ``str()`` for back-compat (an unquoted
    ``1.0`` in YAML), which is exactly where Python and yaml-cpp can diverge
    (unquoted ``1.10`` becomes ``1.1`` under PyYAML, ``1.10`` under yaml-cpp);
    the divergence is pinned by a conformance fixture, not hidden here.
    """
    if isinstance(fv, bool):
        return None
    if isinstance(fv, (int, float)):
        fv = str(fv)
    if not isinstance(fv, str):
        return None
    parts = fv.split(".")
    if len(parts) != 2:
        return None
    # ASCII digits only, checked before int(). int() is far more permissive than
    # the spec: it accepts a sign, underscore separators, surrounding whitespace
    # and non-ASCII digits, so "+1.0", "1.0_0", "1. 0", " 1.0 " and "1.\u0660"
    # all parsed as (1, 0) here. The C++ reference never did (parse_version_parts
    # requires all_digits), so those five were a READER PARITY divergence, not a
    # tolerance anyone chose: the same document loaded in Python and threw in
    # C++. Digits-first makes this reader agree with the C++ one and with the
    # schema pattern, which were already in agreement.
    if not all(part.isascii() and part.isdigit() for part in parts):
        return None
    major, minor = int(parts[0]), int(parts[1])
    if major < 0 or minor < 0:
        return None
    return (major, minor)



def below_minor(version: Any, since: str) -> bool:
    """Whether ``version`` is an earlier minor of the major ``since`` names."""
    have, need = parse_format_version(version), parse_format_version(since)
    return have is not None and need is not None and have[0] == need[0] \
        and have[1] < need[1]


def unplaced_refusal(what: str, version: str) -> str:
    """The sentence refusing ``what``, a thing a source leaves to placement, in a
    source stamped ``version`` below :data:`UNPLACED_SINCE`."""
    return (f"{what} is format {UNPLACED_SINCE}; this source is stamped {version!r}, "
            f"so stamp it \"{UNPLACED_SINCE}\" (docs/VERSION_POLICY.md, \"What bumps "
            f"what\")")


def placed_by_both(who: str) -> str:
    """``who`` has a rotation and no ``at``. A die is placed by both or neither."""
    return (f"{who} has a rotation and no at; a die is placed by both, "
            f"so write both or neither")


def unplaced_names_profile() -> str:
    """``unplaced`` needs the external block whose edges its pins go on."""
    return ("unplaced: its pins go on the carrier's edges, so the system has an "
            "external block naming the profile")


def _accepted_map(supported: Any, name: str,
                  parse: Callable[[Any], Optional[Tuple[int, int]]]
                  ) -> Dict[int, Tuple[int, str]]:
    """Normalize a declared acceptance to ``{major: (minor floor, spelling)}``.

    ``supported`` is a single ``"MAJOR.MINOR"`` string (the one-element set) or a
    sequence of them, one per major the consumer accepts, each carrying the minor
    it was written for. The spelling is kept so a message can quote the consumer's
    own words back.

    Two entries with the same major are a PROGRAMMING error, not a data error:
    the consumer has declared two floors for one major and there is no verdict to
    give. It is refused at call time with :class:`ValueError`, so it fails on the
    consumer's first call rather than on whatever document happens to arrive; the
    same reason a malformed ``supported`` has always been a ValueError here.
    """
    if isinstance(supported, str):
        entries = [supported]
    elif isinstance(supported, _Sequence) and not isinstance(
            supported, (bytes, bytearray)):
        entries = list(supported)
    else:
        raise ValueError(
            f"supported version {supported!r} for {name} is not a "
            f'"MAJOR.MINOR" string or a sequence of them')
    if not entries:
        raise ValueError(
            f"{name} declares no accepted version; a consumer that accepts "
            f"nothing can read nothing")
    out: Dict[int, Tuple[int, str]] = {}
    for item in entries:
        parsed = parse(item) if isinstance(item, str) else None
        if parsed is None:
            raise ValueError(
                f"supported version {item!r} for {name} is not a "
                f'"MAJOR.MINOR" string')
        major, minor = parsed
        if major in out:
            raise ValueError(
                f"{name} declares two accepted versions with major {major} "
                f"({out[major][1]!r} and {item!r}); the set carries one "
                f"MAJOR.MINOR floor per major")
        out[major] = (minor, item)
    return out


def accepted_minors() -> Dict[int, Tuple[int, str]]:
    """The accepted-minor map of :data:`ACCEPTED_FORMAT_VERSIONS`.

    ``{major: (minor, spelling)}``: the highest minor this reader implements
    for each major it accepts, in the spelling the set declares. Callers that
    need the floor stop re-deriving it from the tuple.
    """
    return _accepted_map(ACCEPTED_FORMAT_VERSIONS, "format_version",
                         parse_format_version)


def _accepted_phrase(accepted: Dict[int, Tuple[int, str]]) -> str:
    """"major 1 (e.g. '1.0')", or every accepted major when there is more than one.

    A refusal that named only one accepted major while the consumer accepted two
    would send the producer to fix the wrong end of a transition window, so the
    refusal names them all.
    """
    majors = sorted(accepted)
    spellings = [accepted[m][1] for m in majors]
    if len(majors) == 1:
        return f"major {majors[0]} (e.g. {spellings[0]!r})"
    return (f"majors {', '.join(str(m) for m in majors)} "
            f"(e.g. {', '.join(repr(s) for s in spellings)})")

#: Module-private warn-once state, keyed by the raw "MAJOR.MINOR" that fired.
#: Resettable so a test suite does not leak dedup state across cases.
_WARNED_VERSIONS: set = set()


def _reset_version_warnings() -> None:
    """Clear the warn-once dedup set (test hook)."""
    _WARNED_VERSIONS.clear()


def check_format_version(fv: Any, *,
                         on_warn: Optional[Callable[[str], None]] = None) -> str:
    """Apply the tolerant ``format_version`` policy; return the normalized version.

    Policy: missing or malformed -> :class:`ChipletFormatError`; a major that is
    not in :data:`ACCEPTED_FORMAT_VERSIONS` (higher OR lower) ->
    :class:`ChipletFormatError`, naming every accepted major; an accepted major
    with a minor at or below that major's entry -> accept silently; an accepted
    major with a higher minor -> accept, warn once per distinct version via
    ``warnings.warn`` AND deliver every event (undeduped) to ``on_warn`` when
    supplied. PATCH does not arise here: a ``.chiplet`` ``format_version`` is
    MAJOR.MINOR only, and the schema pins it that way.

    The accepted SET is what makes a major transition shippable in steps
    (docs/VERSION_POLICY.md, "Changing the major"): while the window is open this
    reader accepts both majors, and a second entry means the code path for that
    major exists here.
    """
    if fv is None:
        raise ChipletFormatError("missing required key: format_version")
    parsed = parse_format_version(fv)
    if parsed is None:
        raise ChipletFormatError(
            f"malformed format_version {fv!r}; expected a quoted "
            f'"MAJOR.MINOR" string')
    major, minor = parsed
    accepted = _accepted_map(ACCEPTED_FORMAT_VERSIONS, "format_version",
                             parse_format_version)
    if major not in accepted:
        raise ChipletFormatError(
            f"unsupported format_version {fv!r}; this reader supports "
            f"{_accepted_phrase(accepted)}")
    floor_minor, floor = accepted[major]
    normalized = f"{major}.{minor}"
    if minor > floor_minor:
        msg = (
            f"format_version {fv!r} is newer than this reader's "
            f"{floor!r} (same major {major}); reading it as "
            f"{floor!r} and ignoring unknown additions")
        if on_warn is not None:
            on_warn(msg)
        if normalized not in _WARNED_VERSIONS:
            # Record the key only AFTER the warning has been delivered. With the
            # add first, a host that escalates warnings to errors saw the first
            # read raise and every later read succeed silently: the same document
            # accepted or refused depending on who read it first. Now an
            # escalating host refuses every time, consistently, and a normal host
            # still sees the warning once.
            warnings.warn(msg, FormatVersionWarning, stacklevel=2)
            _WARNED_VERSIONS.add(normalized)
    return normalized


def _parse_contract_version(value: Any) -> Optional[Tuple[int, int]]:
    """Parse a governed-sidecar version into ``(major, minor)``; ``None`` if not.

    Accepts a quoted ``"MAJOR.MINOR"`` or ``"MAJOR.MINOR.PATCH"`` string. PATCH is
    parsed only to be discarded: a patch never changes what a consumer may
    assume, so it must never change a verdict either. Unlike
    :func:`parse_format_version` (the ``.chiplet`` ``format_version``, which is
    MAJOR.MINOR only and is pinned that way by schemas/chiplet.schema.json), a
    non-string is NOT coerced: the sidecars are JSON, where an unquoted 1.10 is
    the number 1.1 with no way back, so a bare number is malformed here.
    """
    if not isinstance(value, str):
        return None
    # No .strip(): the sidecar schemas' version patterns admit no surrounding
    # whitespace, and a reader must not accept what the schema refuses (the
    # earlier exemption, "surrounding whitespace is not a version change", made
    # this parser laxer than both the schema and parse_format_version).
    parts = value.split(".")
    if len(parts) not in (2, 3):
        return None
    # ASCII digits only, for the same reason parse_format_version checks them: int()
    # accepts a sign, underscores, surrounding whitespace and non-ASCII digits,
    # none of which the shared version pattern in every governed sidecar schema
    # allows. This path is the one every sidecar goes through, so a value the
    # schema rejects must not be a value this reader accepts.
    if not all(part.isascii() and part.isdigit() for part in parts):
        return None
    numbers = [int(p) for p in parts]
    if any(n < 0 for n in numbers):
        return None
    return (numbers[0], numbers[1])


def check_contract_version(value: Any,
                           supported: Union[str, "_Sequence[str]"], *, name: str,
                           on_warn: Optional[Callable[[str], None]] = None) -> str:
    """Apply the version policy to any governed artifact; return "MAJOR.MINOR".

    One rule for every governed sidecar (docs/VERSION_POLICY.md), the same one
    :func:`check_format_version` applies to a ``.chiplet``: a quoted
    ``MAJOR.MINOR`` or ``MAJOR.MINOR.PATCH`` string; an accepted major with a
    minor at or below that major's floor accepted silently; an accepted major
    with a HIGHER minor accepted with a warning (the artifact may carry additions
    this consumer does not understand); a major the consumer does not accept, a
    missing value or a malformed one refused with :class:`ContractVersionError`;
    PATCH ignored throughout.

    ``supported`` is the consumer's ACCEPTANCE: a single ``"MAJOR.MINOR"`` string,
    which is the one-element set, or a sequence with one entry per major it
    accepts, each carrying the minor it was written for. More than one entry is
    what an open major transition looks like from the consumer's side; two
    entries with the same major are a programming error and are refused at call
    time with :class:`ValueError`, never at read time. A refusal names every
    accepted major, so a producer is told the whole window and not half of it.

    ``name`` identifies the artifact in messages (e.g. ``"io_pads.json"``), and is
    part of the warn-once key so two sidecars never suppress each other's warning.
    ``on_warn`` receives every event undeduped; the default ``warnings`` channel is
    deduped per (name, version), matching :func:`check_format_version`.

    The point is that a consumer gates on the CONTRACT, not on byte identity with
    a vendored copy: an emitter that ships a compatible minor keeps working, and
    an unaccepted major fails loudly at the boundary instead of half-parsing.
    """
    accepted = _accepted_map(supported, name, _parse_contract_version)
    if value is None:
        raise ContractVersionError(f"{name}: missing required key: version")
    parsed = _parse_contract_version(value)
    if parsed is None:
        raise ContractVersionError(
            f"{name}: malformed version {value!r}; expected a quoted "
            f'"MAJOR.MINOR" or "MAJOR.MINOR.PATCH" string')
    major, minor = parsed
    if major not in accepted:
        raise ContractVersionError(
            f"{name}: unsupported version {value!r}; this consumer supports "
            f"{_accepted_phrase(accepted)}")
    floor_minor, floor = accepted[major]
    normalized = f"{major}.{minor}"
    if minor > floor_minor:
        msg = (
            f"{name}: version {value!r} is newer than the supported "
            f"{floor!r} (same major {major}); reading it as {floor!r} "
            f"and ignoring unknown additions")
        if on_warn is not None:
            on_warn(msg)
        key = (name, normalized)
        if key not in _WARNED_VERSIONS:
            # Same ordering rule as check_format_version: deliver, then record.
            warnings.warn(msg, ContractVersionWarning, stacklevel=2)
            _WARNED_VERSIONS.add(key)
    return normalized


def check_member_since(declared: Any, field: str, member: Any,
                       since: Optional[Dict[str, Dict[str, str]]] = None) -> None:
    """Refuse ``member`` of vocabulary ``field`` in a document stamped ``declared``
    below the minor that added it.

    ``since`` defaults to :data:`MEMBER_SINCE`; conformance passes its own, so the
    shared oracle (conformance/fixtures/member_since_cases.json) can exercise the
    rule while the real map is still small. A member with no entry is since 1.0
    and always passes, and so does a non-string, which other rules judge. A
    different major passes too: the version policy has already decided whether
    that major is read at all.
    """
    if not isinstance(member, str):
        return
    floor = (MEMBER_SINCE if since is None else since).get(field, {}).get(member)
    if floor is None:
        return
    need = parse_format_version(floor)
    have = parse_format_version(declared)
    if need is None or have is None or need[0] != have[0] or have[1] >= need[1]:
        return
    raise ChipletFormatError(
        f"{field} {member!r} is format {floor}; this document is stamped "
        f"{declared!r}, and a member may not be written below the minor that "
        f"adds it (docs/VERSION_POLICY.md, \"What bumps what\")")


def _vocabulary_members(data: Any):
    """Yield ``(field, member)`` for every closed-vocabulary value in ``data``.

    Field names are the keys of :data:`MEMBER_SINCE`. Malformed containers are
    skipped here; the structural rules refuse them.
    """
    if not isinstance(data, dict):
        return
    assembly = data.get("assembly")
    if isinstance(assembly, dict) and "units" in assembly:
        yield "assembly.units", assembly["units"]
    ifaces = data.get("interfaces")
    if isinstance(ifaces, list):
        for iface in ifaces:
            if isinstance(iface, dict) and "type" in iface:
                yield "interfaces[].type", iface["type"]
    comps = data.get("components")
    if isinstance(comps, list):
        for comp in comps:
            if not isinstance(comp, dict):
                continue
            for key in ("anchor", "orientation"):
                if key in comp:
                    yield f"components[].{key}", comp[key]
            pads = comp.get("io_pads")
            if isinstance(pads, list):
                for pad in pads:
                    if isinstance(pad, dict) and "io_class" in pad:
                        yield "io_pads[].io_class", pad["io_class"]


def _source_keys(data: Any):
    """Yield the path of every :data:`SOURCE_KEY_SINCE` key ``data`` writes,
    in the map's order. Each path is a root key of a source or
    ``system.<list>[].<key>``, a key of any item of that list.

    Malformed containers are skipped here; the structural rules refuse them.
    """
    system = data.get("system") if isinstance(data, dict) else None
    if not isinstance(system, dict):
        return
    for path in SOURCE_KEY_SINCE:
        if "." not in path:
            if path in data:
                yield path
            continue
        items, key = path[len("system."):].split("[].")
        found = system.get(items)
        if isinstance(found, list) and any(isinstance(item, dict) and key in item
                                           for item in found):
            yield path


def _left_to_placement(data: Any):
    """Yield what a source leaves to placement (:data:`UNPLACED_SINCE`), each
    as a refusal names it. Nothing for an assembly; malformed containers are
    skipped here, the structural rules refuse them."""
    system = data.get("system") if isinstance(data, dict) else None
    if not isinstance(system, dict):
        return
    dies = system.get("dies", [])
    if isinstance(dies, list) and not dies:
        yield "system: a source with no dies"
    for i, die in enumerate(dies if isinstance(dies, list) else ()):
        if isinstance(die, dict) and "at" not in die:
            yield f"system.dies[{i}]: {die.get('id') or 'a die'} with no at"
    carrier = system.get("carrier")
    if isinstance(carrier, dict) and "size" not in carrier:
        yield "system.carrier: a carrier with no size"
    external = system.get("external")
    if isinstance(external, dict) and "edges" not in external:
        yield "system.external: an external block with no edges"


def content_format_floor(data: Any) -> Optional[str]:
    """The lowest format minor that can carry every member and every source key
    ``data`` uses, and whatever a source leaves to placement.

    ``None`` when none postdates 1.0. A writer stamps at least this, so a
    document it writes never carries a member or a key below the minor that
    adds it; a lossy writer may stamp down only as far as here.
    """
    floors = [MEMBER_SINCE.get(field, {}).get(member)
              for field, member in _vocabulary_members(data) if isinstance(member, str)]
    floors += [SOURCE_KEY_SINCE[path] for path in _source_keys(data)]
    if next(_left_to_placement(data), None) is not None:
        floors.append(UNPLACED_SINCE)
    best = max(filter(None, map(parse_format_version, floors)), default=None)
    return None if best is None else f"{best[0]}.{best[1]}"


def _apply_write_version(out: Dict[str, Any]) -> None:
    """Set ``out['format_version']`` per the passthrough writer rule (H-B crux).

    The stamped version must describe the bytes written. This is a *lossless*
    passthrough writer (it re-emits the whole dict, unknown keys included), so a
    same-major input above the document kind's stamp is PRESERVED; a missing,
    lower or equal version is stamped to the kind's stamp,
    :data:`SOURCE_FORMAT_VERSION` for a document with a ``system`` block and
    :data:`SUPPORTED_FORMAT_VERSION` otherwise, or to the content's floor
    (:func:`content_format_floor`) when that is higher. Warning re-emission is
    done by the validate pass in :func:`dumps`, not here. (Lossy writers, the C++
    struct writer and the from-scratch KiCad exporter, stamp SUPPORTED instead,
    raised to the same content floor.)
    """
    stamp = SOURCE_FORMAT_VERSION if "system" in out else SUPPORTED_FORMAT_VERSION
    content = content_format_floor(out)
    if content is not None and parse_format_version(content) > parse_format_version(stamp):
        stamp = content
    floor = parse_format_version(stamp)
    parsed = parse_format_version(out.get("format_version"))
    if parsed is not None and floor is not None and parsed[0] == floor[0] \
            and parsed[1] > floor[1]:
        out["format_version"] = f"{parsed[0]}.{parsed[1]}"
    else:
        out["format_version"] = stamp


def _check_line_breaks(text: str) -> None:
    """Refuse a document whose line breaks are not LF or CRLF, before any parse.

    The format's line breaks are LF and CRLF (docs/CHIPLET_FORMAT_SPEC.md, "Line
    breaks"). A character a YAML parser breaks a line on and this grammar does
    not is ill-formed, because the grammar is what the repeated-key scan,
    flow rule 4 and every splitting host read: the parser sees a top-level key
    none of them can. Four characters qualify, and they are DERIVED rather than
    listed (conformance/test_top_level_blocks.py runs PyYAML over a code-point
    range and asserts this reader refuses exactly what it finds): NEL, LS and PS
    anywhere, and CR unless an LF follows it immediately.

    This runs on the TEXT, ahead of ``yaml.safe_load``, for the same reason the
    repeated-key scan does: once a parser has been over the bytes the evidence is
    gone, and here the two reference parsers do not even agree on what the bytes
    are. The refusal names the code point and the line, because all four are
    invisible in an editor and a bare "invalid document" would send the author
    looking at the wrong thing.

    A CR at END OF FILE with no LF after it is refused too. That case is decided
    in the spec rather than left to the line splitter, which pops a trailing CR
    whether or not an LF follows and would therefore read one byte less than the
    file holds without saying so.
    """
    line = 1
    last = len(text) - 1
    for index, ch in enumerate(text):
        if ch == "\n":
            line += 1
            continue
        if ch == _CARRIAGE_RETURN:
            if index < last and text[index + 1] == "\n":
                continue
            raise ChipletFormatError(
                f"line {line}: carriage return (U+000D) not followed by LF. A "
                f"document's line breaks are LF and CRLF, so a CR is legal only "
                f"as the first byte of a CRLF, end of file included. PyYAML "
                f"6.0.3 and yaml-cpp 0.8.0 both break a line on a lone CR and "
                f"this grammar does not, so a top-level key written after one is "
                f"invisible to every consumer that splits the text on LF, this "
                f"reader's own repeated-key scan included. Escape it in a "
                f"double-quoted scalar (\\r) "
                f"(docs/CHIPLET_FORMAT_SPEC.md, top-level block grammar).")
        name = _FORBIDDEN_LINE_BREAKS.get(ch)
        if name is None:
            continue
        raise ChipletFormatError(
            f"line {line}: {name} (U+{ord(ch):04X}) inside a scalar. A "
            f"document's line breaks are LF and CRLF. A YAML 1.1 parser "
            f"(PyYAML) treats NEL (U+0085), LS (U+2028) and PS (U+2029) as "
            f"line breaks and a YAML 1.2 parser (yaml-cpp) does not, so the "
            f"same bytes are two different documents and neither reading is "
            f"conforming. Escape it in a double-quoted scalar (\\N, \\L, \\P) "
            f"(docs/CHIPLET_FORMAT_SPEC.md, top-level block grammar).")


def _refuse_source(data: Any, allow_source: bool) -> None:
    """Refuse a source document unless the caller reads sources.

    Runs whatever ``allow_intermediate`` and ``validate`` say. A source carries
    ``_metadata.finalize_required``, but the intermediate guard alone does not
    hold it: callers that pass ``allow_intermediate=True`` for their own
    finalizer would read a source as an assembly with no components.
    """
    if allow_source or not isinstance(data, dict) or "system" not in data:
        return
    raise SourceDocumentError(
        "this is a source .chiplet (it has a top-level 'system' block), not an "
        "assembly; elaborate it first (packaging-flow run, or "
        "chiplet_system_description), or read it with "
        "chiplet_system_description.source, which passes allow_source=True")


def _validate_source(data: Dict[str, Any], version: str) -> None:
    """The document level of a source (spec, "System"): its stamp, its marker,
    no derived block, and a ``system`` that is a mapping. The block itself is
    :func:`_validate_system`'s, run last.

    The system block's full rules (port coverage, ranges, line grammar) belong
    to its own reader; these two are the part every reader of the bytes agrees on.
    A rail's routing requirement is the one range this reader holds too
    (:func:`_validate_rail_requirement`): a router reads it as written.
    """
    parsed = parse_format_version(version)
    floor = parse_format_version(SOURCE_FORMAT_VERSION)
    if parsed is None or floor is None or parsed < floor:
        raise ChipletFormatError(
            f"the 'system' block is format {SOURCE_FORMAT_VERSION}; this document "
            f"is stamped {version!r}")
    meta = data.get("_metadata")
    if not isinstance(meta, dict) or meta.get("finalize_required") is not True:
        raise ChipletFormatError(
            "a source .chiplet carries _metadata.finalize_required: true; it is "
            "never an assembly, so it is always waiting for its finalizer")
    derived = [key for key in DERIVED_BLOCKS if key in data]
    if derived:
        raise ChipletFormatError(
            f"a source .chiplet does not carry {', '.join(derived)}: "
            f"{'they are' if len(derived) > 1 else 'it is'} derived from the "
            f"'system' block into the assembly")
    if not isinstance(data["system"], dict):
        raise ChipletFormatError("'system' must be a mapping")


def _validate_system(data: Dict[str, Any], version: str) -> None:
    """A source's ``system`` block and its root ``unplaced``, raising
    :class:`SystemBlockError`; run last, once the rest of the document holds.

    A rule that holds at every minor comes before what the stamp allows, so the
    reason given is one that fixes the file: a rotation without ``at`` is not
    fixed by stamping 1.3. Then what a stamp allows a source to leave out: from
    1.3 its placement (spec, "Format 1.3: the unplaced source"), refused below.
    """
    system = data["system"]
    if not isinstance(system.get("carrier"), dict):
        raise SystemBlockError("system.carrier is required and must be a mapping")
    # Absent or empty only from 1.3, which the stamp check below holds.
    dies = system.get("dies", [])
    if not isinstance(dies, list):
        raise SystemBlockError("system.dies must be a list")
    for i, die in enumerate(dies):
        if not isinstance(die, dict):
            raise SystemBlockError(f"system.dies[{i}] must be a mapping")
        if not die.get("id"):
            raise SystemBlockError(f"system.dies[{i}] missing required 'id'")
        if "rotation" in die and "at" not in die:
            raise SystemBlockError(placed_by_both(f"system.dies[{i}]: {die['id']}"))
        if "admission" in die and not _is_admission_path(die["admission"]):
            raise SystemBlockError(
                f"system.dies[{i}]: {die['id']}: admission names a .json file by a path "
                f"relative to the source: no leading '/', no variable, no '..' segment")
    rails = system.get("rails")
    for i, rail in enumerate(rails if isinstance(rails, list) else ()):
        if isinstance(rail, dict):
            _validate_rail_requirement(rail, f"system.rails[{i}]: {rail.get('name') or 'a rail'}")
    for path in _source_keys(data):
        if below_minor(version, SOURCE_KEY_SINCE[path]):
            raise SystemBlockError(
                f"{path} is format {SOURCE_KEY_SINCE[path]}; this source is stamped "
                f"{version!r}, and a key may not be written below the minor that adds "
                f"it (docs/VERSION_POLICY.md, \"What bumps what\")")
    left = next(_left_to_placement(data), None)
    if left is not None and below_minor(version, UNPLACED_SINCE):
        raise SystemBlockError(unplaced_refusal(left, version))
    if "unplaced" in data:
        _validate_unplaced(data["unplaced"], system)


def _is_admission_path(value: Any) -> bool:
    """Whether ``value`` can name a die's admission file (format 1.4): the
    schema's rule, a ``.json`` path relative to the source with no leading
    ``/``, no variable and no ``..`` segment. A source reader may hold paths
    stricter (the description refuses backslashes and drive letters); what the
    file holds is that reader's to check."""
    return (isinstance(value, str) and value.endswith(".json") and not value.startswith("/")
            and "${" not in value and ".." not in value.split("/"))


def _validate_rail_requirement(rail: Dict[str, Any], where: str) -> None:
    """A rail's routing requirement (format 1.5): ``width_um``, or ``amps``
    with ``current_density_ma_per_um``, never both forms, each a positive finite
    number. These are the refusals of interposer-pnr's net_requirements rows, so
    a rail the source refuses is one the router refuses for the same reason.
    This is the schema's rule plus finiteness, which a schema cannot state; the
    stamp check comes after."""
    has_width, has_amps, has_density = (key in rail for key in _RAIL_REQUIREMENT_KEYS)
    if has_width and (has_amps or has_density):
        raise SystemBlockError(
            f"{where} states width_um and a current; a rail states one or the other")
    if has_amps != has_density:
        stated, missing = (("amps", "current_density_ma_per_um") if has_amps
                           else ("current_density_ma_per_um", "amps"))
        raise SystemBlockError(
            f"{where} states {stated} without {missing}; a width is computed from both")
    for key in _RAIL_REQUIREMENT_KEYS:
        if key in rail and not _positive_finite(rail[key]):
            raise SystemBlockError(
                f"{where}: {key} must be a positive, finite number, not {rail[key]!r}")


def _positive_finite(value: Any) -> bool:
    # bool is an int to Python, and never a width or a current.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:  # an int too large for a float reaches no router
        return False


def _validate_unplaced(unplaced: Any, system: Dict[str, Any]) -> None:
    """The root ``unplaced`` block (format 1.3): the external entries no edge
    carries yet. Its line grammar and what each entry names are the source
    reader's; this is its shape, and that it needs the ``external`` block whose
    edges its entries go on."""
    pins = unplaced.get("pins") if isinstance(unplaced, dict) else None
    if not isinstance(pins, list) or not pins or \
            not all(isinstance(pin, str) and pin for pin in pins):
        raise SystemBlockError(
            "unplaced is a mapping holding pins, a non-empty list of port refs and "
            "rail names")
    if "external" not in system:
        raise SystemBlockError(unplaced_names_profile())


def _validate(data: Dict[str, Any], *, allow_intermediate: bool,
              on_warn: Optional[Callable[[str], None]] = None,
              allow_source: bool = False) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ChipletFormatError("top-level .chiplet document must be a mapping")

    # The source refusal comes first, as it does in loads(): what a document IS
    # decides who may read it, before which version of it this reader accepts.
    _refuse_source(data, allow_source)
    version = check_format_version(data.get("format_version"), on_warn=on_warn)
    source = "system" in data
    if source:
        _validate_source(data, version)
    elif "unplaced" in data:
        raise ChipletFormatError(_UNPLACED_IN_ASSEMBLY)

    meta = data.get("_metadata") or {}
    if isinstance(meta, dict) and meta.get("finalize_required") and not source \
            and not allow_intermediate:
        raise ChipletFormatError(
            "this is an intermediate .chiplet (_metadata.finalize_required: true); "
            "run the finalizer (e.g. hyp_to_gds --update-chiplet-file) before "
            "loading, or pass allow_intermediate=True"
        )

    assembly = data.get("assembly")
    if not isinstance(assembly, dict):
        raise ChipletFormatError("missing or invalid 'assembly' section")
    if not assembly.get("name"):
        raise ChipletFormatError("assembly.name is required")
    if "units" in assembly and assembly["units"] not in KNOWN_UNITS:
        raise ChipletFormatError(_units_refusal(assembly["units"]))

    techs = data.get("technologies")
    if techs is not None and not isinstance(techs, dict):
        raise ChipletFormatError("'technologies' must be a mapping")

    comps = data.get("components")
    if comps is not None:
        if not isinstance(comps, list):
            raise ChipletFormatError("'components' must be a list")
        for i, comp in enumerate(comps):
            if not isinstance(comp, dict):
                raise ChipletFormatError(f"component[{i}] must be a mapping")
            if not comp.get("id"):
                raise ChipletFormatError(f"component[{i}] missing required 'id'")
            if not comp.get("type"):
                raise ChipletFormatError(
                    f"component {comp.get('id')!r} missing required 'type'"
                )

    _validate_interfaces(data.get("interfaces"))
    _validate_pad_usage(data.get("interfaces"), comps)
    for field, member in _vocabulary_members(data):
        check_member_since(version, field, member)
    if source:
        _validate_system(data, version)

    return data


def _units_refusal(units: Any) -> str:
    return (f"assembly.units is {units!r}; every length in a .chiplet is in "
            f"{', '.join(KNOWN_UNITS)} (the default when the key is absent), and "
            f"a reader does not convert, so it refuses another unit rather than "
            f"read the geometry wrong (docs/CHIPLET_FORMAT_SPEC.md, Assembly "
            f"Section)")


def _validate_interfaces(ifaces: Any) -> None:
    """Validation rule 4: an interface has an id and a type. That is all.

    Whether the type is a member of :data:`KNOWN_INTERFACE_TYPES` is NOT decided
    here, and neither reference reader decides it any more. The library carries
    every enum-like field as the string the document wrote, the schema closes
    each vocabulary and binds WRITERS, and a consumer that cannot act on an
    unrecognised member refuses the ELEMENT that carries it. Refusing the
    DOCUMENT is what turns an added enum member from a MINOR into a MAJOR for
    everyone downstream (docs/VERSION_POLICY.md, "What bumps what"), which is why
    it is not this reader's call to make. The unrecognised member is reported on
    the warn channel instead, at parse; see :func:`_note_unknown_vocabulary`.
    """
    if ifaces is None:
        return
    if not isinstance(ifaces, list):
        raise ChipletFormatError("'interfaces' must be a list")
    for i, iface in enumerate(ifaces):
        if not isinstance(iface, dict):
            raise ChipletFormatError(f"interface[{i}] must be a mapping")
        if not iface.get("id"):
            raise ChipletFormatError(f"interface[{i}] missing required 'id'")
        if not iface.get("type"):
            raise ChipletFormatError(
                f"interface {iface['id']!r} missing required 'type'")


def _note_unknown_vocabulary(
        data: Any, on_warn: Optional[Callable[[str], None]]) -> None:
    """Report an unrecognised member of a closed vocabulary on the warn channel.

    Called from :func:`loads` at PARSE, outside the ``validate`` gate, and that
    position is the whole point: a consumer running with ``validate=False`` is
    the one most likely to meet a document from a newer minor, and a note that
    only fires under validation is delivered on the default path and dropped on
    the path an orchestrator actually takes.

    ``on_warn`` receives every event, undeduplicated: it is the single NORMATIVE
    channel of this reader. Everything else is convenience (see the module
    docstring), and a consumer that wants to count notes per document counts
    these.
    """
    if on_warn is None:
        return
    ifaces = data.get("interfaces") if isinstance(data, dict) else None
    if not isinstance(ifaces, list):
        return
    for iface in ifaces:
        if not isinstance(iface, dict):
            continue
        itype = iface.get("type")
        if isinstance(itype, str) and itype and \
                itype not in KNOWN_INTERFACE_TYPES:
            on_warn(
                f"interface {iface.get('id')!r} has an interface type this "
                f"reader does not know, {itype!r}; it is carried through "
                f"verbatim. Known types are "
                f"{', '.join(KNOWN_INTERFACE_TYPES)} "
                f"(docs/CHIPLET_FORMAT_SPEC.md, validation rule 4)")



def _validate_pad_usage(ifaces: Any, comps: Any) -> None:
    """Validation rule 8: a pad's io_class must allow the interface's type.

    Scope, and it is deliberately narrow. The document binds no pad to an
    interface: an endpoint is ``{component, surface, port_layer}``, and only the
    interposer carries inline ``io_pads``. So the endpoint's PAD SET is the
    inline pads of the endpoint's component whose ``layer`` is the endpoint's
    ``port_layer``, an empty set is vacuous, and an endpoint whose component has
    no inline pads (a die) is not checked at all until an explicit pad binding
    exists. A pad whose io_class is outside the table is not judged
    here either; the schema closes that vocabulary.

    An unrecognised interface TYPE is skipped for exactly the same reason as an
    unrecognised io_class, and the symmetry is the fix: this rule was the third
    place the library refused an unknown type, and the only one that survived
    removing the other two, because a type outside the table matches no allowed
    entry and so read as a violation rather than as a value the rule has nothing
    to say about. Rule 8 relates two CLOSED vocabularies; a member of neither is
    outside its domain, not in breach of it.
    """
    if not isinstance(ifaces, list) or not isinstance(comps, list):
        return
    by_id: Dict[Any, Any] = {}
    for comp in comps:
        if isinstance(comp, dict) and isinstance(comp.get("id"), str):
            by_id.setdefault(comp["id"], comp)
    for iface in ifaces:
        if not isinstance(iface, dict):
            continue
        itype = iface.get("type")
        if itype not in KNOWN_INTERFACE_TYPES:
            continue
        for side in ("from", "to"):
            endpoint = iface.get(side)
            if not isinstance(endpoint, dict):
                continue
            comp = by_id.get(endpoint.get("component"))
            if not isinstance(comp, dict):
                continue
            pads = comp.get("io_pads")
            if not isinstance(pads, list):
                continue
            layer = endpoint.get("port_layer")
            for pad in pads:
                if not isinstance(pad, dict) or pad.get("layer") != layer:
                    continue
                io_class = pad.get("io_class")
                if not isinstance(io_class, str):
                    continue
                allowed = IO_CLASS_INTERFACE_TYPES.get(io_class)
                if allowed is None or itype in allowed:
                    continue
                raise ChipletFormatError(
                    f"interface {iface.get('id')!r} of type {itype!r} meets pad "
                    f"{pad.get('id')!r} (io_class {io_class!r}) on layer "
                    f"{layer!r} of component {comp.get('id')!r}: io_class "
                    f"{io_class!r} allows only {', '.join(allowed)} "
                    f"(docs/CHIPLET_FORMAT_SPEC.md, validation rule 8)")

def validate(data: Dict[str, Any], *, allow_intermediate: bool = False,
             on_warn: Optional[Callable[[str], None]] = None,
             allow_source: bool = False) -> Dict[str, Any]:
    """Validate a parsed .chiplet mapping in place; return it. Raises on error.

    A source document (top-level ``system``) raises :class:`SourceDocumentError`
    unless ``allow_source`` is true; see :func:`loads`.
    """
    return _validate(data, allow_intermediate=allow_intermediate, on_warn=on_warn,
                     allow_source=allow_source)


def loads(text: str, *, allow_intermediate: bool = False, validate: bool = True,
          on_warn: Optional[Callable[[str], None]] = None,
          allow_source: bool = False) -> Dict[str, Any]:
    """Parse a .chiplet document from a YAML string into a dict.

    Refuses a document with a REPEATED top-level key, on the source text, before
    anything is read out of it. That is not a style rule: PyYAML keeps the last
    value and yaml-cpp the first, so this reader and the C++ one would report
    different documents from one file, and nothing downstream, schema included,
    can tell. A line at column zero that no top-level key owns (a quoted key, an
    explicit key, a bare key outside the grammar) makes a document unsplittable,
    not invalid, and is read normally (see :func:`top_level_blocks` and flow
    rule 1).

    Refuses, for the same reason and ahead of the YAML parse rather than after
    it, a document whose line breaks are not LF or CRLF: NEL (U+0085), U+2028 or
    U+2029 anywhere, and a CR not immediately followed by LF. A YAML parser
    breaks a line on each of them and this grammar does not (see
    :func:`_check_line_breaks`). It runs FIRST because on such a document
    ``yaml.safe_load`` either raises with a parser message that says nothing
    about the real defect or, worse, returns a document with a top-level key
    that is not in the file.

    Refuses a SOURCE document, one with a top-level ``system`` block, with
    :class:`SourceDocumentError` unless ``allow_source`` is true, and does so
    whatever ``allow_intermediate`` and ``validate`` say: a source is not an
    assembly, and a caller that asked for one must not get an empty one. Only a
    reader of sources passes ``allow_source=True``.
    """
    _check_line_breaks(text)
    data = yaml.safe_load(text)
    if data is None:
        raise ChipletFormatError("empty .chiplet document")
    # One pass over the text. Cheap, and it is the only place the repeat is
    # visible: `data` above has already resolved it away.
    _scan_top_level(text)
    # At PARSE, deliberately outside the gate below: an unrecognised member of a
    # closed vocabulary is news for every caller, and the caller most likely to
    # meet one is the orchestrator that passed validate=False.
    _note_unknown_vocabulary(data, on_warn)
    # Outside the validate gate, for the reason the note above is: the caller
    # that turned validation off is the one least able to notice an empty
    # assembly.
    _refuse_source(data, allow_source)
    if validate:
        _validate(data, allow_intermediate=allow_intermediate, on_warn=on_warn,
                  allow_source=allow_source)
    return data


def load(path, *, allow_intermediate: bool = False, validate: bool = True,
         on_warn: Optional[Callable[[str], None]] = None,
         allow_source: bool = False) -> Dict[str, Any]:
    """Read and parse a .chiplet file into a dict."""
    with open(path, "r", encoding="utf-8") as fh:
        return loads(fh.read(), allow_intermediate=allow_intermediate,
                     validate=validate, on_warn=on_warn,
                     allow_source=allow_source)


def dumps(data: Dict[str, Any], *, validate: bool = True,
          on_warn: Optional[Callable[[str], None]] = None) -> str:
    """Serialize a .chiplet mapping to a canonical YAML string.

    Key order is preserved (insertion order). This is semantic, not byte-exact
    to the GPL host writers. This is a lossless passthrough writer: the stamped
    ``format_version`` describes the bytes written, so a same-major higher-minor
    input is preserved (and, when validating, re-warns), never silently stamped
    down. See :func:`_apply_write_version`.

    It writes a ``flow`` block from the parsed value, never from source bytes, so
    it does not implement flow rule 4 and never claimed to. The corollary is that
    it has no "the block has no slice" state to refuse on: the C++ reference,
    which does keep the source slice, refuses to write a document whose flow
    block the grammar could not delimit (``FlowSource::NotDelimitable``). A host
    that needs rule 4 keeps the original text beside the parsed document and
    writes the text; this function is not that host.

    Refuses a mapping whose top-level keys this emitter cannot write as KEY
    LINES, which is the writer half of the top-level block grammar. Measured on
    PyYAML 6.0.3, a top-level key carrying a space comes out as a bare ``a b:``
    that the grammar does not recognise, one carrying NEL, LS, PS or a CR comes
    out as an explicit key (``? "a\\Lb"`` on one line, ``: x: 1`` on the next),
    and a key such as ``yes`` or ``1.0`` comes out quoted. All three escape the
    splitter, which then attributes those lines to the PRECEDING key, while
    ``loads`` reads a key the split never saw. See
    :func:`_check_writer_top_level_keys`, which is where the refusal is decided.
    """
    out = dict(data)
    if validate:
        # Writing an intermediate (finalize_required) document is legitimate,
        # and so is writing a source: the refusal is a READER's, for callers that
        # want an assembly. This re-runs check_format_version, so a preserved
        # higher minor re-warns on write (deduped default channel; every event
        # to on_warn).
        _validate(out, allow_intermediate=True, on_warn=on_warn, allow_source=True)
    _apply_write_version(out)
    text = yaml.dump(
        out,
        Dumper=_CanonicalDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    _check_writer_top_level_keys(text, out)
    return text


#: What a scalar may not carry raw on the way OUT: the reader's set with CR
#: added unconditionally. The reader can afford CR's condition because it sees
#: the next byte; a writer cannot, since whether an LF follows the CR it just
#: wrote depends on where in the value it sits, and "it usually does" is not a
#: writer rule.
_ESCAPE_IN_SCALARS = tuple(_FORBIDDEN_LINE_BREAKS) + (_CARRIAGE_RETURN,)


class _CanonicalDumper(yaml.SafeDumper):
    """SafeDumper that never emits a forbidden line break as a raw character.

    ``yaml.safe_dump(allow_unicode=True)`` writes NEL, U+2028 and U+2029 into a
    SINGLE-quoted scalar as raw bytes, and PyYAML then reads its own output back
    as a folded line break: measured, ``{'name': 'demo<LS>x'}`` does not survive
    a dump/load round trip. Forcing the double-quoted style for exactly those
    scalars puts the emitter on the path where it already writes ``\\N``, ``\\L``
    and ``\\P``, so the value round-trips and the bytes on disk are a document
    this reader still accepts.

    CR is in the trigger set too, and is the one member the emitter already gets
    right on its own (measured on PyYAML 6.0.3: ``{'name': 'demo<CR>x'}`` comes
    out as ``"demo\\rx"`` with no help). It is asserted by the same test as the
    other three rather than assumed, because that is a fact about a version.
    """

    #: PyYAML spells U+0085 as ``\\N`` in a double-quoted scalar, and that escape
    #: does not survive the OTHER reference reader. Measured on PyYAML 6.0.3 and
    #: yaml-cpp 0.8.0: ``"a\\Nb"`` reads back as ``61 c2 85 62`` here and as
    #: ``61 85 62`` there, a bare 0x85 that is not valid UTF-8 by itself, so a
    #: document this writer produced hands the C++ reader a malformed string.
    #: Dropping the entry sends the emitter down its hex path and it writes
    #: ``\\x85``, which both readers decode to U+0085. ``\\L`` and ``\\P``
    #: round-trip correctly in both and are deliberately left in place: the
    #: defect is this one escape, not the family, and removing all three would
    #: churn every existing document for no gain.
    ESCAPE_REPLACEMENTS = {
        k: v for k, v in yaml.SafeDumper.ESCAPE_REPLACEMENTS.items()
        if k != "\u0085"
    }


def _represent_str(dumper: yaml.SafeDumper, data: str) -> Any:
    tag = "tag:yaml.org,2002:str"
    if any(ch in data for ch in _ESCAPE_IN_SCALARS):
        return dumper.represent_scalar(tag, data, style='"')
    return dumper.represent_scalar(tag, data)


_CanonicalDumper.add_representer(str, _represent_str)


def dump(data: Dict[str, Any], path, *, validate: bool = True,
         on_warn: Optional[Callable[[str], None]] = None) -> None:
    """Serialize a .chiplet mapping to a file.

    Serialize before opening the destination, so refused input leaves an
    existing file untouched. This does not make filesystem writes atomic.
    """
    text = dumps(data, validate=validate, on_warn=on_warn)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# --- the top-level block grammar -------------------------------------------
#
# Flow rule 4 (docs/CHIPLET_FORMAT_SPEC.md) says a host re-emits a flow block it
# did not author BYTE FOR BYTE. That is a statement about text, and it needs one
# definition of where a block starts and ends, shared by every implementation:
# the merge splitter in the KiCad plugin, this reader, and the C++ reference.
# The definition is normative in the spec under "Top-level block grammar" and
# every implementation is measured against
# conformance/fixtures/top_level_blocks_cases.json, never against another
# implementation.
#
# No YAML is parsed here, on purpose. A parsed node tree cannot answer the
# question: an emitter re-quotes scalars by its own rules (a source '0755' comes
# back bare and is an integer to the next PyYAML reader) and drops comments,
# which in a hand-written flow carry the author's intent.

#: A top-level block starts at a KEY LINE: a bare key at column zero, optionally
#: followed by whitespace and a value or a comment. Anchored with \Z, never $:
#: in Python $ also matches before a trailing newline, so a $-anchored copy of
#: this expression reads a different grammar than an ECMA-262 one does (the
#: defect adapter_id_cases.json was written for). The portable spelling of \Z is
#: (?![\s\S]); an ECMAScript implementation writes [^\n] for the dot, which
#: there excludes CR as well as LF.
_KEY_LINE_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*):(?:\s.*)?\Z")

#: A quoted key at column zero. Valid YAML, NOT a key line, and therefore a
#: document this module refuses to split: a splitter would attach the block to
#: the preceding key, whose owner regenerates it away on the next export. This is
#: the spelling a document-wide key-quoting emitter switch produces, which is why
#: validation rule 7 forbids one.
_QUOTED_KEY_LINE_RE = re.compile(
    r"""^(?:"(?:[^"\\]|\\.)*"|'(?:[^']|'')*'):(?:\s.*)?\Z""")

#: A BLOCK SEQUENCE ENTRY at column zero: ``-`` followed by a space, a tab or
#: nothing at all. It is not a key line, and it is the one non-key line at column
#: zero that IS attributable: PyYAML writes a block sequence under a mapping key
#: at the parent's indentation, so ``components:`` followed by ``- id: ...``
#: lines is what every generated document in this ecosystem looks like (68 such
#: lines across the 77 tracked ``.chiplet`` when the rule below was written). The
#: entry belongs to the key whose block it sits in, and both a splitter and a
#: parser agree that it does.
_SEQUENCE_ENTRY_RE = re.compile(r"^-(?:[ \t].*)?\Z")

#: A document marker, ``---`` or ``...``, alone or introducing a value. Not a key
#: line: it stays in the current block, which is the preamble only before the
#: first key line.
_DOCUMENT_MARKER_RE = re.compile(r"^(?:---|\.\.\.)(?:[ \t].*)?\Z")

#: Key of the PREAMBLE bucket: the lines before the first key line (a leading
#: ``---``, a file header comment). They belong to no top-level key.
PREAMBLE_KEY = ""


def _iter_lines(text: str):
    """Yield each line of ``text`` WITH its terminator, splitting on LF only.

    Not ``str.splitlines()``: that also splits on CR, VT, FF, NEL, U+2028 and
    friends, so a document with one of those inside a quoted scalar grows a
    top-level block that is not in the file. A line ends at LF, full stop.
    """
    start, n = 0, len(text)
    while start < n:
        nl = text.find("\n", start)
        end = n if nl < 0 else nl + 1
        yield text[start:end]
        start = end


def _content(line: str) -> str:
    """The part of a line the grammar matches: no LF, one optional CR removed."""
    if line.endswith("\n"):
        line = line[:-1]
    if line.endswith("\r"):
        line = line[:-1]
    return line


def top_level_key(line_content: str) -> Optional[str]:
    """Return the top-level key ``line_content`` opens, or ``None``.

    ``line_content`` is ONE line with its terminator already removed (that is
    what the oracle's ``key_lines`` cases carry). Deliberately strict: a value
    with a trailing newline is not a line, and silently accepting one is the
    ``$``-anchor defect wearing a different hat.
    """
    match = _KEY_LINE_RE.match(line_content)
    return match.group(1) if match else None


def _is_unattributable(line_content: str) -> bool:
    """Is this line content at column zero with no top-level key that owns it?

    The generalisation of the quoted-key rule, and the property it protects is
    this: the split and the parse must not disagree about the
    document's top-level keys. A line at column zero that is not a key line
    starts no block, so a splitter attaches it to the PRECEDING key, whose owner
    regenerates it away on the next re-export, while a YAML parser reads a
    top-level key the split never saw.

    Column zero is "does not start with a space". A tab is therefore column
    zero, deliberately: this grammar has no notion of tab indentation, and YAML
    has none either in block context (measured on PyYAML 6.0.3 and yaml-cpp
    0.8.0: both refuse a tab-indented mapping outright), so a tab-led line is
    never a line an implementation may quietly attribute to the open block.

    Five shapes at column zero are attributable and are not this:

    * a blank line (only SPACE and TAB), which belongs to its current block;
    * a comment (``#``) and a directive (``%``), on the same terms;
    * a document marker (``---``, ``...``), which stays in the current block
      (the preamble only before the first key line);
    * a BLOCK SEQUENCE ENTRY (``-`` then a space, a tab or end of line), which
      belongs to the key whose block it sits in. This one is not a nicety: it is
      how PyYAML writes every ``components:`` list this ecosystem produces, and
      a rule without it would call almost every real document unsplittable;
    * a key line, which opens a block of its own.
    """
    if not line_content.strip(" \t"):
        return False
    if line_content[0] in " #%":
        return False
    if _DOCUMENT_MARKER_RE.match(line_content):
        return False
    if _SEQUENCE_ENTRY_RE.match(line_content):
        return False
    return top_level_key(line_content) is None


def _scan_top_level(text: str) -> Tuple[Dict[str, str], Optional[str]]:
    """One pass over the source text: the blocks, and why they may be unusable.

    Returns ``(blocks, not_splittable)``. ``not_splittable`` is ``None`` for a
    document that can be split, and otherwise the reason it cannot be, which the
    splitting accessors raise and :func:`loads` ignores. The two are different
    verdicts: a line at column zero that is not a key line and not one of the
    attributable shapes (see :func:`_is_unattributable`) leaves nobody able to
    say who owns those bytes, and that is a reason not to SPLIT or WRITE the
    document, not a reason to refuse to read it.

    Raises :class:`ChipletFormatError` for a REPEATED top-level key, which is a
    different thing again: that document is ill-formed and every caller here,
    :func:`loads` included, refuses it. PyYAML resolves a repeated key to the
    last value and yaml-cpp to the first, so no reading of it is conforming.
    """
    blocks: Dict[str, str] = {PREAMBLE_KEY: ""}
    current = PREAMBLE_KEY
    not_splittable: Optional[str] = None
    for number, line in enumerate(_iter_lines(text), start=1):
        content = _content(line)
        key = top_level_key(content)
        if key is not None:
            if key in blocks:
                raise ChipletFormatError(
                    f"line {number}: repeated top-level key {key!r}. A document "
                    f"names each top-level key once. PyYAML resolves a repeat "
                    f"to the LAST value and yaml-cpp to the FIRST, so two "
                    f"conforming readers read different documents from these "
                    f"bytes, and neither the schema nor a parsed node tree can "
                    f"see that it happened "
                    f"(docs/CHIPLET_FORMAT_SPEC.md, top-level block grammar).")
            current = key
            blocks[key] = ""
        elif not_splittable is None and _is_unattributable(content):
            if _QUOTED_KEY_LINE_RE.match(content):
                not_splittable = (
                    f"line {number}: quoted key at column zero ({content!r}). "
                    f"A quoted key is valid YAML but does not start a "
                    f"top-level block, so this document cannot be split "
                    f"without attaching the block to the preceding key, where "
                    f"its owner drops it on the next re-export. Emit bare keys "
                    f"and quote values "
                    f"(docs/CHIPLET_FORMAT_SPEC.md, top-level block grammar).")
            else:
                not_splittable = (
                    f"line {number}: unattributable line at column zero "
                    f"({content!r}). It is not a key line, and not a comment, "
                    f"a document marker, a directive or a block sequence entry "
                    f"either, so the line grammar cannot establish ownership. "
                    f"Guessing can hide a top-level key or misinterpret a quoted "
                    f"scalar continuation. Two problematic key "
                    f"spellings the Python emitter can produce for a key it cannot "
                    f"write bare are the explicit key (`? ...` with its `: ...` "
                    f"value line) and a bare key outside the grammar (`a b:`). "
                    f"Emit top-level keys as key lines and re-emit quoted scalar "
                    f"continuations with a conforming writer "
                    f"(docs/CHIPLET_FORMAT_SPEC.md, top-level block grammar).")
        blocks[current] += line
    if not blocks[PREAMBLE_KEY]:
        del blocks[PREAMBLE_KEY]
    return blocks, not_splittable


def top_level_blocks(text: str) -> Dict[str, str]:
    """Split a .chiplet document into its top-level blocks, in document order.

    Returns ``{key: exact source text}``. A block runs from its key line up to
    but excluding the next key line, or to end of file, and the text is the
    SOURCE BYTES: key line included, original line endings, no trailing-newline
    normalisation, nothing stripped. Lines before the first key line land under
    :data:`PREAMBLE_KEY`, which is present only when there are any.

    Raises :class:`ChipletFormatError` twice over, for two different reasons:

    * an unattributable line at column zero: a quoted key, an explicit key
      (``? ...``), a bare key outside the grammar (``a b:``), or anything else
      there that is not a key line, a comment, a directive, a document marker or
      a block sequence entry. It starts no block, so splitting would attach
      those bytes to the preceding key and lose them on the next re-export while
      a parser reads a key the split never saw. The document is still valid and
      :func:`loads` still reads it; splitting is what cannot be done.
    * a repeated top-level key. That document is ill-formed and :func:`loads`
      refuses it too.
    """
    blocks, not_splittable = _scan_top_level(text)
    if not_splittable is not None:
        raise ChipletFormatError(not_splittable)
    return blocks


@dataclass(frozen=True)
class CapturedTopLevelBlock:
    """An owned exact source slice; offsets count UTF-8 bytes, half-open."""
    key: str
    byte_start: int
    byte_end: int
    raw: str


@dataclass(frozen=True)
class TopLevelBlockCapture:
    """All-or-nothing source ownership, not schema or overwrite permission."""
    safe: bool
    preamble: str = ""
    blocks: Tuple[CapturedTopLevelBlock, ...] = ()
    reason_code: str = ""
    reason: str = ""
    byte_offset: Optional[int] = None


class _CaptureRefusal(Exception):
    def __init__(self, code: str, reason: str, index: Optional[int] = None):
        self.code, self.reason, self.index = code, reason, index


_CAPTURE_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")
_CAPTURE_RESERVED_KEYS = frozenset(("y", "n", "yes", "no", "true", "false",
                                    "on", "off", "null"))


def _capture_root_events(text: str):
    """Use parser events, including properties invisible in constructed values."""
    ev = yaml.events
    events = list(yaml.parse(text, Loader=yaml.SafeLoader))
    for event in events:
        if isinstance(event, ev.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise _CaptureRefusal("unsupported_property", "Anchors and aliases are not capturable",
                                  event.start_mark.index)
        if getattr(event, "tag", None) is not None:
            raise _CaptureRefusal("unsupported_property", "Explicit tags are not capturable",
                                  event.start_mark.index)
        if isinstance(event, ev.DocumentStartEvent) and (
                event.explicit or event.version is not None or event.tags):
            raise _CaptureRefusal("unsupported_property", "Directives/document starts are not capturable",
                                  event.start_mark.index)
    if (len(events) < 6 or not isinstance(events[0], ev.StreamStartEvent)
            or not isinstance(events[1], ev.DocumentStartEvent)
            or not isinstance(events[2], ev.MappingStartEvent) or events[2].flow_style
            or sum(isinstance(e, ev.DocumentStartEvent) for e in events) != 1):
        raise _CaptureRefusal("yaml_structure", "Capture requires one nonempty block root mapping")

    def skip_node(index):
        if isinstance(events[index], ev.ScalarEvent):
            return index + 1
        if not isinstance(events[index], (ev.MappingStartEvent, ev.SequenceStartEvent)):
            raise _CaptureRefusal("yaml_structure", "Expected a YAML value node")
        depth = 1
        index += 1
        while depth:
            event = events[index]
            if isinstance(event, (ev.MappingStartEvent, ev.SequenceStartEvent)):
                depth += 1
            elif isinstance(event, (ev.MappingEndEvent, ev.SequenceEndEvent)):
                depth -= 1
            index += 1
        return index

    keys, seen = [], set()
    index = 3
    while not isinstance(events[index], ev.MappingEndEvent):
        key = events[index]
        if (not isinstance(key, ev.ScalarEvent) or key.style is not None
                or not _CAPTURE_KEY_RE.fullmatch(key.value)
                or key.value.lower() in _CAPTURE_RESERVED_KEYS
                or key.start_mark.column != 0):
            raise _CaptureRefusal("root_key", "Root keys must be unquoted non-reserved capture identifiers",
                                  key.start_mark.index)
        if key.value in seen:
            raise _CaptureRefusal("root_key", "Duplicate root key: " + key.value, key.start_mark.index)
        seen.add(key.value)
        keys.append(key)
        index = skip_node(index + 1)
    if not keys:
        raise _CaptureRefusal("yaml_structure", "An empty mapping has no capturable root blocks")
    return keys


def capture_top_level_blocks(text: str) -> TopLevelBlockCapture:
    """Attest exact movable root slices against parser structure (additive API).

    ``text`` is one immutable strict UTF-8 string; no file I/O or normalization
    occurs. Hosts must give the same buffer to ``loads``. Valid capture does not
    imply valid .chiplet schema/version/lifecycle, or general nested-field
    preservation. Anchors, aliases, explicit tags and directives are unsupported.
    Existing loading, writing and lexical key APIs retain their own contracts.
    """
    if not isinstance(text, str):
        raise TypeError("capture_top_level_blocks requires str")
    try:
        encoded = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        return TopLevelBlockCapture(False, reason_code="encoding", reason=str(exc))
    offsets = [0]
    for char in text:
        offsets.append(offsets[-1] + len(char.encode("utf-8")))
    try:
        if text.startswith("\ufeff"):
            raise _CaptureRefusal("encoding", "UTF-8 BOM is unsupported", 0)
        try:
            _check_line_breaks(text)
        except ChipletFormatError as exc:
            raise _CaptureRefusal("line_break", str(exc)) from exc
        position = 0
        for line in _iter_lines(text):
            if _DOCUMENT_MARKER_RE.fullmatch(_content(line)):
                raise _CaptureRefusal("boundary_line", "Column-zero document-marker-shaped line is unsupported",
                                      position)
            position += len(line)
        try:
            lexical, unsafe = _scan_top_level(text)
        except ChipletFormatError as exc:
            raise _CaptureRefusal("lexical_boundary", str(exc)) from exc
        if unsafe:
            raise _CaptureRefusal("lexical_boundary", unsafe)
        preamble = lexical.get(PREAMBLE_KEY, "")
        # A conservative shared capture profile: the parsers disagree on tabs
        # in pre-root indentation. Comment contents and values remain untouched.
        preamble_offset = 0
        for line in _iter_lines(preamble):
            content = _content(line)
            indentation = len(content) - len(content.lstrip(" \t"))
            tab = content.find("\t", 0, indentation)
            if tab != -1:
                raise _CaptureRefusal("preamble_tab", "Tabs in pre-root indentation are unsupported by capture",
                                      preamble_offset + tab)
            preamble_offset += len(line)
        if any(_content(line).strip(" \t") and not _content(line).lstrip(" \t").startswith("#")
               for line in _iter_lines(preamble)):
            raise _CaptureRefusal("lexical_boundary", "Preamble may contain only blank lines and comments")
        parsed = _capture_root_events(text)
        pieces = [(key, raw) for key, raw in lexical.items() if key != PREAMBLE_KEY]
        if [key for key, _ in pieces] != [event.value for event in parsed]:
            raise _CaptureRefusal("lexical_boundary", "Parser root identities/order disagree with lexical slices")
        blocks = []
        char_start = len(preamble)
        for (key, raw), event in zip(pieces, parsed):
            if (event.start_mark.index != char_start or event.start_mark.column != 0
                    or event.start_mark.line != text.count("\n", 0, char_start)):
                raise _CaptureRefusal("lexical_boundary", "Parser root start disagrees with slice boundary",
                                      event.start_mark.index)
            try:
                independent = _capture_root_events(raw)
            except _CaptureRefusal as exc:
                raise _CaptureRefusal("slice_boundary", exc.reason,
                                      None if exc.index is None else char_start + exc.index) from exc
            except yaml.YAMLError as exc:
                mark = getattr(exc, "problem_mark", None)
                raise _CaptureRefusal("slice_boundary", str(exc),
                                      None if mark is None else char_start + mark.index) from exc
            if len(independent) != 1 or independent[0].value != key or independent[0].start_mark.index != 0:
                raise _CaptureRefusal("slice_boundary", "Slice does not independently parse as its one root entry",
                                      char_start)
            end = char_start + len(raw)
            blocks.append(CapturedTopLevelBlock(key, offsets[char_start], offsets[end], raw))
            char_start = end
        if (char_start != len(text) or preamble + "".join(b.raw for b in blocks) != text
                or offsets[-1] != len(encoded)):
            raise _CaptureRefusal("slice_boundary", "Slices do not reconstruct the original buffer")
        return TopLevelBlockCapture(True, preamble, tuple(blocks))
    except _CaptureRefusal as exc:
        return TopLevelBlockCapture(False, reason_code=exc.code, reason=exc.reason,
                                    byte_offset=None if exc.index is None else offsets[exc.index])
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        return TopLevelBlockCapture(False, reason_code="yaml_structure", reason=str(exc),
                                    byte_offset=None if mark is None else offsets[mark.index])


@dataclass(frozen=True)
class TopLevelKeys:
    """The top-level block grammar's key sequence, as a public verdict.

    ``keys`` are the top-level keys in document order, :data:`PREAMBLE_KEY`
    EXCLUDED (lines before the first key line own no key); meaningful only while
    ``not_splittable`` is ``None``. ``not_splittable`` is ``None`` when the
    document splits and otherwise the splitter's own reason it cannot be (a
    quoted key at column zero, an unattributable line at column zero); then
    ``keys`` is empty and MUST NOT be read. A host treats a non-``None`` value as
    a refusal, never as "no keys" and never as success. The C++ reference spells
    the same two fields with an empty string for "splittable".
    """
    keys: Tuple[str, ...]
    not_splittable: Optional[str]

    @property
    def splittable(self) -> bool:
        return self.not_splittable is None


def split_top_level_keys(text: str) -> TopLevelKeys:
    """Split ``text`` with the reader's own grammar and report the key sequence.

    A host that writes a ``.chiplet`` on its own needs the question
    :func:`dumps` asks of its own output: does the text it is about to write
    split into exactly the top-level keys it meant to write, in order? This is a
    THIN WRAPPER over the one splitter in this module, the one :func:`loads` and
    :func:`dumps` run; there is no second grammar, and the block texts stay
    private (:func:`top_level_blocks` is the accessor for those).

    Raises :class:`ChipletFormatError` for a REPEATED top-level key, exactly as
    :func:`loads` does: that document is ill-formed and there is no sequence to
    hand back. Every other verdict is in the returned :class:`TopLevelKeys`.

    What this is NOT. It is not a loader and makes no semantic claim: a document
    carrying a forbidden line break splits perfectly well here and is refused by
    :func:`loads`; a document that is not YAML at all may split. And the split
    alone does not validate an emission: a key-shaped continuation line inside a
    multi-line quoted scalar is a key line to this grammar and a scalar to the
    parser, so ``keys`` can name a key the parser never sees. The host compares
    ``keys`` against the list it INTENDED to write, obtained independently, and
    refuses on any difference or order change; agreement is the property, not a
    non-empty result.
    """
    blocks, not_splittable = _scan_top_level(text)
    if not_splittable is not None:
        return TopLevelKeys(keys=(), not_splittable=not_splittable)
    return TopLevelKeys(
        keys=tuple(key for key in blocks if key != PREAMBLE_KEY),
        not_splittable=None)


def top_level_block(text: str, key: str) -> Optional[str]:
    """Return the exact source text of one top-level block, or ``None``.

    Convenience over :func:`top_level_blocks` with the same guarantees, and the
    call flow rule 4 needs: ``top_level_block(text, "flow")`` is the block a host
    that did not author it re-emits unchanged.
    """
    return top_level_blocks(text).get(key)


def _check_writer_top_level_keys(text: str, data: Dict[str, Any]) -> None:
    """Refuse output whose top-level keys the split cannot recover, in order.

    The writer half of the grammar, and it is a POST-check on the emitted text
    rather than a regex on the keys, deliberately. A pre-check would have to
    predict the emitter, and the emitter has three ways of writing a key that is
    not a bare identifier, only one of which a key regex would catch: it quotes
    ``yes``, ``null`` and ``1.0`` (which pass the key-line expression happily and
    still come out as a quoted key), it writes an explicit key ``? ...`` with a
    separate ``: ...`` value line for a key carrying a forbidden line break, and
    it writes a key with a space bare, as ``a b:``, which is outside the grammar.
    Running the reader's own splitter over the finished bytes catches all three
    with one mechanism and cannot drift from the reader, because it IS the
    reader.

    What it refuses is exactly the split-parse property, produced by our own writer:
    a document whose split and whose parse disagree about the top-level keys.
    """
    blocks, not_splittable = _scan_top_level(text)
    split_keys = [key for key in blocks if key != PREAMBLE_KEY]
    expected = list(data)
    if not_splittable is None and split_keys == expected:
        return

    # The FIRST key the split did not see, in the order they were written. A
    # position check and not a membership test: the split can also recover a key
    # under a different NAME (an integer key 1 comes back as the string "1"),
    # and that is the same defect one step further along.
    missed, missed_at, missed_found = None, 0, False
    for position, key in enumerate(expected):
        if position >= len(split_keys) or split_keys[position] != key:
            missed, missed_at = key, position
            missed_found = True
            break

    # And the line the emitter wrote for it. Every top-level key produces
    # exactly one line at column zero that either opens a block or breaks the
    # grammar, in document order, so the missed key's line stands at its own
    # position in that list.
    interesting = [content for content in
                   (_content(raw) for raw in _iter_lines(text))
                   if _is_unattributable(content)
                   or top_level_key(content) is not None]
    line = interesting[missed_at] if missed_at < len(interesting) else ""
    if missed_found and not isinstance(missed, str):
        recovered = top_level_key(line)
        consequence = (
            f"The block reader reads it back as the string key {recovered!r}, "
            f"not the original {type(missed).__name__} key. "
            if recovered is not None else
            "The block reader can recover only string keys, and this emitted "
            "line does not open a key block. ")
        raise ChipletFormatError(
            f"top-level key {missed!r} is not a string: the emitter wrote "
            f"{line!r}. {consequence}Make it a string key that the emitter "
            f"can write as a key line, or nest it under one "
            f"(docs/CHIPLET_FORMAT_SPEC.md, top-level block grammar).")
    raise ChipletFormatError(
        f"top-level key {missed!r} cannot be written as a key line: the "
        f"emitter wrote {line!r}. A top-level key is a bare identifier "
        f"[A-Za-z0-9_][A-Za-z0-9_.-]* that the emitter writes as a key line at "
        f"column zero; nested keys are unrestricted. Written as it stands, this "
        f"document's own splitter attributes those lines to the preceding key, "
        f"whose owner drops them on the next re-export, while a reader sees a "
        f"top-level key the split never did. Rename the key, or nest it under "
        f"one that is a bare identifier "
        f"(docs/CHIPLET_FORMAT_SPEC.md, top-level block grammar).")
