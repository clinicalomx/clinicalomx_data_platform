# Upgrading a consumer to `clinicalomx` v0.3.0

v0.3.0 deletes the `dataspec` and `roispec` aliases.
Nothing else changed: `_EXPORTS`, the public API of `clinicalomx`, is the same.
`import dataspec` or `import roispec` now raises `ModuleNotFoundError`.

## Steps

1. Find every use of the old names:

   ```bash
   grep -rn "dataspec\|roispec" --include="*.py" --include="*.ipynb" --include="*.toml" . | grep -v "\.venv"
   ```

2. Rewrite each import with this map:

   | Old | New |
   |---|---|
   | `dataspec` | `clinicalomx` |
   | `dataspec.config` `dataspec.environments` `dataspec.idempotency` `dataspec.manifest` `dataspec.models` `dataspec.refs` `dataspec.review` `dataspec.settings` | `clinicalomx.<same name>` |
   | `roispec` | `clinicalomx` |
   | `roispec.ops` | `clinicalomx.roi` |

   The ROI functions (`annotate`, `create`, `update`, `split`, `merge`, `resolve_roi`, `require_roi`, `ensure_features`, `ensure_types`, `get_type`, `get_or_create`) and constants (`ROI_UID`, `ROI_NAME`, `ROI_TYPE`, `PARENT_ROI`, `ROIOp`) are all on the facade.
   So `from roispec import ROI_UID` becomes `from clinicalomx import ROI_UID`.

3. Run the grep again; it must print nothing.
4. Only now widen the pin to include 0.3.0.

Every consumer in this monorepo bounds `clinicalomx` at `>=0.2,<0.3`.
Moving that bound is a release decision.

The vocabulary-ownership half of the migration is [migration.md](migration.md).
