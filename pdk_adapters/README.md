# PDK adapters

Adapters bridge ADK abstract layer names to PDK-specific fabrication layers.
The ADK never names a PDK layer number directly. Adapters are the only place
where that mapping is allowed to exist.

## Structure

- `interposer/` — one file per supported interposer PDK. An interposer adapter
  is required for any ADK run. The active adapter is selected via the
  runner's `--interposer-adapter <name>` flag (or, via the orchestrator,
  through the `.chiplet` YAML's `interposer.adapter` field).

There is intentionally **no chiplet adapter** in v0.1.0. Today's ASM rule set
treats chiplets as black-boxes via `exchange0` (their mechanical outline). A
chiplet-adapter mechanism will be designed if and when a real consumer
appears. See `docs/architecture.md`.

## Contract

See `docs/adapter_contract.md` for the formal contract. The runner validates
that every required abstract input is declared by the adapter *before* the
deck is evaluated; missing inputs fail loudly with an explicit error.
