"""Dev-only scaffolding for working on the review app's UI without lamindb or Prefect.

The production app never imports this package. ``dev/app.py`` loads it (see ``dev_review_app.sh``).

``tests/test_review_dev_harness.py`` drives the real ``review/app.py`` through these fakes. A fake
feature must be declared before it takes a value, and the fakes accept every argument shape real
lamindb accepts.
"""
