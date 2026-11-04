"""The objective template: what a detection objective under ``croi/objectives/`` provides.

- ``objective.py``: a :class:`DetectionObjective` record.
- ``registry.py``: a :class:`croi.io.registries.ChampionRegistry`.
- ``dataset.py``: a :class:`croi.io.datasets.DetectionDataset` subclass implementing ``pairs_in``.
- ``vocab.py``: an :class:`ObjectiveVocabulary`, also a ``clinicalomx.vocabulary`` entry point.
- ``cli.py``: ``SUBCOMMANDS`` of :class:`croi.config.Subcommand`, configs modelled in ``config.py``.
- ``environment.py``: a :class:`croi.environment.ObjectiveEnvironment` over its ``configs/``.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol

from croi.io.datasets import DetectionDataset

if TYPE_CHECKING:
    from clinicalomx.vocab import Vocabulary

    from croi.io.registries import ChampionRegistry

Config = Mapping[str, Any]


class ObjectiveVocabulary(Protocol):
    """An objective's ``vocab`` module: the features it owns and the ROI scalars stages write."""

    VOCAB: Vocabulary
    ROI_SCALARS: dict[str, type]


@dataclass(frozen=True)
class DetectionObjective:
    """One detection objective, as the shared layer reads it.

    ``name`` is its directory. ``lamin_key_prefix`` is the prefix its instance keys use.
    """

    name: str
    lamin_key_prefix: str
    registry: ChampionRegistry
    dataset: type[DetectionDataset]

    def load_dataset(self, config: Config) -> DetectionDataset:
        """The objective's dataset over a train stage config's ``inputs``."""
        return self.dataset.from_config(config)

    def vocabulary(self) -> ObjectiveVocabulary:
        """The objective's ``vocab`` module, imported on demand because it imports lamindb."""
        return import_module(f"croi.objectives.{self.name}.vocab")
