"""The Streamlit review app: the human decision at each pipeline gate.

It runs on the hub VM, which cannot read HPC storage, so everything it shows arrives as Prefect
artifacts under the keys in ``domain/hitl.py``. The dearray falls back in order: ``tma_gridding``
fits a lattice, ``grid_graph`` walks a neighbour graph, and ``grid_links`` takes the reviewer's
stated adjacency.
"""
