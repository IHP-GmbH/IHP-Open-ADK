# Vendored libraries

## chiplet_format_io

Byte-identical copy of the Apache-2.0 reference `.chiplet` reader/writer from
the chiplet-spec repository (`reference/python/chiplet_format_io/__init__.py`).
Same vendoring pattern the other ecosystem consumers use.

Currently vendored: reader 1.3.0, from chiplet-spec `origin/dev` at `0d850ca`
(`vendor/chiplet_format_io/__init__.py` sha256 `5c1d3ebe...`).

Do not edit here. To update: copy the file from chiplet-spec verbatim and
verify the checksums match. `tests/test_chiplet2dbx.py` cross-checks
byte-identity against a discoverable chiplet-spec sibling checkout and fails
loudly on drift.
