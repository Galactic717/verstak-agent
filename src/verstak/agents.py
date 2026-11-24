from typing import Literal, TypedDict, cast
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from verstak.grounding import correction_prompt, review_proposal
from verstak.llm import InvalidModelOutput, PermanentModelError, Router, Triage
from verstak.observability import trace_span
from verstak.store import (
    RunLimit,
    apply_case,
    record_verification,
    reserve_attempt,
    saved_proposal,
    settle_attempt,
)


class WorkflowState(TypedDict, total=False):
    run_id: str
    tenant: str
    text: str
    context: str
    triage: dict[str, object]
    proposal_sequence: int | None
    correction: str
    approved: bool
    outcome: Literal["succeeded", "rejected", "failed"]
    error: str


def build_graph(
    saver: BaseCheckpointSaver[str], model: Router
) -> CompiledStateGraph[WorkflowState, None, WorkflowState, WorkflowState]:
    async def classify(state: WorkflowState) -> WorkflowState:
        run_id = UUID(state["run_id"])
        if saved := await saved_proposal(run_id):
            return {"triage": saved.fields, "proposal_sequence": saved.sequence}
        text = state["text"]
        context = state["context"]
        correction = state.get("correction", "")
        feedback = correction
        while True:
            try:
                reservation = model.max_cost_micros(text, context, feedback)
                sequence = await reserve_attempt(run_id, reservation)
            except RunLimit as exc:
                code = str(exc)
                if code == "step_limit":
                    if correction:
                        code = "ungrounded_model_output"
                    elif feedback:
                        code = "invalid_model_output"
                return {"outcome": "failed", "error": code}
            try:
                with trace_span("workflow.classify"):
                    result = await model.classify(text, context, feedback)
            except InvalidModelOutput:
                feedback = (
                    f"{correction} Return valid JSON matching the schema and tool pair.".strip()
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
            return {"triage": triage, "proposal_sequence": sequence}

    async def verify(state: WorkflowState) -> WorkflowState:
        fields = state["triage"]
        with trace_span("workflow.verify"):
            review = review_proposal(Triage.model_validate(fields), state["text"])
            await record_verification(
                UUID(state["run_id"]), state.get("proposal_sequence"), fields, review
            )
        return {"correction": correction_prompt(review)}

    def approval(state: WorkflowState) -> WorkflowState:
        approved = interrupt({"run_id": state["run_id"], "triage": state["triage"]})
        if not isinstance(approved, bool):
            raise ValueError("approval must be a boolean")
        return {"approved": approved}

    async def act(state: WorkflowState) -> WorkflowState:
        if not state["approved"]:
            return {"outcome": "rejected"}
        triage = Triage.model_validate(state["triage"])
        with trace_span("workflow.act"):
            await apply_case(UUID(state["run_id"]), state["tenant"], triage.tool, state["triage"])
        return {"outcome": "succeeded"}

    def route_after_classification(state: WorkflowState) -> Literal["verify", "end"]:
        return "end" if state.get("outcome") == "failed" else "verify"

    def route_after_verification(state: WorkflowState) -> Literal["classify", "approval"]:
        return "classify" if state["correction"] else "approval"

    graph = StateGraph(WorkflowState)
    graph.add_node("classify", classify)
    graph.add_node("verify", verify)
    graph.add_node("approval", approval)
    graph.add_node("act", act)
    graph.add_edge(START, "classify")
    graph.add_conditional_edges(
        "classify", route_after_classification, {"verify": "verify", "end": END}
    )
    graph.add_conditional_edges("verify", route_after_verification)
    graph.add_edge("approval", "act")
    graph.add_edge("act", END)
    return graph.compile(checkpointer=saver)


def checkpoint_state(value: object) -> WorkflowState:
    if not isinstance(value, dict):
        raise TypeError("expected workflow checkpoint state")
    return cast(WorkflowState, value)
