"""TMA core detection: find the cores of a TMA and fit the grid they sit on.

Data stages: ``labels``, ``split``. ``gridding`` and ``dearray`` turn detected cores into the
full grid; ``extract`` is the offline dearray batch. ``recover_annotated_images`` rebuilds the
Collection ``labels`` reads.
"""
