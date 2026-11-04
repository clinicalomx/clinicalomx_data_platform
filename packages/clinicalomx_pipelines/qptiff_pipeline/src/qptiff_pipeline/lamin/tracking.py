"""Opening a lamindb Run for a stage, tied to the Prefect flow run that drives it."""
from __future__ import annotations


def track_prefect_run(stage_key: str):
    """Open a lamindb Run for this stage, linked to the current Prefect flow run.

    Call once at the start of a stage flow and pair it with ``ln.finish()``. Every Artifact saved in
    between attaches to the Run. On the HPC, the run also cites the registered SIF it runs in.
    """
    import lamindb as ln
    from clinicalomx.environments import link_apptainer_to_run
    from prefect.runtime import flow_run

    from qptiff_pipeline.domain import states
    from qptiff_pipeline.lamin.registry import get_or_create_project

    # Ensure the pipeline Project exists before ln.track links to it by name.
    project = get_or_create_project(states.PROJECT_NAME, states.PROJECT_DESCRIPTION)
    transform = (
        ln.Transform.filter(key=stage_key, is_latest=True).first()
        or ln.Transform(key=stage_key, type="pipeline").save()
    )
    ln.track(
        transform=transform,
        project=project.name,
        params={
            "prefect_run_id": str(flow_run.id or ""),
            "prefect_run_name": flow_run.name or "",
        },
    )
    # After ln.track, because it needs ln.context.run to exist.
    link_apptainer_to_run()

