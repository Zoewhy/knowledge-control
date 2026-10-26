"""LangGraph 版 Agent Runtime 编排。

把原先手写的 while 循环表达为 StateGraph：

    START -> prepare -> step ──(条件边)──> step / finalize / END(待审批)

审批门仍由 ``approve_medium_risk`` 驱动，DB（AgentTask.step_index + ToolCallRecord）
是恢复/重放的唯一可信源，因此不启用 LangGraph checkpointer / interrupt（跨 Celery
进程无法依赖内存状态）。
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..models import AgentTask, TaskStatus, ToolCallRecord, utcnow
from .agent_runtime import _event, _save_artifact, registry

CONTINUE = "continue"
APPROVAL = "approval"
DONE = "done"


class AgentState(TypedDict, total=False):
    outputs: list[dict[str, Any]]
    next_action: str


def _build_graph(db, task: AgentTask, approve_medium_risk: bool, ctx: dict[str, Any]):
    def prepare(state: AgentState) -> dict[str, Any]:
        task.status = TaskStatus.running
        task.error = None
        task.error_detail = {}
        _event(db, task, "task_started", {"resume_from": task.step_index})
        db.commit()
        return {"outputs": list(state.get("outputs", [])), "next_action": CONTINUE}

    def execute_step(state: AgentState) -> dict[str, Any]:
        outputs = list(state.get("outputs", []))
        if task.step_index >= len(task.plan):
            return {"outputs": outputs, "next_action": DONE}

        current_step = task.step_index
        ctx["current_step"] = current_step
        step = task.plan[task.step_index]
        tool = registry.get(step["tool"])
        ctx["current_tool"] = tool.name

        existing = next(
            (
                call
                for call in task.tool_calls
                if call.step_index == task.step_index and call.status == "succeeded"
            ),
            None,
        )
        if existing:
            outputs.append(existing.output)
            task.step_index += 1
            _event(db, task, "step_skipped", {"tool": tool.name}, current_step)
            db.commit()
            return {"outputs": outputs, "next_action": CONTINUE}

        if tool.risk_level in {"medium", "high"} and not approve_medium_risk:
            task.status = TaskStatus.waiting_approval
            _event(db, task, "approval_required", {"tool": tool.name}, current_step)
            db.commit()
            return {"outputs": outputs, "next_action": APPROVAL}

        record = ToolCallRecord(
            task_id=task.id,
            step_index=task.step_index,
            tool_name=tool.name,
            arguments={**step.get("arguments", {}), "task_id": task.id},
            risk_level=tool.risk_level,
            status="running",
        )
        ctx["current_record"] = record
        db.add(record)
        db.flush()
        _event(db, task, "step_started", {"tool": tool.name}, current_step)
        db.commit()

        output = tool.handler(db, record.arguments)
        record.output = output
        record.status = "succeeded"
        record.ended_at = utcnow()
        outputs.append(output)
        _save_artifact(db, task, record, output)
        task.step_index += 1
        _event(db, task, "step_succeeded", {"tool": tool.name}, current_step)
        db.commit()
        return {"outputs": outputs, "next_action": CONTINUE}

    def finalize(state: AgentState) -> dict[str, Any]:
        outputs = list(state.get("outputs", []))
        task.result = {"steps": outputs, "final": outputs[-1] if outputs else {}}
        task.status = TaskStatus.succeeded
        task.error = None
        task.error_detail = {}
        _event(db, task, "task_succeeded", {"steps": len(outputs)})
        db.commit()
        return {"outputs": outputs, "next_action": DONE}

    def route(state: AgentState) -> str:
        action = state.get("next_action", DONE)
        if action == CONTINUE:
            return "step"
        if action == DONE:
            return "finalize"
        return END

    builder = StateGraph(AgentState)
    builder.add_node("prepare", prepare)
    builder.add_node("step", execute_step)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "step")
    builder.add_conditional_edges(
        "step", route, {"step": "step", "finalize": "finalize", END: END}
    )
    builder.add_edge("finalize", END)
    return builder.compile()


def run_plan(
    db, task: AgentTask, approve_medium_risk: bool = False
) -> AgentTask:
    """执行（或从 step_index 恢复）任务计划，返回刷新后的任务。"""
    # 与原实现一致：计划校验失败直接抛出，不写入失败状态
    registry.validate_plan(task.plan)
    ctx: dict[str, Any] = {
        "current_step": task.step_index,
        "current_tool": None,
        "current_record": None,
    }
    graph = _build_graph(db, task, approve_medium_risk, ctx)
    try:
        graph.invoke({"outputs": [], "next_action": CONTINUE})
    except Exception as exc:  # noqa: BLE001 - 与既有失败诊断语义保持一致
        task.status = TaskStatus.failed
        task.error = str(exc)
        task.error_detail = {
            "type": type(exc).__name__,
            "message": str(exc),
            "step_index": ctx["current_step"],
            "tool": ctx["current_tool"],
            "retryable": True,
        }
        record = ctx["current_record"]
        if record is not None and record.status == "running":
            record.status = "failed"
            record.ended_at = utcnow()
            record.output = task.error_detail
        _event(db, task, "task_failed", task.error_detail, ctx["current_step"])
    db.commit()
    db.refresh(task)
    return task
