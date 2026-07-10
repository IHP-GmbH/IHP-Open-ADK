# KiCad assets

Generate the ADK assembly DRU section so it can be dropped into a KiCad
project's `.kicad_dru` file. The output mirrors the ADK KLayout deck: the
only assembly rule with a clean KiCad mapping is `ASM.b` (chiplet-to-chiplet
spacing, emitted as a `courtyard_clearance` constraint). `ASM.a` is left to
KiCad's native courtyard collision check, and `ASM.e`/`ASM.f` (plus the
optional interconnect `IXN.*` rules) are post-layout only and enforced by the
KLayout deck, not here.

## Usage

```sh
# Defaults from config/rule_params.json, written to stdout
python dru/generate_assembly_dru.py

# Merge per-interposer overrides, then write to a file
python dru/generate_assembly_dru.py --interposer-adapter intm4tm2 --out assembly.kicad_dru

# Add an interconnect-axis provenance block (bump pitch/spacing)
python dru/generate_assembly_dru.py --interconnect-adapter ihp_cupillar
```

CLI flags:

- `--interposer-adapter <name|.drc>`: merge numeric `drc_rules[...]` overrides
  from an interposer adapter on top of the defaults. Resolves a shortname
  against `../pdk_adapters/interposer/` or a literal `.drc` path.
- `--interconnect-adapter <name|.drc>`: add an interconnect-axis (`IXN.*`)
  provenance comment. Resolves against `../pdk_adapters/interconnect/`. Omit
  for interposer-only output, which is byte-identical to before this axis
  existed.
- `--out <path>`: write to a file instead of stdout.
- `--rule-params <path>`: use an alternate `rule_params.json`
  (default: `../config/rule_params.json`).

## Importing into another generator

A downstream per-project DRU generator can import the renderer and append its
output as a section:

```python
from adk.kicad.dru.generate_assembly_dru import render_assembly_rules, load_defaults

section = render_assembly_rules(load_defaults())
```

The signature is append-only, so existing importers stay source-compatible:

```python
render_assembly_rules(
    rules,                              # dict of ASM_* defaults (required)
    adapter_name=None,                 # interposer provenance label
    template_dir=None,                 # override the template search dir
    interconnect_rules=None,           # IXN_* defaults
    interconnect_adapter_name=None,    # interconnect provenance label
) -> str
```

See `../docs/integration.md` for the downstream contract.

## Files

- `dru/generate_assembly_dru.py`: the generator; reads `../config/rule_params.json`
  for `ASM_*` defaults and, when an interconnect adapter is given,
  `../config/interconnect.json` for `IXN_*` defaults.
- `dru/templates/assembly_rules.dru.jinja`: Jinja2 template for the rendered section.
