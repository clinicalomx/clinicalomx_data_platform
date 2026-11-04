"""One-time setup: create or update the Prefect MicrosoftTeamsWebhook block for alerts.

The webhook URL is a secret. Pass it through the environment, never commit it:

    TEAMS_WEBHOOK_URL='https://...<your Teams incoming webhook / workflow URL>...' \\
      PREFECT_API_URL=http://localhost:4200/api \\
      <vm-process venv>/bin/python -m qptiff_pipeline.setup_teams_webhook

Get the URL in Teams from the channel's Workflows (or Connectors) incoming webhook. The block is
saved as ``notify.TEAMS_BLOCK``.
"""
from __future__ import annotations

import os
import sys


def main() -> None:
    from prefect.blocks.notifications import MicrosoftTeamsWebhook

    from qptiff_pipeline.orchestration.notify import TEAMS_BLOCK

    url = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
    if not url:
        print("ERROR: set TEAMS_WEBHOOK_URL in the environment", file=sys.stderr)
        raise SystemExit(2)

    MicrosoftTeamsWebhook(url=url).save(TEAMS_BLOCK, overwrite=True)
    print(f"saved MicrosoftTeamsWebhook block {TEAMS_BLOCK!r}")

    if "--test" in sys.argv:
        # Send through notify_teams, the Adaptive Card path the Workflows webhook accepts.
        from qptiff_pipeline.orchestration.notify import notify_teams

        ok = notify_teams(
            subject="[clinicalomx] webhook test",
            body="clinicalomx: Teams webhook configured (Adaptive Card test message).",
        )
        print("sent test message" if ok else "test message FAILED (see warning above)")


if __name__ == "__main__":
    main()
