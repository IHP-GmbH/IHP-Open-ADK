# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
r"""adk_registry -- anchored ID->location resolver for the ADK seam.

Purpose
-------
No project-derived value (a KiCad ``.kicad_pro`` text var, a footprint /
board CONNECTION field, a shipped ``.chiplet``) may ever name a code root, an
interpreter, or an eval'd DRC deck. It may name only a *registry id*. This
module maps such ids to concrete locations using ONLY registry files anchored
to the running code, never a location a project archive can reach.

Namespaces
----------
Three flat namespaces, each an ``id -> path`` table:

* ``adapter``      -- a shortname resolving to a vetted ``.drc`` file.
* ``root``         -- a repository/tree root resolving to a **directory**
                      (consumers join a fixed relative path onto it).
* ``interpreter``  -- a worker interpreter id resolving to an executable path.

Every id is validated against ``\A[A-Za-z0-9_][A-Za-z0-9_.-]*\Z``, which by
construction forbids ``/``, ``~``, a leading ``.``/``-``, ``..`` and
``${...}``. Any real path fails it, so the regex does id-vs-path
discrimination for free.

Registry locations (anchored; precedence low -> high)
-----------------------------------------------------
1. Built-in default: ``adk_registry.builtin.json`` **in the same directory as
   this module**. Located via ``Path(__file__)`` -- never via a text var -- so
   a project archive cannot repoint the file that resolves roots. Required.
2. XDG user layer: ``$XDG_CONFIG_HOME/adk-tools/registry.json`` (default
   ``~/.config/adk-tools/registry.json``). Optional.
3. Process-env override: ``$ADK_REGISTRY_DIR/registry.json``. Optional, but
   if ``ADK_REGISTRY_DIR`` is set the file must exist and be valid.

Layers merge per namespace/id with the higher layer winning; the file that
supplied the winning value is returned in :class:`Resolution.source`, so a run
can RECORD which registry answered (no silent authority).

A relative path value resolves against the directory of the registry file that
declared it; an absolute value is used as-is.

Fail closed
-----------
A regex-valid but unregistered id is a hard error. No default, no fallback, no
CWD join, no discovery walk.

Exit-code mapping (for a CLI boundary; mirrors run_drc's EXIT_* meanings --
do NOT import run_drc). The resolver itself only raises typed exceptions; a
CLI wrapper maps them:

* :class:`IdLookupError`      -> ``1`` (caller: unknown id, malformed id, or a
                                 path where an id is required).
* :class:`RegistrySourceError`-> ``5`` (a registry that is absent, unreadable,
                                 malformed, or maps a root to a non-directory:
                                 a source that cannot be used).
* :class:`RegistryVersionError`-> ``6`` (a registry declares a major this code
                                 must not read).

The convenience constants :data:`EXIT_USAGE`, :data:`EXIT_SOURCE` and
:data:`EXIT_VERSION` hold ``1``/``5``/``6`` and :func:`exit_code_for` maps an
exception instance, so a boundary need not re-derive the table.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

__all__ = [
    "REGISTRY_SCHEMA",
    "SUPPORTED_MAJOR",
    "NAMESPACES",
    "ID_PATTERN",
    "RegistryError",
    "IdLookupError",
    "RegistrySourceError",
    "RegistryVersionError",
    "EXIT_USAGE",
    "EXIT_SOURCE",
    "EXIT_VERSION",
    "exit_code_for",
    "Resolution",
    "validate_id",
    "resolve_adapter",
    "resolve_root",
    "resolve_interpreter",
    "available",
]

#: Schema marker every registry file must carry.
REGISTRY_SCHEMA = "adk-registry"

#: The registry major this code reads. A same-major higher minor is tolerated
#: (additive, like the .chiplet reader); any other major is refused with
#: :class:`RegistryVersionError`.
SUPPORTED_MAJOR = 1

#: The three id namespaces and, per namespace, the JSON key holding its table.
NAMESPACES: Tuple[str, ...] = ("adapter", "root", "interpreter")
_NS_KEY = {"adapter": "adapters", "root": "roots", "interpreter": "interpreters"}

#: Built-in registry filename, resolved beside this module (the trust anchor).
_BUILTIN_FILENAME = "adk_registry.builtin.json"

#: Process-env override: a *directory* holding ``registry.json``.
_ENV_OVERRIDE_DIR = "ADK_REGISTRY_DIR"

#: XDG user layer, under ``$XDG_CONFIG_HOME`` (default ``~/.config``).
_XDG_SUBPATH = ("adk-tools", "registry.json")

#: Basename of the registry file inside the env-override and XDG directories.
_LAYER_BASENAME = "registry.json"

#: The one true id shape. Forbids ``/``, ``~``, leading ``.``/``-``, ``..`` and
#: ``${...}`` by construction, so any real path fails it. Anchored with
#: ``\A``/``\Z``, NOT ``^``/``$``: in Python ``$`` also matches just before a
#: single trailing newline, so ``^...$`` accepts ``"id\n"`` -- which would then
#: flow straight into a quoted scalar of an emitted ``.chiplet``. Keep
#: ``\A``/``\Z``; the exported constant must be safe under match, search and
#: fullmatch alike (a non-Python port must anchor the whole string, newline
#: included).
ID_PATTERN = r"\A[A-Za-z0-9_][A-Za-z0-9_.-]*\Z"
_ID_RE = re.compile(ID_PATTERN)


# --------------------------------------------------------------------------- #
# Exceptions and the CLI exit-code mapping                                     #
# --------------------------------------------------------------------------- #
class RegistryError(Exception):
    """Base class for every registry failure."""


class IdLookupError(RegistryError):
    """Caller error: unknown id, malformed id, or a path where an id is
    required. A CLI boundary maps this to exit ``1`` (usage/bad-input)."""


class RegistrySourceError(RegistryError):
    """The registry is unusable as a source: absent (when required),
    unreadable, malformed, or it maps a root to something that is not a
    directory. A CLI boundary maps this to exit ``5``."""


class RegistryVersionError(RegistryError):
    """A registry declares a major version this code must not read. A CLI
    boundary maps this to exit ``6``."""


EXIT_USAGE = 1
EXIT_SOURCE = 5
EXIT_VERSION = 6


def exit_code_for(exc: BaseException) -> int:
    """Map a resolver exception to the shared exit-code table.

    Mirrors run_drc's EXIT_* meanings without importing it. An exception with
    no rule maps to ``7`` (internal) so a boundary never reports a resolver
    bug as a clean or caller result.
    """
    if isinstance(exc, IdLookupError):
        return EXIT_USAGE
    if isinstance(exc, RegistryVersionError):
        return EXIT_VERSION
    if isinstance(exc, RegistrySourceError):
        return EXIT_SOURCE
    return 7


class Resolution(NamedTuple):
    """The answer to one resolve call.

    ``value`` is a directory for a ``root`` id, a ``.drc`` file for an
    ``adapter`` id, and an interpreter path for an ``interpreter`` id.
    ``source`` is the registry file that supplied the winning value -- log it
    so the authority behind a run is never silent.
    """

    id: str
    namespace: str
    value: Path
    source: Path


# --------------------------------------------------------------------------- #
# Id validation                                                                #
# --------------------------------------------------------------------------- #
def validate_id(raw: object, namespace: Optional[str] = None) -> str:
    """Return ``raw`` unchanged if it is a well-formed id, else raise.

    A value carrying a path separator or a ``${...}`` token is reported as
    "a path where an id is required"; anything else failing the regex is a
    malformed id. Both are :class:`IdLookupError` (exit ``1``).
    """
    where = f" for {namespace}" if namespace else ""
    if not isinstance(raw, str) or not raw:
        raise IdLookupError(
            f"expected a registry id{where}, got {raw!r}."
        )
    if _ID_RE.match(raw):
        return raw
    looks_like_path = ("/" in raw or "\\" in raw or raw.startswith("~")
                       or "${" in raw or raw == "." or raw == "..")
    if looks_like_path:
        raise IdLookupError(
            f"a path was given where a registry id is required{where}: "
            f"{raw!r}. Register the target and reference it by id "
            f"(pattern {ID_PATTERN})."
        )
    raise IdLookupError(
        f"malformed registry id{where}: {raw!r} (must match {ID_PATTERN})."
    )


# --------------------------------------------------------------------------- #
# Registry loading and layering                                                #
# --------------------------------------------------------------------------- #
def _builtin_path() -> Path:
    return Path(__file__).resolve().parent / _BUILTIN_FILENAME


def _xdg_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root.joinpath(*_XDG_SUBPATH)


def _env_path() -> Optional[Path]:
    directory = os.environ.get(_ENV_OVERRIDE_DIR)
    if not directory:
        return None
    return Path(directory) / _LAYER_BASENAME


def _check_major(data: dict, path: Path) -> None:
    version = data.get("version")
    try:
        major = int(str(version).split(".")[0])
    except (ValueError, TypeError, IndexError, AttributeError):
        raise RegistrySourceError(
            f"{path}: malformed registry version {version!r}."
        )
    if major != SUPPORTED_MAJOR:
        raise RegistryVersionError(
            f"{path}: registry declares major {major}; this code reads "
            f"major {SUPPORTED_MAJOR} only."
        )


def _load_one(path: Path, required: bool) -> Optional[dict]:
    """Load one registry file, or None when an optional layer is absent."""
    if not path.is_file():
        if required:
            raise RegistrySourceError(f"registry file not found: {path}")
        return None
    try:
        text = path.read_text()
        data = json.loads(text)
    except OSError as exc:
        raise RegistrySourceError(f"{path}: cannot read registry: {exc}")
    except json.JSONDecodeError as exc:
        raise RegistrySourceError(f"{path}: registry is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise RegistrySourceError(
            f"{path}: registry root must be a JSON object, got "
            f"{type(data).__name__}."
        )
    if data.get("schema") != REGISTRY_SCHEMA:
        raise RegistrySourceError(
            f"{path}: not an adk-registry (schema={data.get('schema')!r}, "
            f"expected {REGISTRY_SCHEMA!r})."
        )
    _check_major(data, path)
    return data


def _layers() -> List[Tuple[Path, bool]]:
    """Registry files in precedence order, low -> high (later overrides)."""
    layers: List[Tuple[Path, bool]] = [(_builtin_path(), True)]
    layers.append((_xdg_path(), False))
    env = _env_path()
    if env is not None:
        layers.append((env, True))
    return layers


def _merged() -> Dict[str, Dict[str, Tuple[str, Path]]]:
    """Merge every present layer into ``namespace -> {id: (value, source)}``."""
    merged: Dict[str, Dict[str, Tuple[str, Path]]] = {
        ns: {} for ns in NAMESPACES
    }
    for path, required in _layers():
        data = _load_one(path, required)
        if data is None:
            continue
        for ns in NAMESPACES:
            table = data.get(_NS_KEY[ns], {})
            if table is None:
                continue
            if not isinstance(table, dict):
                raise RegistrySourceError(
                    f"{path}: '{_NS_KEY[ns]}' must be a JSON object, got "
                    f"{type(table).__name__}."
                )
            for key, value in table.items():
                if not isinstance(value, str):
                    raise RegistrySourceError(
                        f"{path}: {_NS_KEY[ns]}[{key!r}] must be a string "
                        f"path, got {type(value).__name__}."
                    )
                merged[ns][key] = (value, path)
    return merged


def _to_path(value: str, source: Path) -> Path:
    """Resolve a registry value: relative to the declaring file, or absolute."""
    p = Path(value)
    if not p.is_absolute():
        p = source.parent / p
    return p.resolve()


def _resolve(namespace: str, id_: str) -> Resolution:
    validate_id(id_, namespace)
    table = _merged()[namespace]
    entry = table.get(id_)
    if entry is None:
        known = ", ".join(sorted(table)) or "(none)"
        raise IdLookupError(
            f"unknown {namespace} id {id_!r}. Registered {namespace} ids: "
            f"{known}."
        )
    value, source = entry
    return Resolution(id_, namespace, _to_path(value, source), source)


# --------------------------------------------------------------------------- #
# Public resolve API                                                           #
# --------------------------------------------------------------------------- #
def resolve_root(root_id: str) -> Resolution:
    """Resolve a ``root`` id to a directory.

    Invariant: the returned ``value`` is an existing directory, because
    consumers join fixed relative paths onto a root and a non-directory would
    break those joins silently. A root that maps to a missing path or a file
    is a :class:`RegistrySourceError` (exit ``5``).
    """
    res = _resolve("root", root_id)
    if not res.value.is_dir():
        raise RegistrySourceError(
            f"root {root_id!r} maps to {res.value}, which is not a directory "
            f"(registry: {res.source}). Root ids must resolve to a directory."
        )
    return res


def resolve_adapter(adapter_id: str) -> Resolution:
    """Resolve an ``adapter`` id to a vetted ``.drc`` file.

    A known id whose file is missing is a :class:`RegistrySourceError`
    (exit ``5``); an unknown id is an :class:`IdLookupError` (exit ``1``).
    """
    res = _resolve("adapter", adapter_id)
    if not res.value.is_file():
        raise RegistrySourceError(
            f"adapter {adapter_id!r} maps to {res.value}, which is not a file "
            f"(registry: {res.source})."
        )
    return res


def resolve_interpreter(interpreter_id: str) -> Resolution:
    """Resolve an ``interpreter`` id to an interpreter path.

    Unlike a root, the path is returned without a filesystem check: the
    consumer that execs it (the plugin) validates executability. An unknown id
    is still an :class:`IdLookupError` (exit ``1``).
    """
    return _resolve("interpreter", interpreter_id)


def available(namespace: str) -> List[str]:
    """Sorted registered ids in ``namespace`` (for ``--list-*`` diagnostics)."""
    if namespace not in NAMESPACES:
        raise ValueError(
            f"unknown namespace {namespace!r}; expected one of {NAMESPACES}."
        )
    return sorted(_merged()[namespace])
