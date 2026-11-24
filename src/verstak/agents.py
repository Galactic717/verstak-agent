from typing import Literal, TypedDict, cast
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from verstak.llm import InvalidModelOutput, PermanentModelError, Router, Triage
from verstak.store import RunLimit, apply_case, reserve_attempt, saved_triage, settle_attempt


class WorkflowState(TypedDict, total=False):
    run_id: str
    tenant: str
    text: str
    context: str
    triage: dict[str, object]
    approved: bool
    outcome: Literal["succeeded", "rejected", "failed"]
    error: str


def build_graph(
    saver: BaseCheckpointSaver[str], model: Router
) -> CompiledStateGraph[WorkflowState, None, WorkflowState, WorkflowState]:
    async def classify(state: WorkflowState) -> WorkflowState:
        run_id = UUID(state["run_id"])
        if saved := await saved_triage(run_id):
            return {"triage": saved}
        text = state["text"]
        context = state["context"]
        feedback = ""
        while True:
            try:
                reservation = model.max_cost_micros(text, context, feedback)
                sequence = await reserve_attempt(run_id, reservation)
            except RunLimit as exc:
                code = str(exc)
                if code == "step_limit" and feedback:
                    code = "invalid_model_output"
                return {"outcome": "failed", "error": code}
            try:
                result = await model.classify(text, context, feedback)
            except InvalidModelOutput:
                feedback = (
                    "Return a single valid JSON object matching the schema and category/tool pair."
                )
                continue
            except PermanentModelError:
                return {"outcome": "failed", "error": "provider_rejected"}
            triage = result.triage.model_dump(mode="json")
            within_budget = await settle_attempt(
                run_id,
                sequence,
                result.cost_micros,
                result.input_tokens,
                result.output_tokens,
                triage,
            )
            if not within_budget:
                return {"outcome": "failed", "error": "budget_limit"}
            return {"triage": triage}

    def approval(state: WorkflowState) -> WorkflowState:
        approved = interrupt({"run_id": state["run_id"], "triage": state["triage"]})
        if not isinstance(approved, bool):
            raise ValueError("approval must be a boolean")
        return {"approved": approved}

    async def act(state: WorkflowState) -> WorkflowState:
        if not state["approved"]:
            return {"outcome": "rejected"}
        triage = Triage.model_validate(state["triage"])
        await apply_case(UUID(state["run_id"]), state["tenant"], triage.tool, state["triage"])
        return {"outcome": "succeeded"}

    def route_after_classification(state: WorkflowState) -> Literal["approval", "end"]:
        return "end" if state.get("outcome") == "failed" else "approval"

    graph = StateGraph(WorkflowState)
    graph.add_node("classify", classify)
    graph.add_node("approval", approval)
    graph.add_node("act", act)
    graph.add_edge(START, "classify")
    graph.add_conditional_edges(
        "classify", route_after_classification, {"approval": "approval", "end": END}
    )
    graph.add_edge("approval", "act")
    graph.add_edge("act", END)
    return graph.compile(checkpointer=saver)


def checkpoint_state(value: object) -> WorkflowState:
    if not isinstance(value, dict):
        raise TypeError("expected workflow checkpoint state")
    return cast(WorkflowState, value)
