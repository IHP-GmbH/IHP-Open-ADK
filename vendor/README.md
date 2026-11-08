# Vendored libraries

## chiplet_format_io

Byte-identical copy of the Apache-2.0 reference `.chiplet` reader/writer from
the chiplet-spec repository (`reference/python/chiplet_format_io/__init__.py`).
Same vendoring pattern the other ecosystem consumers use.

Currently vendored: reader 1.21.0, from chiplet-spec `release/v2026.11`
at `6f8e0dc` (`vendor/chiplet_format_io/__init__.py` sha256 `b48aadf2...`).

Do not edit here. To update: copy the file from chiplet-spec verbatim, move the
pin above with it, and verify the checksums match. `tests/test_chiplet2dbx.py`
resolves the DECLARED pin with git in a discoverable chiplet-spec repository and
cross-checks byte-identity against that commit's blob, never against the
checkout's working tree, so a sibling that has moved ahead does not turn it red
and drift still does.
