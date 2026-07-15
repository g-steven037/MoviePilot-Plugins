"""Encode a font as deterministic gzip + Base64 text for MoviePilot v2.14.2."""

from __future__ import annotations

import base64
import gzip
import hashlib
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: encode_font_asset.py INPUT_FONT OUTPUT_B64")
    source, target = map(Path, sys.argv[1:])
    raw = source.read_bytes()
    encoded = base64.b64encode(gzip.compress(raw, compresslevel=9, mtime=0))
    target.write_bytes(encoded)
    print(f"SHA256={hashlib.sha256(raw).hexdigest().upper()} SIZE={len(raw)} ENCODED={len(encoded)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
