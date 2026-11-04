"""Extract Explorer: a Reflex UI over the extract layer. Needs ``clinicalomx[app]``.

    ./run_extract_app.sh          # http://localhost:8601

`web.make_app` builds it, `web.view` draws it, `web.state` holds the user's choices, `web.cache`
holds heavy objects, and `web.tokens` decides appearance. Demo mode feeds a pre-populated Snapshot
from `demo.build_snapshot` to the real `describe` and `plan`.
"""
