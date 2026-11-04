"""How a module declares the vocabulary it owns.

Nothing here may import lamindb. Importing lamindb connects to the instance.
"""
from __future__ import annotations

from typing import Any

#: Sentinel for an ``ln.Artifact``-valued feature, resolved in ``vocab.vocabulary()``.
ARTIFACT = "__artifact__"


class Declarer:
    """The vocabulary one module owns. Each declaring method returns the name it declared."""

    def __init__(self, module: str) -> None:
        #: The declaring module, for error messages.
        self.module = module
        #: ``(name, dtype-or-sentinel, description)`` in declaration order.
        self.features: list[tuple[str, Any, str]] = []
        #: ROI subtypes used as link-feature dtypes. Resolved to Records at ``ensure()`` time.
        self.roi_types: list[str] = []
        #: Non-ROI Record types, as ``(name, description)``. The ROI invariant checks skip these.
        self.record_types: list[tuple[str, str]] = []

    def feature(self, name: str, dtype: Any, description: str) -> str:
        """Declare a feature and return its name."""
        self.features.append((name, dtype, description))
        return name

    def link(self, name: str, roi_type: str, description: str) -> str:
        """Declare a feature whose value is an ROI Record of ``roi_type``."""
        if roi_type not in self.roi_types:
            self.roi_types.append(roi_type)
        return self.feature(name, roi_type, description)

    def record_type(self, name: str, description: str) -> str:
        """Declare a non-ROI Record type and return its name."""
        if not any(existing == name for existing, _ in self.record_types):
            self.record_types.append((name, description))
        return name

    def typed(self, name: str, record_type: str, description: str) -> str:
        """Declare a feature whose value is a Record of an already-declared non-ROI type."""
        return self.feature(name, record_type, description)
