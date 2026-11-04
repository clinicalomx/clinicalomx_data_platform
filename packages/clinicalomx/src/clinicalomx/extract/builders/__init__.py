"""The builders, which read and write bytes. Needs ``clinicalomx[extract]``.

Never imported at module scope outside this package; ``assemble`` imports one by its dotted
``Recipe.builder`` path. Every builder has the signature::

    build(unit, *, recipe, placement, snapshot, **kw) -> str

It takes one :class:`~clinicalomx.extract.plan.Unit` and returns a one-line detail. It raises
rather than write a degraded output; ``assemble`` catches per unit.
"""
