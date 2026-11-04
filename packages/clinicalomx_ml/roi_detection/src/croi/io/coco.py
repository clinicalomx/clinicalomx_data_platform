"""The COCO detection dataset every trainer receives: images, pixel boxes, categories.

Only images, boxes and categories are modelled. Models are strict and frozen, and refuse unknown
keys, non-finite numbers, unknown categories, repeated ids and category ids other than ``0..n-1``.
"""
from __future__ import annotations

from collections import Counter
from typing import Annotated

import pydantic as pdt

Id = Annotated[int, pdt.Field(ge=0)]
Pixels = Annotated[int, pdt.Field(gt=0)]


class CocoModel(pdt.BaseModel, strict=True, frozen=True, extra="forbid", allow_inf_nan=False):
    """Strict, frozen; refuses unknown keys and non-finite numbers."""


class Category(CocoModel):
    id: Id
    name: Annotated[str, pdt.Field(min_length=1)]


class Image(CocoModel):
    """One image. ``path`` locates its bytes and is not written to the COCO JSON."""

    id: Id
    file_name: Annotated[str, pdt.Field(min_length=1)]
    width: Pixels
    height: Pixels
    path: Annotated[str, pdt.Field(exclude=True)]


class Annotation(CocoModel):
    """One box: ``bbox`` is ``(x_min, y_min, width, height)`` in pixels of its image."""

    id: Id
    image_id: Id
    category_id: Id
    bbox: tuple[float, float, float, float]

    @pdt.field_validator("bbox")
    @classmethod
    def _extent_is_not_negative(cls, bbox: tuple[float, float, float, float]):
        if bbox[2] < 0 or bbox[3] < 0:
            raise ValueError(f"bbox {bbox} has a negative width or height")
        return bbox


class CocoDataset(CocoModel):
    """One split as one COCO file. A category id is its class index. Reports every problem."""

    images: tuple[Image, ...]
    annotations: tuple[Annotation, ...]
    categories: Annotated[tuple[Category, ...], pdt.Field(min_length=1)]

    @pdt.model_validator(mode="after")
    def _references_resolve(self) -> CocoDataset:
        problems = []
        ids = [c.id for c in self.categories]
        if ids != list(range(len(ids))):
            problems.append(f"category ids are {ids}, not 0..{len(ids) - 1}: "
                            "a category's id is its class index")
        problems += [f"image id {i} is used {n} times"
                     for i, n in Counter(i.id for i in self.images).items() if n > 1]
        problems += [f"file name {f} is used {n} times"
                     for f, n in Counter(i.file_name for i in self.images).items() if n > 1]
        problems += [f"annotation id {i} is used {n} times"
                     for i, n in Counter(a.id for a in self.annotations).items() if n > 1]

        images = {i.id: i.file_name for i in self.images}
        unknown: dict[str, set[int]] = {}
        for a in self.annotations:
            if a.image_id not in images:
                problems.append(f"annotation {a.id} is on image {a.image_id}, which does not exist")
            elif a.category_id not in ids:
                unknown.setdefault(images[a.image_id], set()).add(a.category_id)
        problems += [f"{name} names category {sorted(cats)}, not one of 0..{len(ids) - 1}"
                     for name, cats in sorted(unknown.items())]
        if problems:
            raise ValueError("\n".join(problems))
        return self


class CocoSplits(CocoModel):
    """What a trainer is handed: one :class:`CocoDataset` per split, over one category list."""

    train: CocoDataset
    val: CocoDataset
    test: CocoDataset

    @pdt.field_validator("train")
    @classmethod
    def _something_to_train_on(cls, train: CocoDataset) -> CocoDataset:
        if not train.images:
            raise ValueError("no labelled image: training cannot start")
        return train

    @pdt.model_validator(mode="after")
    def _one_taxonomy(self) -> CocoSplits:
        if not (self.train.categories == self.val.categories == self.test.categories):
            raise ValueError("train, val and test do not share one category list, so a class "
                             "index would mean different things in different splits")
        return self

    @property
    def categories(self) -> tuple[Category, ...]:
        return self.train.categories
