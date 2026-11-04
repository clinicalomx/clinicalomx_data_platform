"""The Prefect rendering of the graph: Automations that start one deployment from another.

Register with
    PREFECT_API_URL=http://localhost:4200/api \\
      /home/ubuntu/clinicalomxdb/hub-pipelines/prefect/workers/vm-process/.venv/bin/python \\
      -m qptiff_pipeline.orchestration.automations

:class:`Edge` fires on a unit event and runs one deployment per unit uid. :class:`FlowEdge` fires
when a source deployment's flow run finishes, and runs the target with fixed parameters. The target
then finds its own work.

Event names come from :mod:`~qptiff_pipeline.domain.states`. ``tests/test_automations.py`` pins
every edge and its rendered Automation.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

from qptiff_pipeline.domain.states import ENTRY_EVENTS, GATES, SLIDE_RESET_EVENT


def _dep(flow_name: str) -> str:
    """Deployment reference ``<flow>/pipeline-<flow>``, as prefect.yaml names deployments."""
    return f"{flow_name}/pipeline-{flow_name}"


@dataclass(frozen=True)
class Edge:
    """One hop: ``on_event`` triggers ``deployment``.

    param:   deployment parameter that receives the unit uid (as a 1-list).
    uid_key: event payload key holding the unit's uid (gate emits ``record_uid``, general-persist
             emits ``tma_uid``).
    """

    name: str
    on_event: str
    deployment: str
    param: str
    uid_key: str = "record_uid"
    description: str = ""
    enabled: bool = True  # False -> registered but paused (kept declarative, not re-enabled on re-register)


# The pipeline graph. Add a stage by adding an Edge.
EDGES: list[Edge] = [
    # Paused. check-general-detect runs once per batch instead, through the
    # mint-slides-general-detect FlowEdge and a daily schedule. See flows/check_general_detect.py.
    Edge(
        name="slide-registered-general-detect",
        on_event=ENTRY_EVENTS["general"],  # clinical-roi.slide.registered
        deployment=_dep("check-general-detect"),
        param="slide_uids",
        description="(paused) registered WholeSlide -> check-general-detect; superseded by mint-completion + heartbeat",
        enabled=False,
    ),
    Edge(
        name="general-accepted-persist",
        on_event=GATES["general"].event,  # clinical-roi.general.accepted
        deployment=_dep("general-persist"),
        param="slide_uids",
        description="general accepted -> general-persist",
    ),
    # Core stage: mirrors general at the TMA level.
    # Each edge is skipped at register time until its deployment exists.
    Edge(
        name="tma-registered-core-detect",
        on_event=ENTRY_EVENTS["core"],  # clinical-roi.tma.registered (per TMA)
        deployment=_dep("core-detect"),
        param="tma_uids",
        uid_key="tma_uid",  # general-persist emits payload['tma_uid']
        description="registered TMA -> core-detect",
    ),
    Edge(
        name="core-accepted-persist",
        on_event=GATES["core"].event,  # clinical-roi.core.accepted (gate.decide emits record_uid=tma.uid)
        deployment=_dep("core-persist"),
        param="tma_uids",
        description="core accepted -> core-persist",
    ),
    # bgnorm fan-out is not an event edge. The bgnorm-runner launches the bgnorm-core jobs, caps how
    # many run at once, resubmits failures and advances the TMA itself.
    # Cascade reset: a general reopen (cascade=True) with downstream core work tears it down.
    Edge(
        name="slide-reset-teardown",
        on_event=SLIDE_RESET_EVENT,  # clinical-roi.slide.reset
        deployment=_dep("reset-slide"),
        param="slide_uids",
        uid_key="record_uid",  # reopen emits payload['record_uid'] = slide.uid
        description="reset requested runs reset-slide (teardown core/bgnorm, back to general_pending)",
    ),
]


@dataclass(frozen=True)
class FlowEdge:
    """One hop triggered when a source deployment's flow run finishes, not by a unit event.

    Fires once per finished run of ``source_deployment`` and runs ``deployment`` with fixed
    ``parameters``. The target scans for its own work.
    """

    name: str
    source_deployment: str
    deployment: str
    parameters: dict = field(default_factory=dict)
    description: str = ""
    on_state: str = "Completed"  # flow-run terminal state to fire on (Completed/Failed/Crashed)


# Flow-completion edges: when a source deployment finishes, run the target.
# Ingest chain: register (hash bytes) -> check (integrity gate) -> mint (state spine).
# check-qptiffs quarantines a corrupt file before detect sees it.
FLOW_EDGES: list[FlowEdge] = [
    FlowEdge(
        name="register-qptiffs-check-qptiffs",
        source_deployment=_dep("register-qptiffs"),
        deployment=_dep("check-qptiffs"),
        description="register-qptiffs completed -> check-qptiffs (integrity gate)",
    ),
    FlowEdge(
        name="check-qptiffs-mint-slides",
        source_deployment=_dep("check-qptiffs"),
        deployment=_dep("mint-slides"),
        description="check-qptiffs completed -> mint-slides (mint integrity-ok qptiffs)",
    ),
    # One gate run per ingestion batch. The gate starts general-detect only if registered slides
    # exist and none is in flight.
    FlowEdge(
        name="mint-slides-general-detect",
        source_deployment=_dep("mint-slides"),
        deployment=_dep("check-general-detect"),
        description="mint-slides completed -> check-general-detect (per-batch gate -> general-detect)",
    ),
    # Re-run the gate after each general-detect, for slides that registered while it ran. Stops on
    # its own once no registered slides remain.
    FlowEdge(
        name="general-detect-recheck",
        source_deployment=_dep("general-detect"),
        deployment=_dep("check-general-detect"),
        description="general-detect completed -> check-general-detect (sweep slides registered mid-run)",
    ),
    # Each persist completion rebuilds its stage's training-reference Collection. Idempotent. The
    # deployment's daily schedule catches a missed event.
    FlowEdge(
        name="general-persist-harvest",
        source_deployment=_dep("general-persist"),
        deployment=_dep("harvest-feedback"),
        parameters={"stage": "general", "dry_run": False},
        description="general-persist completed -> harvest-feedback (rebuild general training-reference Collection)",
    ),
    FlowEdge(
        name="core-persist-harvest",
        source_deployment=_dep("core-persist"),
        deployment=_dep("harvest-feedback"),
        parameters={"stage": "core", "dry_run": False},
        description="core-persist completed -> harvest-feedback (rebuild core training-reference Collection)",
    ),
    # bgnorm has no finalize FlowEdge: the bgnorm-runner tracks its jobs and advances the TMA.
]


def _automation(edge: Edge, deployment_id):
    """Build the Prefect Automation for one graph edge: one run per triggering unit."""
    from prefect.automations import Automation
    from prefect.events.actions import RunDeployment
    from prefect.events.schemas.automations import EventTrigger, Posture

    return Automation(
        name=edge.name,
        description=edge.description,
        enabled=edge.enabled,
        trigger=EventTrigger(
            expect={edge.on_event},
            posture=Posture.Reactive,
            threshold=1,
            within=timedelta(0),
            for_each={"prefect.resource.id"},
        ),
        actions=[
            RunDeployment(
                source="selected",
                deployment_id=deployment_id,
                parameters={edge.param: [f"{{{{ event.payload['{edge.uid_key}'] }}}}"]},
            )
        ],
    )


def _flow_automation(edge: FlowEdge, source_deployment_id, target_deployment_id):
    """Build the Automation for one flow-completion edge: run the target with fixed params."""
    from prefect.automations import Automation
    from prefect.events.actions import RunDeployment
    from prefect.events.schemas.automations import EventTrigger, Posture

    return Automation(
        name=edge.name,
        description=edge.description,
        enabled=True,
        trigger=EventTrigger(
            expect={f"prefect.flow-run.{edge.on_state}"},
            posture=Posture.Reactive,
            threshold=1,
            within=timedelta(0),
            # Only runs from the source deployment.
            match_related={
                "prefect.resource.role": "deployment",
                "prefect.resource.id": f"prefect.deployment.{source_deployment_id}",
            },
        ),
        actions=[
            RunDeployment(
                source="selected",
                deployment_id=target_deployment_id,
                parameters=edge.parameters,
            )
        ],
    )


async def _replace(client, automation) -> None:
    """Delete any existing automation of the same name, then create this one."""
    for a in await client.read_automations():
        if a.name == automation.name:
            await client.delete_automation(a.id)
    await automation.acreate()


async def main() -> None:
    from prefect.client.orchestration import get_client
    from prefect.exceptions import ObjectNotFound

    async with get_client() as client:
        for edge in EDGES:
            try:
                dep = await client.read_deployment_by_name(edge.deployment)
            except ObjectNotFound:
                print(f"[graph] skip {edge.name}: deployment {edge.deployment!r} not deployed yet")
                continue
            await _replace(client, _automation(edge, dep.id))
            print(f"[graph] {edge.name}: {edge.description}")

        for edge in FLOW_EDGES:
            try:
                src = await client.read_deployment_by_name(edge.source_deployment)
                tgt = await client.read_deployment_by_name(edge.deployment)
            except ObjectNotFound:
                print(
                    f"[graph] skip {edge.name}: {edge.source_deployment!r} or "
                    f"{edge.deployment!r} not deployed yet"
                )
                continue
            await _replace(client, _flow_automation(edge, src.id, tgt.id))
            print(f"[graph] {edge.name}: {edge.description}")


if __name__ == "__main__":
    asyncio.run(main())
