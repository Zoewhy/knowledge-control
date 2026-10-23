import os
import sys
import time

import httpx


def main() -> int:
    base = os.getenv("SMOKE_BASE_URL", "http://localhost:8000/api/v1")
    write_checks = os.getenv("SMOKE_WRITE", "false").lower() == "true"
    checks = ["/health", "/dashboard", "/wiki", "/tools", "/reviews", "/evaluations/runs"]
    with httpx.Client(timeout=20) as client:
        for path in checks:
            response = client.get(f"{base}{path}")
            response.raise_for_status()
            print(f"PASS GET {path} {response.status_code}")
        search = client.post(
            f"{base}/search",
            json={"query": "算法质量如何归因", "include_answer": False},
        )
        search.raise_for_status()
        diagnostics = search.json()["diagnostics"]
        assert "branches" in diagnostics and "trace_id" in diagnostics
        print(f"PASS /search hits={len(search.json()['hits'])}")
        if not write_checks:
            return 0

        marker = int(time.time())
        document = client.post(
            f"{base}/documents",
            json={
                "title": f"Smoke 验收规则 {marker}",
                "content": "# Smoke 验收\n知识接入、审核和检索必须保留来源。",
                "owner": "smoke",
            },
        )
        document.raise_for_status()
        task = client.post(
            f"{base}/agent/tasks",
            json={"instruction": "为 Smoke 验收设计测试用例", "auto_run": True},
        )
        task.raise_for_status()
        task = task.json()
        if task["status"] == "waiting_approval":
            approved = client.post(f"{base}/agent/tasks/{task['id']}/approve")
            approved.raise_for_status()
            task = approved.json()
        assert task["status"] == "succeeded", task
        assert task["artifacts"], "agent artifact missing"
        reflow = client.post(f"{base}/agent/artifacts/{task['artifacts'][0]['id']}/reflow")
        reflow.raise_for_status()
        run = client.post(f"{base}/evaluations/runs")
        run.raise_for_status()
        metrics = run.json()["metrics"]
        assert "wiki_hit" in metrics and "p95_latency_ms" in metrics
        print("PASS write smoke: document, agent, artifact reflow, evaluation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
