"""Run the docstring examples of the modules listed below. No network, no database.

Add a module when it grows an example. Modules are listed instead of using
`--doctest-modules`, which would import `vocab.py`.
"""
from __future__ import annotations

import doctest

import pytest

from clinicalomx_markers.domain import imaging_channel as channel_model
from clinicalomx_markers.domain import interaction as interaction_model
from clinicalomx_markers.domain import marker as marker_model
from clinicalomx_markers.domain import reagent_mapping as mapping_model
from clinicalomx_markers.domain import target as target_model

DOCUMENTED = (marker_model, mapping_model, channel_model, target_model, interaction_model)


@pytest.mark.parametrize("module", DOCUMENTED, ids=lambda m: m.__name__)
def test_the_examples_still_work(module):
    failed, attempted = doctest.testmod(module, verbose=False)
    assert attempted, f"{module.__name__} is listed here but carries no example"
    assert failed == 0, f"{failed} of {attempted} examples in {module.__name__} failed"
