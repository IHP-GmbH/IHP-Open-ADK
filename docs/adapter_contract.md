# Adapter contract

Referenced by `klayout/drc/run_drc.py`, `klayout/drc/adk_assembly.drc`, the
READMEs under `pdk_adapters/`, and the top-level `README.md`.

## The contract sentence

> The `.chiplet` adapter field carries a registry **id** matching
> `\A[A-Za-z0-9_][A-Za-z0-9_.-]*\Z` and NOT ending in `.drc`; producers validate
> at emit, consumers validate at use and resolve only through the anchored
> registry; no component maps that field onto a path, an environment variable,
> or a hatch flag.

The canonical form of this contract is the `*.adapter` fields of
`chiplet-spec/schemas/chiplet.schema.json`: the id pattern PLUS
`"not": {"pattern": "\\.drc$"}`. That PAIR, not the bare pattern, is the
contract. `adk_registry.validate_adapter_id` implements it; the generic
`validate_id` is only the first half and accepts, e.g., `evil.drc`. Every
implementation -- the ADK here, the KiCad plugin that emits the field, the
Mosaic engine, and chiplet-studio -- parity-tests against one shared oracle,
`chiplet-spec conformance/fixtures/adapter_id_cases.json` (vendored at
`tests/fixtures/adapter_id_cases.json`), so no consumer verdict silently
diverges. The anchor translates per dialect (`\Z` in Python, `(?![\s\S])` in the
schema and any ECMA/portable port, no end anchor in a C++ `regex_match`); never
copy it across engines.

## Why an id and not a path

An adapter `.drc` is not data. `adk_assembly.drc` does `File.read` on the
selected adapter and `eval`s the result, so whatever that field names is
executed. The field itself arrives from a document (`interposer.adapter` /
`interconnect.adapter` in a `.chiplet`), and a downloaded project is untrusted
by definition. A resolver that accepts a path therefore lets a downloaded
archive choose the code the DRC runs, with the attacker's choice of outcome:

- a *permissive* planted deck yields a silent clean DRC report (exit 0) on
  fab-bound geometry, discovered only in silicon; this is the worse outcome;
- a *hostile* deck is straightforward code execution.

The id shape does most of the discrimination for free. `\A...\Z` forbids `/`,
`\`, `~`, a leading `.` or `-`, `..`, `${...}` and a trailing newline, so no real
path can pass it; and even if one did, the lookup fails closed, because a path
is not a key in the registry table.

One value slips through the shape alone: a bare `.drc` file name like `evil.drc`
is a valid id SHAPE (letters and dots), so `validate_id` accepts it, and only the
registry lookup would then refuse it (it is not a key). The adapter field
therefore carries the extra `.drc` negative, and `validate_adapter_id` refuses
`evil.drc` at validation, in the producer as well as the consumer, so the
producer never emits it and all three roads reject the same set. `x.drcx` and a
bare `drc` are ids, not deck names, and stay valid.

## Producer / consumer split

**Producers** (anything that writes a `.chiplet`: the KiCad plugin, exporters,
hand-authored documents) validate the field **at emit** against the PAIR. Call
`adk_registry.validate_adapter_id` (id pattern plus the `.drc` negative), or
validate against `chiplet.schema.json`'s `*.adapter` directly; a port that
mirrors only `ID_PATTERN`, or that anchors with `^...$` instead of `\A`/`\Z`,
silently accepts `evil.drc` or a trailing-newline id (in Python `$` also matches
just before a final newline).

**Consumers** (this repository: `klayout/drc/run_drc.py` and
`kicad/dru/generate_assembly_dru.py`) validate the field **at use** and then
resolve it through `adk_registry` and nothing else. The consumers are the
enforcement point, not the producers: the threat model is a document that never
passed through our producer, so the guarantee has to hold with a hostile,
absent, or hand-edited one. Both consumers move together; a split would leave
one accepting what the other rejects.

Neither resolver constructs a `Path` from its argument, joins it onto a
directory, or touches the working directory. Their return range is by
construction a subset of the registry table's vetted values.

Exit codes at a consumer boundary come from `adk_registry.exit_code_for`:
unknown or malformed id (and a path where an id belongs) is `1`, an unusable
registry is `5`, a registry declaring an unreadable major is `6`.

## Registry layers

`adk_registry` merges three anchored layers, lowest precedence first:

1. `adk_registry.builtin.json`, beside `adk_registry.py` (required; the trust
   anchor, located via `Path(__file__)` so no document can repoint it);
2. `$XDG_CONFIG_HOME/adk-tools/registry.json` (optional user layer);
3. `$ADK_REGISTRY_DIR/registry.json` (optional process-env layer; if the
   variable is set the file must exist and be valid).

A relative value resolves against the directory of the file that declared it.
`Resolution.source` names the file that supplied the winning value, and the
consumers log it, so which registry answered is never silent.

`run_drc.py --list-adapters` prints the registered ids with their decks and
sources.

## Migration: the path form is gone

Adapters used to be selectable either by shortname or by an absolute path to a
`.drc`. **The path form is removed, with no deprecation window** (a window
keeps the hole open for the length of the window). Both forms of the old
capability go together: the path branch and the shortname-into-directory join
that let `../interconnect/ihp_cupillar` escape the vetted directory without
looking like a path.

- Using a shipped adapter: nothing changes. `--interposer-adapter intm4tm2`,
  `--interconnect-adapter ihp_cupillar`, and the other built-in ids all still
  work exactly as before.
- Using an unregistered deck (a PDK author's work in progress, an operator's
  local variant): **register it**, then name the id. Write a
  `registry.json` next to it and point `ADK_REGISTRY_DIR` at that directory,
  or add the entry to `$XDG_CONFIG_HOME/adk-tools/registry.json`:

  ```json
  {
    "schema": "adk-registry",
    "version": "1.0",
    "adapters": { "my_interposer_wip": "/abs/path/to/my_interposer.drc" }
  }
  ```

  then `--interposer-adapter my_interposer_wip`.

Registering is a deliberate, auditable act by the operator, in a location a
project archive cannot write. Naming a path was neither.

There is no `--unsafe-deck`-style escape flag, by decision: a flag on the
binary that processes untrusted documents cannot tell an operator-typed string
from a document-carried one, since both arrive as the same argument value.
