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


@celery_app.task(name="ingest.execute")
def execute_ingest_task(document_id: str) -> dict:
    from .services.ingestion import run_ingest_pipeline

    with SessionLocal() as db:
        document = run_ingest_pipeline(db, document_id)
        return {"id": document.id, "status": document.status}


@celery_app.task(name="experiment.execute_run")
def execute_experiment_run_task(run_id: str) -> dict:
    from .services.reprocess import execute_experiment_run

    with SessionLocal() as db:
        run = execute_experiment_run(db, run_id)
        return {"id": run.id, "status": run.status}
