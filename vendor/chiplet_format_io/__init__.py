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

from typing import Any, Dict

import yaml

__all__ = [
    "SUPPORTED_FORMAT_VERSION",
    "ChipletFormatError",
    "loads",
    "load",
    "dumps",
    "dump",
    "validate",
]

#: The only ``format_version`` this reference implementation understands.
SUPPORTED_FORMAT_VERSION = "1.0"


class ChipletFormatError(ValueError):
    """Raised when a .chiplet document is malformed or unsupported."""


def _validate(data: Dict[str, Any], *, allow_intermediate: bool) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ChipletFormatError("top-level .chiplet document must be a mapping")

    fv = data.get("format_version")
    if fv is None:
        raise ChipletFormatError("missing required key: format_version")
    if str(fv) != SUPPORTED_FORMAT_VERSION:
        raise ChipletFormatError(
            f"unsupported format_version {fv!r}; expected "
            f"{SUPPORTED_FORMAT_VERSION!r}"
        )

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


def validate(data: Dict[str, Any], *, allow_intermediate: bool = False) -> Dict[str, Any]:
    """Validate a parsed .chiplet mapping in place; return it. Raises on error."""
    return _validate(data, allow_intermediate=allow_intermediate)


def loads(text: str, *, allow_intermediate: bool = False, validate: bool = True) -> Dict[str, Any]:
    """Parse a .chiplet document from a YAML string into a dict."""
    data = yaml.safe_load(text)
    if data is None:
        raise ChipletFormatError("empty .chiplet document")
    if validate:
        _validate(data, allow_intermediate=allow_intermediate)
    return data


def load(path, *, allow_intermediate: bool = False, validate: bool = True) -> Dict[str, Any]:
    """Read and parse a .chiplet file into a dict."""
    with open(path, "r", encoding="utf-8") as fh:
        return loads(fh.read(), allow_intermediate=allow_intermediate, validate=validate)


def dumps(data: Dict[str, Any], *, validate: bool = True) -> str:
    """Serialize a .chiplet mapping to a canonical YAML string.

    Key order is preserved (insertion order). This is semantic, not byte-exact
    to the GPL host writers.
    """
    if validate:
        # writing an intermediate (finalize_required) document is legitimate
        _validate(data, allow_intermediate=True)
    return yaml.safe_dump(
        data,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


def dump(data: Dict[str, Any], path, *, validate: bool = True) -> None:
    """Serialize a .chiplet mapping to a file."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(dumps(data, validate=validate))
