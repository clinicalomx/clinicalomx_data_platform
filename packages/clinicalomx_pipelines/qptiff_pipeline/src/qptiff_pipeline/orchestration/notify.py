"""Operator alerts to Microsoft Teams, as an Adaptive Card through an incoming webhook.

Best effort: a failure is logged and never raised.

The webhook URL lives in a Prefect ``MicrosoftTeamsWebhook`` block. This module reads the URL and
posts the card itself; the block's own ``.notify()`` sends a format Teams Workflows rejects.

Set up once (the URL is a secret, never in git):
    TEAMS_WEBHOOK_URL=<incoming-webhook-url> \\
      <vm-process venv>/bin/python -m qptiff_pipeline.setup_teams_webhook --test
"""
from __future__ import annotations

#: Prefect MicrosoftTeamsWebhook block holding the operator-alert webhook.
TEAMS_BLOCK = "clinicalomx-teams"


def _adaptive_card(subject: str, body: str) -> dict:
    """A minimal Adaptive Card in the ``attachments`` envelope the Teams Workflows webhook needs."""
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": subject,
                            "weight": "Bolder",
                            "size": "Large",
                            "wrap": True,
                        },
                        {"type": "TextBlock", "text": body, "wrap": True},
                    ],
                },
            }
        ],
    }


def notify_teams(subject: str, body: str, *, block_name: str | None = None) -> bool:
    """POST an Adaptive Card to Teams using the webhook URL in the block ``block_name``.

    Returns True on a 2xx response, False on any failure (logged). Never raises.
    """
    name = block_name or TEAMS_BLOCK
    try:
        import httpx
        from prefect.blocks.notifications import MicrosoftTeamsWebhook

        url = MicrosoftTeamsWebhook.load(name).url.get_secret_value()
        resp = httpx.post(url, json=_adaptive_card(subject, body), timeout=30)
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 - missing block / network / non-2xx must not crash the flow
        try:
            from prefect import get_run_logger

            get_run_logger().warning(f"Teams notify failed (block {name!r}): {exc!r}")
        except Exception:  # noqa: BLE001 - no Prefect context; fall back to stdout
            print(f"Teams notify failed (block {name!r}): {exc!r}", flush=True)
        return False
