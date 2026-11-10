# Vendored libraries

## chiplet_format_io

Byte-identical copy of the Apache-2.0 reference `.chiplet` reader/writer from
the chiplet-spec repository (`reference/python/chiplet_format_io/__init__.py`).
Same vendoring pattern the other ecosystem consumers use.

Currently vendored: reader 1.22.0, from chiplet-spec dev
at `e846129` (`vendor/chiplet_format_io/__init__.py` sha256 `256191bb...`),
a public dev commit that carries reader release 1.22.0.

Do not edit here. To update: copy the file from chiplet-spec verbatim, move the
pin above with it, and verify the checksum matches. `tests/test_chiplet2dbx.py`
resolves the DECLARED pin with git in a discoverable chiplet-spec repository and
cross-checks byte-identity against that commit's blob, never against the
checkout's working tree, so a sibling that has moved ahead does not turn it red
and drift still does.
