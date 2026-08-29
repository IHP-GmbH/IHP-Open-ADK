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
"""
from __future__ import annotations

import warnings
from typing import Any, Callable, Dict, Optional, Tuple

import yaml

__all__ = [
    "SUPPORTED_FORMAT_VERSION",
    "ChipletFormatError",
    "FormatVersionWarning",
    "check_format_version",
    "loads",
    "load",
    "dumps",
    "dump",
    "validate",
]

#: The highest ``format_version`` this reference implementation was written for.
#: The on-disk baseline stays additive-stable at "1.0"; readers are tolerant of
#: a same-major higher minor (see :func:`check_format_version`), so this is a
#: single exported string constant, never re-derived from a bump.
SUPPORTED_FORMAT_VERSION = "1.0"


class ChipletFormatError(ValueError):
    """Raised when a .chiplet document is malformed or unsupported."""


class FormatVersionWarning(UserWarning):
    """A .chiplet declares a newer same-major minor than the reader supports.

    The document is still read (as the supported version, ignoring unknown
    additions), but a same-major higher minor may carry fields this reader does
    not understand, so the event is surfaced.
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
    parts = fv.strip().split(".")
    if len(parts) != 2:
        return None
    try:
        major, minor = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if major < 0 or minor < 0:
        return None
    return (major, minor)


_SUPPORTED_MAJOR, _SUPPORTED_MINOR = _parse_version(SUPPORTED_FORMAT_VERSION)  # type: ignore[misc]

#: Module-private warn-once state, keyed by the raw "MAJOR.MINOR" that fired.
#: Resettable so a test suite does not leak dedup state across cases.
_WARNED_VERSIONS: set = set()


def _reset_version_warnings() -> None:
    """Clear the warn-once dedup set (test hook)."""
    _WARNED_VERSIONS.clear()


def check_format_version(fv: Any, *,
                         on_warn: Optional[Callable[[str], None]] = None) -> str:
    """Apply the tolerant ``format_version`` policy; return the normalized version.

    Policy: missing or malformed -> :class:`ChipletFormatError`; a major other
    than the supported major (higher OR lower) -> :class:`ChipletFormatError`;
    same major, minor <= supported -> accept silently; same major, higher minor
    -> accept, warn once per distinct version via ``warnings.warn`` AND deliver
    every event (undeduped) to ``on_warn`` when supplied.
    """
    if fv is None:
        raise ChipletFormatError("missing required key: format_version")
    parsed = _parse_version(fv)
    if parsed is None:
        raise ChipletFormatError(
            f"malformed format_version {fv!r}; expected a quoted "
            f'"MAJOR.MINOR" string')
    major, minor = parsed
    if major != _SUPPORTED_MAJOR:
        raise ChipletFormatError(
            f"unsupported format_version {fv!r}; this reader supports major "
            f"{_SUPPORTED_MAJOR} (e.g. {SUPPORTED_FORMAT_VERSION!r})")
    normalized = f"{major}.{minor}"
    if minor > _SUPPORTED_MINOR:
        msg = (
            f"format_version {fv!r} is newer than this reader's "
            f"{SUPPORTED_FORMAT_VERSION!r} (same major {major}); reading it as "
            f"{SUPPORTED_FORMAT_VERSION!r} and ignoring unknown additions")
        if on_warn is not None:
            on_warn(msg)
        if normalized not in _WARNED_VERSIONS:
            _WARNED_VERSIONS.add(normalized)
            warnings.warn(msg, FormatVersionWarning, stacklevel=2)
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

    return data


def validate(data: Dict[str, Any], *, allow_intermediate: bool = False,
             on_warn: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Validate a parsed .chiplet mapping in place; return it. Raises on error."""
    return _validate(data, allow_intermediate=allow_intermediate, on_warn=on_warn)


def loads(text: str, *, allow_intermediate: bool = False, validate: bool = True,
          on_warn: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Parse a .chiplet document from a YAML string into a dict."""
    data = yaml.safe_load(text)
    if data is None:
        raise ChipletFormatError("empty .chiplet document")
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
    """
    out = dict(data)
    if validate:
        # Writing an intermediate (finalize_required) document is legitimate.
        # This re-runs check_format_version, so a preserved higher minor re-warns
        # on write (deduped default channel; every event to on_warn).
        _validate(out, allow_intermediate=True, on_warn=on_warn)
    _apply_write_version(out)
    return yaml.safe_dump(
        out,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


def dump(data: Dict[str, Any], path, *, validate: bool = True,
         on_warn: Optional[Callable[[str], None]] = None) -> None:
    """Serialize a .chiplet mapping to a file."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(dumps(data, validate=validate, on_warn=on_warn))
