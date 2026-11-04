"""The data layer: datasets, model registries, and the services a run starts.

Objectives implement the templates here; nothing here imports an objective.

- ``datasets``: the ``DetectionDataset`` template and its COCO form.
- ``readers``: config inputs to lamindb records, PNG sizes, Label Studio label rows.
- ``coco``: the COCO schema, the canonical training format.
- ``objectives``: the ``DetectionObjective`` record.
- ``registries``: model frameworks, champion resolution, alias promotion.
- ``services``: global contexts a run starts and stops.
"""
