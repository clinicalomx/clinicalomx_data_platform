"""Stage flows. Each module exposes a Prefect ``@flow``. Import the module you need.

Nothing is imported here: several stages import only inside their SIF.

Flow code is not installed. The HPC clones the platform tag at run time, so each module puts
``src/`` on ``sys.path`` itself.

The ``utils-*`` repair flows can be re-run; read a module's docstring before running it. For
bgnorm output, pick by what is wrong:

  a result to delete and re-queue       reset_bgnorm
  no result, and a failure marker       clear_bgnorm_failures
  a row whose bytes are gone            heal_orphaned_bgnorm
  bytes with no row                     sweep_orphan_bgnorm_zarrs

Never assign ``Artifact.suffix``: lamindb moves the file on save. A destructive flow derives every
selection from one scope. Reconcile never moves a unit back past a gate a human decided.
"""
from __future__ import annotations
