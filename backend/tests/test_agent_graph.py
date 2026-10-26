from app.database import SessionLocal
from app.models import AgentTask, TaskStatus
from app.services.agent_graph import run_plan
from app.services.agent_runtime import ToolDefinition, registry


def _echo(db, args):
    return {"echo": args.get("value")}


def test_langgraph_runs_plan_and_records_state():
    registry.register(ToolDefinition("graph_echo", "echo", "low", _echo, ["value"]))
    try:
        with SessionLocal() as db:
            task = AgentTask(
                instruction="echo",
                plan=[{"tool": "graph_echo", "arguments": {"value": "hi"}}],
            )
            db.add(task)
            db.commit()

            run_plan(db, task)

            assert task.status == TaskStatus.succeeded
            assert task.step_index == 1
            assert task.result["final"] == {"echo": "hi"}
            assert task.tool_calls[0].status == "succeeded"
            event_types = {event.event_type for event in task.events}
            expected = {"task_started", "step_started", "step_succeeded", "task_succeeded"}
            assert expected <= event_types
            assert "approval_required" not in event_types
    finally:
        registry._tools.pop("graph_echo", None)


def test_langgraph_pauses_for_risky_tool_then_resumes():
    registry.register(ToolDefinition("graph_risky", "risky", "medium", _echo, ["value"]))
    try:
        with SessionLocal() as db:
            task = AgentTask(
                instruction="risky",
                plan=[{"tool": "graph_risky", "arguments": {"value": "x"}}],
            )
            db.add(task)
            db.commit()

            run_plan(db, task)
            assert task.status == TaskStatus.waiting_approval
            assert task.step_index == 0
            assert "approval_required" in {event.event_type for event in task.events}

            run_plan(db, task, approve_medium_risk=True)
            assert task.status == TaskStatus.succeeded
            assert task.step_index == 1
            assert task.result["final"] == {"echo": "x"}
    finally:
        registry._tools.pop("graph_risky", None)
