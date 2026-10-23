from celery import Celery

from .config import settings
from .database import SessionLocal
from .models import AgentTask
from .services.agent_runtime import execute_task

celery_app = Celery("alg_qa_agent", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(task_serializer="json", result_serializer="json", accept_content=["json"])


@celery_app.task(name="agent.execute")
def execute_agent_task(task_id: str, approve_medium_risk: bool = False) -> dict:
    with SessionLocal() as db:
        task = db.get(AgentTask, task_id)
        if not task:
            return {"error": "task not found"}
        task = execute_task(db, task, approve_medium_risk)
        return {"id": task.id, "status": task.status.value, "result": task.result}
