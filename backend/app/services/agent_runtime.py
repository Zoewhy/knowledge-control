from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy.orm import Session

from ..models import AgentArtifact, AgentEvent, AgentTask, TaskStatus, ToolCallRecord, utcnow
from .graph import query_graph
from .llm import llm
from .retrieval import hybrid_search


@dataclass
class ToolDefinition:
    name: str
    description: str
    risk_level: str
    handler: Callable[[Session, dict[str, Any]], dict[str, Any]]
    required_arguments: list[str] = field(default_factory=list)
    allowed_actions: set[str] = field(default_factory=lambda: {"execute"})


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition:
        if name not in self._tools:
            raise ValueError(f"tool not allowed: {name}")
        return self._tools[name]

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "name": item.name,
                "description": item.description,
                "risk_level": item.risk_level,
                "required_arguments": item.required_arguments,
                "allowed_actions": sorted(item.allowed_actions),
            }
            for item in self._tools.values()
        ]

    def validate_plan(self, plan: list[dict[str, Any]]) -> None:
        if not isinstance(plan, list) or not plan:
            raise ValueError("执行计划不能为空")
        for index, step in enumerate(plan):
            if not isinstance(step, dict) or not isinstance(step.get("tool"), str):
                raise ValueError(f"step {index} 缺少工具名称")
            tool = self.get(step["tool"])
            arguments = step.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError(f"step {index} 参数必须是对象")
            missing = [name for name in tool.required_arguments if name not in arguments]
            if missing:
                raise ValueError(f"step {index} 缺少参数: {', '.join(missing)}")
            action = step.get("action", "execute")
            if action not in tool.allowed_actions:
                raise ValueError(f"step {index} action 不在白名单: {action}")


registry = ToolRegistry()


def knowledge_search(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    response = hybrid_search(db, str(args["query"]), top_k=int(args.get("top_k", 6)))
    return response.model_dump(mode="json")


def graph_query(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    return {"paths": query_graph(db, str(args["query"]), limit=int(args.get("top_k", 10)))}


def autocase_agent_run(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    requirement = str(args["requirement"])
    context = hybrid_search(db, requirement, top_k=5)
    fallback = {
        "title": f"自动化用例：{requirement[:40]}",
        "preconditions": ["依赖服务可用", "测试数据已准备"],
        "cases": [
            {
                "name": f"{requirement[:60]}主流程验证",
                "steps": ["准备需求对应输入", "调用被测能力", "校验响应与副作用"],
                "priority": "P0",
            },
            {
                "name": f"{requirement[:60]}异常输入验证",
                "steps": ["构造非法或边界输入", "调用被测能力", "校验错误处理"],
                "priority": "P1",
            },
        ],
    }
    generated = llm.structured(
        "你是测试设计专家。根据需求和知识上下文设计可执行测试用例。"
        "只返回 JSON：{title,preconditions:[...],cases:[{name,steps:[...],priority}]}。",
        f"需求：{requirement}\n知识上下文：{[hit.content for hit in context.hits]}",
        fallback,
    )
    if not isinstance(generated.get("cases"), list) or not generated["cases"]:
        generated = fallback
    generated["knowledge_context"] = [hit.title for hit in context.hits]
    return generated


registry.register(
    ToolDefinition("knowledge_search", "可追溯知识问答", "low", knowledge_search, ["query"])
)
registry.register(
    ToolDefinition("rag_search", "混合检索原始上下文", "low", knowledge_search, ["query"])
)
registry.register(
    ToolDefinition("graph_query", "查询知识关系路径", "low", graph_query, ["query"])
)
registry.register(
    ToolDefinition(
        "autocase_agent_run",
        "根据需求生成测试用例",
        "medium",
        autocase_agent_run,
        ["requirement"],
    )
)


def make_plan(instruction: str) -> list[dict[str, Any]]:
    lowered = instruction.lower()
    if any(word in lowered for word in ["用例", "case", "测试"]):
        return [
            {
                "tool": "knowledge_search",
                "action": "execute",
                "arguments": {"query": instruction, "top_k": 5},
            },
            {
                "tool": "autocase_agent_run",
                "action": "execute",
                "arguments": {"requirement": instruction},
            },
        ]
    if any(word in lowered for word in ["关系", "归因", "graph"]):
        return [
            {
                "tool": "graph_query",
                "action": "execute",
                "arguments": {"query": instruction, "top_k": 10},
            }
        ]
    return [
        {
            "tool": "knowledge_search",
            "action": "execute",
            "arguments": {"query": instruction, "top_k": 6},
        }
    ]


def _event(
    db: Session, task: AgentTask, event_type: str, payload: dict[str, Any], step: int | None = None
) -> None:
    db.add(AgentEvent(task_id=task.id, step_index=step, event_type=event_type, payload=payload))


def create_task(db: Session, instruction: str, context: dict[str, Any]) -> AgentTask:
    plan = make_plan(instruction)
    registry.validate_plan(plan)
    task = AgentTask(instruction=instruction, context=context, plan=plan)
    db.add(task)
    db.flush()
    _event(db, task, "task_created", {"plan": plan})
    db.commit()
    db.refresh(task)
    return task


def _save_artifact(db: Session, task: AgentTask, record: ToolCallRecord, output: dict) -> None:
    if record.tool_name != "autocase_agent_run":
        return
    lines = [f"# {output.get('title') or '自动化测试用例'}", "", "## 前置条件"]
    lines.extend(f"- {item}" for item in output.get("preconditions", []))
    for case in output.get("cases", []):
        lines.extend(
            [
                "",
                f"## {case.get('name', '未命名用例')}",
                f"优先级：{case.get('priority', 'P2')}",
            ]
        )
        lines.extend(
            f"{index}. {step}" for index, step in enumerate(case.get("steps", []), start=1)
        )
    db.add(
        AgentArtifact(
            task_id=task.id,
            tool_call_id=record.id,
            name=str(output.get("title") or f"{record.tool_name}-{record.step_index}"),
            artifact_type="markdown",
            content="\n".join(lines),
            metadata_json={"tool": record.tool_name, "step_index": record.step_index},
        )
    )


def execute_task(db: Session, task: AgentTask, approve_medium_risk: bool = False) -> AgentTask:
    registry.validate_plan(task.plan)
    task.status = TaskStatus.running
    task.error = None
    task.error_detail = {}
    _event(db, task, "task_started", {"resume_from": task.step_index})
    db.commit()
    outputs: list[dict[str, Any]] = []
    current_step = task.step_index
    current_tool = None
    current_record: ToolCallRecord | None = None
    try:
        while task.step_index < len(task.plan):
            current_step = task.step_index
            step = task.plan[task.step_index]
            tool = registry.get(step["tool"])
            current_tool = tool.name
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
                continue
            if tool.risk_level in {"medium", "high"} and not approve_medium_risk:
                task.status = TaskStatus.waiting_approval
                _event(db, task, "approval_required", {"tool": tool.name}, current_step)
                db.commit()
                return task
            record = ToolCallRecord(
                task_id=task.id,
                step_index=task.step_index,
                tool_name=tool.name,
                arguments=step.get("arguments", {}),
                risk_level=tool.risk_level,
                status="running",
            )
            current_record = record
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
        task.result = {"steps": outputs, "final": outputs[-1] if outputs else {}}
        task.status = TaskStatus.succeeded
        task.error = None
        task.error_detail = {}
        _event(db, task, "task_succeeded", {"steps": len(outputs)})
    except Exception as exc:
        task.status = TaskStatus.failed
        task.error = str(exc)
        task.error_detail = {
            "type": type(exc).__name__,
            "message": str(exc),
            "step_index": current_step,
            "tool": current_tool,
            "retryable": True,
        }
        if current_record is not None and current_record.status == "running":
            current_record.status = "failed"
            current_record.ended_at = utcnow()
            current_record.output = task.error_detail
        _event(db, task, "task_failed", task.error_detail, current_step)
    db.commit()
    db.refresh(task)
    return task
