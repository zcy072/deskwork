"""Identity passed explicitly to graph calls; never serialized into checkpoints."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RunContext:
    session_id: str
    owner: str
    run_id: str
    turn_id: str
    trace_id: str
    workspace_mode: str = "write"


def session_id_of(runtime) -> str:
    context = getattr(runtime, "context", None)
    if isinstance(context, RunContext):
        return context.session_id
    # Graphs built directly by tests/legacy integrations have no context schema.
    return runtime.config["configurable"]["thread_id"]
