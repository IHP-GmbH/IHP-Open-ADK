# IHP Open Assembly Design Kit (ADK)

The ADK holds the assembly-level rules and checks for placing chiplets on an
interposer: placement and interconnect DRC through PDK adapters, the KiCad
design rules generated from them, and the OpenROAD 3Dblox export of a finished
`.chiplet` assembly. It sits one level above the interposer and die PDKs, which
stay ordinary PDKs, in the IHP Open ADK flow.

## Use

The assembly DRC on an interposer GDS, inside the ADK-Tools image, where
`adk-drc` runs the ADK's `klayout/drc/run_drc.py` with the image's KLayout:

```bash
adk-drc --path design.gds --interposer-adapter intm4tm2 --run_dir /tmp/adk_drc
```

The runner reads the chiplet boundaries from `design.boundaries.json` next to
the GDS.

## Documentation

The full documentation is at
https://ihp-open-adk-docs.readthedocs.io/en/latest/adk.html

## Release

Released as part of the IHP Open ADK. The release page lists the commit of every
repository in each release:
https://ihp-open-adk-docs.readthedocs.io/en/latest/install/00_release.html

## License

Apache-2.0. See `LICENSE`.
