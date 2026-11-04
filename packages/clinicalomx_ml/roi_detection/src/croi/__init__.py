"""``croi``: ROI detection, as a shared layer and the objectives that implement it.

Nothing outside ``objectives/<obj>/`` names an objective.

- ``io/``           the dataset template, COCO schema, objective record, registries and services.
- ``objectives/``   one directory per objective.
- ``detection/``    the detector and trainer contracts, and one backend package per framework.
- ``evaluation/``   the scorer and its figures.
- ``utils/``        the ROI schema and pixel preparation.
- ``flows/``        the Prefect flow base class.
- ``config``        the stage config models and ``Subcommand``.
- ``environment``   the instance manifest, config lookup and stage preflight.
- ``train``         the train stage.
- ``schemas``       request, response and dataset-report models.
- ``feature_names`` feature names the shared layer writes.
- ``cli``           ``python -m croi <objective> <stage>``.
"""
