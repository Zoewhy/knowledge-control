"""消融实验 4 臂复跑脚本：依次运行 G0/G1/G2/G3 并打印整体指标。

用法：PYTHONPATH=backend python -m app.scripts.run_ablation
环境变量：DATABASE_URL / ENABLE_NEO4J / BAILIAN_API_KEY / LLM_MOCK_MODE
"""

from app.database import SessionLocal
from app.services.evaluation import run_evaluation

for arm in ["G0", "G1", "G2", "G3"]:
    db = SessionLocal()
    try:
        run = run_evaluation(db, arm=arm)
        m = run.metrics
        print(f"== {arm} run_id={run.id} pass_rate={m.get('pass_rate')} "
              f"avg_lat={m.get('avg_latency_ms')} p95={m.get('p95_latency_ms')}")
        for k in ("source_hit","wiki_hit","citation_validity","rag_no_hit"):
            print(f"   {k}: {m.get(k)}")
    finally:
        db.close()
print("DONE")
