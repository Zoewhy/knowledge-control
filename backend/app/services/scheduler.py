from sqlalchemy.orm import Session

from ..config import settings
from ..models import AgentTask
from .agent_runtime import execute_task


def dispatch_task(db: Session, task: AgentTask, approve_medium_risk: bool = False) -> AgentTask:
    if settings.agent_execution_mode == "sync":
        return execute_task(db, task, approve_medium_risk)
    if settings.agent_execution_mode != "celery":
        raise ValueError(f"未知 Agent 执行模式: {settings.agent_execution_mode}")
    from ..worker import execute_agent_task

    execute_agent_task.delay(task.id, approve_medium_risk)
    return task
