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

Two version constants, and they answer different questions.
:data:`SUPPORTED_FORMAT_VERSION` is the highest ``.chiplet`` ``format_version``
this reader was written for, i.e. a fact about documents. :data:`__version__` is
the release of the reader itself, and it is the value a VENDORED copy of this
file carries into the tool that embedded it. That is what makes byte identity
unnecessary: a consumer requires a reader release (``cfio.__version__``) and a
document version, and never has to hash a copied file to find out what it has.
"""
from __future__ import annotations

import re
import warnings
from collections.abc import Sequence as _Sequence
from typing import Any, Callable, Dict, Optional, Tuple, Union

import yaml

__all__ = [
    "SUPPORTED_FORMAT_VERSION",
    "ACCEPTED_FORMAT_VERSIONS",
    "KNOWN_INTERFACE_TYPES",
    "IO_CLASS_INTERFACE_TYPES",
    "__version__",
    "ChipletFormatError",
    "ContractVersionError",
    "ContractVersionWarning",
    "FormatVersionWarning",
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
]

#: The ``format_version`` this reference implementation WRITES, and the entry of
#: :data:`ACCEPTED_FORMAT_VERSIONS` for the major it writes. The on-disk baseline
#: stays additive-stable at "1.0"; readers are tolerant of a same-major higher
#: minor (see :func:`check_format_version`), so this is a single exported string
#: constant, never re-derived from a bump.
SUPPORTED_FORMAT_VERSION = "1.0"

#: The SET of majors this reader accepts, one ``MAJOR.MINOR`` floor per major
#: (docs/VERSION_POLICY.md, "Changing the major"). A one-element tuple is the
#: ordinary state; a second entry appears only while a major transition is open,
#: and it is a PROMISE that the code path for that major exists, never a wish.
#: :data:`SUPPORTED_FORMAT_VERSION` must be a member (conformance asserts it):
#: what this reader writes has to be something it can read back.
ACCEPTED_FORMAT_VERSIONS = ("1.0",)

#: The release of THIS reader, and the value a vendored copy carries with it.
#: Distinct from :data:`SUPPORTED_FORMAT_VERSION`, which is about the documents:
#: one says what is on disk, the other says which reader is in the tree. It is the
#: distribution version too (``pyproject.toml`` reads it from here), so a consumer
#: that installed the package and one that vendored the file agree on the number.
#: Bumped under the same policy as everything else (docs/VERSION_POLICY.md).
__version__ = "1.3.0"

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
#: release. The grammar is what SPEC-14's repeated-key scan, flow rule 4,
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


def _parse_version(fv: Any) -> Optional[Tuple[int, int]]:
    """Parse a ``format_version`` value into ``(major, minor)`` or ``None``.

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


_SUPPORTED_MAJOR, _SUPPORTED_MINOR = _parse_version(SUPPORTED_FORMAT_VERSION)  # type: ignore[misc]


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
    parsed = _parse_version(fv)
    if parsed is None:
        raise ChipletFormatError(
            f"malformed format_version {fv!r}; expected a quoted "
            f'"MAJOR.MINOR" string')
    major, minor = parsed
    accepted = _accepted_map(ACCEPTED_FORMAT_VERSIONS, "format_version",
                             _parse_version)
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
    :func:`_parse_version` (the ``.chiplet`` ``format_version``, which is
    MAJOR.MINOR only and is pinned that way by schemas/chiplet.schema.json), a
    non-string is NOT coerced: the sidecars are JSON, where an unquoted 1.10 is
    the number 1.1 with no way back, so a bare number is malformed here.
    """
    if not isinstance(value, str):
        return None
    # No .strip(): the sidecar schemas' version patterns admit no surrounding
    # whitespace, and a reader must not accept what the schema refuses (the
    # earlier exemption, "surrounding whitespace is not a version change", made
    # this parser laxer than both the schema and _parse_version; SPEC-2).
    parts = value.split(".")
    if len(parts) not in (2, 3):
        return None
    # ASCII digits only, for the same reason _parse_version checks them: int()
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


def _apply_write_version(out: Dict[str, Any]) -> None:
    """Set ``out['format_version']`` per the passthrough writer rule (H-B crux).

    The stamped version must describe the bytes written. This is a *lossless*
    passthrough writer (it re-emits the whole dict, unknown keys included), so a
    same-major higher-minor input is PRESERVED; a missing/lower/equal version is
    stamped down to :data:`SUPPORTED_FORMAT_VERSION`. Warning re-emission is done
    by the validate pass in :func:`dumps`, not here. (Lossy writers -- the C++
    struct writer, the from-scratch KiCad exporter -- stamp SUPPORTED instead.)
    """
    parsed = _parse_version(out.get("format_version"))
    if parsed is not None and parsed[0] == _SUPPORTED_MAJOR \
            and parsed[1] > _SUPPORTED_MINOR:
        out["format_version"] = f"{parsed[0]}.{parsed[1]}"
    else:
        out["format_version"] = SUPPORTED_FORMAT_VERSION


def _check_line_breaks(text: str) -> None:
    """Refuse a document whose line breaks are not LF or CRLF, before any parse.

    The format's line breaks are LF and CRLF (docs/CHIPLET_FORMAT_SPEC.md, "Line
    breaks"). A character a YAML parser breaks a line on and this grammar does
    not is ill-formed, because the grammar is what SPEC-14's repeated-key scan,
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


def _validate(data: Dict[str, Any], *, allow_intermediate: bool,
              on_warn: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ChipletFormatError("top-level .chiplet document must be a mapping")

    check_format_version(data.get("format_version"), on_warn=on_warn)

    meta = data.get("_metadata") or {}
    if isinstance(meta, dict) and meta.get("finalize_required") and not allow_intermediate:
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

    return data


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
    exists (SPEC-24). A pad whose io_class is outside the table is not judged
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
             on_warn: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Validate a parsed .chiplet mapping in place; return it. Raises on error."""
    return _validate(data, allow_intermediate=allow_intermediate, on_warn=on_warn)


def loads(text: str, *, allow_intermediate: bool = False, validate: bool = True,
          on_warn: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Parse a .chiplet document from a YAML string into a dict.

    Refuses a document with a REPEATED top-level key, on the source text, before
    anything is read out of it. That is not a style rule: PyYAML keeps the last
    value and yaml-cpp the first, so this reader and the C++ one would report
    different documents from one file, and nothing downstream, schema included,
    can tell. A quoted key at column zero makes a document unsplittable, not
    invalid, and is read normally (see :func:`top_level_blocks` and flow rule 1).

    Refuses, for the same reason and ahead of the YAML parse rather than after
    it, a document whose line breaks are not LF or CRLF: NEL (U+0085), U+2028 or
    U+2029 anywhere, and a CR not immediately followed by LF. A YAML parser
    breaks a line on each of them and this grammar does not (see
    :func:`_check_line_breaks`). It runs FIRST because on such a document
    ``yaml.safe_load`` either raises with a parser message that says nothing
    about the real defect or, worse, returns a document with a top-level key
    that is not in the file.
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
    if validate:
        _validate(data, allow_intermediate=allow_intermediate, on_warn=on_warn)
    return data


def load(path, *, allow_intermediate: bool = False, validate: bool = True,
         on_warn: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Read and parse a .chiplet file into a dict."""
    with open(path, "r", encoding="utf-8") as fh:
        return loads(fh.read(), allow_intermediate=allow_intermediate,
                     validate=validate, on_warn=on_warn)


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
    """
    out = dict(data)
    if validate:
        # Writing an intermediate (finalize_required) document is legitimate.
        # This re-runs check_format_version, so a preserved higher minor re-warns
        # on write (deduped default channel; every event to on_warn).
        _validate(out, allow_intermediate=True, on_warn=on_warn)
    _apply_write_version(out)
    return yaml.dump(
        out,
        Dumper=_CanonicalDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


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
    """Serialize a .chiplet mapping to a file."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(dumps(data, validate=validate, on_warn=on_warn))


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


def _scan_top_level(text: str) -> Tuple[Dict[str, str], Optional[str]]:
    """One pass over the source text: the blocks, and why they may be unusable.

    Returns ``(blocks, not_splittable)``. ``not_splittable`` is ``None`` for a
    document that can be split, and otherwise the reason it cannot be, which the
    splitting accessors raise and :func:`loads` ignores. The two are different
    verdicts: a quoted key at column zero leaves nobody able to say who owns
    those bytes, and that is a reason not to SPLIT or WRITE the document, not a
    reason to refuse to read it.

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
        elif not_splittable is None and _QUOTED_KEY_LINE_RE.match(content):
            not_splittable = (
                f"line {number}: quoted key at column zero ({content!r}). A "
                f"quoted key is valid YAML but does not start a top-level "
                f"block, so this document cannot be split without attaching "
                f"the block to the preceding key, where its owner drops it on "
                f"the next re-export. Emit bare keys and quote values "
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

    * a quoted key at column zero. It is not a key line, so splitting would
      attach that block to the preceding key and lose it on the next re-export.
      The document is still valid and :func:`loads` still reads it; splitting is
      what cannot be done.
    * a repeated top-level key. That document is ill-formed and :func:`loads`
      refuses it too.
    """
    blocks, not_splittable = _scan_top_level(text)
    if not_splittable is not None:
        raise ChipletFormatError(not_splittable)
    return blocks


def top_level_block(text: str, key: str) -> Optional[str]:
    """Return the exact source text of one top-level block, or ``None``.

    Convenience over :func:`top_level_blocks` with the same guarantees, and the
    call flow rule 4 needs: ``top_level_block(text, "flow")`` is the block a host
    that did not author it re-emits unchanged.
    """
    return top_level_blocks(text).get(key)
