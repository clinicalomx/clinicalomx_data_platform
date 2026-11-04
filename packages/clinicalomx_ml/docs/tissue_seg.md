# Objective: `tissue_seg` (tissue segmentation) - not started

**This objective has no code.**
This page is the whole of it: no configs, no drivers, no Prefect deployment, and nothing runs.
It is a planned design, not an abandoned attempt.

## Planned shape

- Pixel or instance segmentation of tissue regions, instead of the rectangular boxes `general` produces.
- It reads `tissue_section` detections from the `general` objective, crops to each box, and segments inside it.
- That handoff is a pinned release, not a live input: it pins `{key, version}`, the way `core` pins `general`'s crops.
- Like the other objectives it builds on `clinicalomx` (manifest, `InputRef`, `materialize`, `ModelRef`, the ROI graph).
  From `croi` it reuses only what is not detection-specific: the ROI schema and the stage environment.

## To start it

1. Read `packages/clinicalomx_ml/docs/adding-an-objective.md`.
   It is the contract: the inference and train split, the key layout, the version pins and the release rules.
2. Create it at `roi_detection/src/croi/objectives/tissue_seg/`, copying the shape of either sibling in that folder.
