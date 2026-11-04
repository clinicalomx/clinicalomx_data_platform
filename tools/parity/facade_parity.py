"""Stage 2 facade parity probe. Runs under an isolated HOME; no instance contact.

Run from the repository root:

    mkdir -p /tmp/isohome
    HOME=/tmp/isohome uv --directory packages/clinicalomx run python ../../tools/parity/facade_parity.py
"""
import json

import clinicalomx

exports = sorted(clinicalomx._EXPORTS)
print(json.dumps({"export_count": len(exports), "exports": exports}))

# names must be eager and zero-import
print("names.PROJECT:", clinicalomx.names.PROJECT)

# The dataspec/roispec shim probe was dropped when clinicalomx 0.3.0 deleted both aliases.

# entry-point groups visible from installed metadata
from importlib.metadata import entry_points
for group in ("clinicalomx.vocabulary", "clinicalomx.checks", "clinicalomx.governance", "clinicalomx.recipes"):
    eps = entry_points(group=group)
    print(f"group {group}: {[f'{e.name}={e.value}' for e in eps]}")
