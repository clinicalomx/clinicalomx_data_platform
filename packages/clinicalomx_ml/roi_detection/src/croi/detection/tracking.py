"""Log lamindb records as MLflow Datasets that link to lamin.ai.

Each Collection or Artifact becomes an MLflow ``MetaDataset`` with an HTTP source, its lamin.ai
URL, which the MLflow UI renders as a link. No data is copied. The digest is the record uid.
"""
from __future__ import annotations

import lamindb as ln
import mlflow
from mlflow.data.http_dataset_source import HTTPDatasetSource
from mlflow.data.meta_dataset import MetaDataset


def _current_instance() -> str:
    return ln.setup.settings.instance.slug


def lamin_url(record, *, instance: str | None = None) -> str:
    """lamin.ai URL for a record, e.g. https://lamin.ai/<slug>/collection/<uid>."""
    slug = instance or _current_instance()
    registry = record.__class__.__name__.lower()
    return f"https://lamin.ai/{slug}/{registry}/{record.uid}"


def lamin_dataset(record, *, instance: str | None = None, name: str | None = None) -> MetaDataset:
    """Wrap an ``ln.Collection``/``ln.Artifact`` as a clickable MLflow ``MetaDataset``."""
    source = HTTPDatasetSource(url=lamin_url(record, instance=instance))
    return MetaDataset(source=source, name=name or record.key, digest=record.uid)


def log_collection_inputs(
    collections: dict[str, ln.Collection],
    *,
    instance: str | None = None,
) -> None:
    """``mlflow.log_input`` each ``{context: collection}``, skipping None. Needs an active run.

    ``context`` becomes the ``mlflow.data.context`` tag, e.g. ``train``.
    """
    inst = instance or _current_instance()
    for context, coll in collections.items():
        if coll is None:
            continue
        mlflow.log_input(lamin_dataset(coll, instance=inst), context=context)
