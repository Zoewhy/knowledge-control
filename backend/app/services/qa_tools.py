from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AgentTask, QaRequirement, QaTestCase, QaTestSuite
from .llm import llm
from .retrieval import hybrid_search


class ParsedRequirement(BaseModel):
    goals: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class TestPoint(BaseModel):
    name: str
    type: str = "functional"
    priority: str = "P1"
    rationale: str = ""


class TestPointDrafts(BaseModel):
    testpoints: list[TestPoint] = Field(default_factory=list)


class CaseDraft(BaseModel):
    name: str
    precondition: str = ""
    steps: list[str] = Field(default_factory=list)
    expected_result: str = ""
    priority: str = "P2"


class CaseDrafts(BaseModel):
    cases: list[CaseDraft] = Field(default_factory=list)


_DOMAIN_RULES: list[tuple[str, list[tuple[str, str]]]] = [
    ("登录", [("登录主流程", "functional"), ("错误凭证与锁定策略", "exception"),
              ("会话过期处理", "boundary")]),
    ("权限", [("越权访问拦截", "security"), ("角色权限矩阵覆盖", "functional")]),
    ("导入", [("批量导入主流程", "functional"), ("非法文件与超大文件", "exception"),
              ("重复数据幂等", "boundary")]),
    ("导出", [("导出数据完整性", "functional"), ("大数据量导出性能", "performance")]),
    ("告警", [("告警触发阈值", "boundary"), ("告警通知触达", "functional"),
              ("重复告警去重", "exception")]),
    ("支付", [("支付主流程", "functional"), ("重复支付幂等", "exception"),
              ("支付超时回滚", "exception")]),
    ("搜索", [("搜索相关性", "functional"), ("空结果与无命中", "boundary")]),
    ("召回", [("召回率回归", "functional"), ("索引更新延迟", "exception")]),
    ("接口", [("接口契约校验", "functional"), ("接口超时与重试", "exception"),
              ("限流与降级", "performance")]),
    ("审核", [("审核通过与拒绝流转", "functional"), ("并发审核冲突", "exception")]),
]


def _get_requirement(db: Session, requirement_id: str) -> QaRequirement:
    requirement = db.get(QaRequirement, requirement_id)
    if not requirement:
        raise ValueError(f"需求不存在: {requirement_id}")
    return requirement


def _get_suite(db: Session, requirement_id: str, task_id: str | None) -> QaTestSuite | None:
    query = select(QaTestSuite).where(QaTestSuite.requirement_id == requirement_id)
    if task_id:
        query = query.where(QaTestSuite.task_id == task_id)
    return db.scalars(query.order_by(QaTestSuite.created_at.desc())).first()


def _fallback_parsed(requirement: QaRequirement) -> dict[str, Any]:
    lines = [
        line.strip().lstrip("-#* ").strip()
        for line in requirement.content.splitlines()
        if line.strip()
    ]
    body = [line for line in lines if line and line != requirement.title]
    return ParsedRequirement(
        goals=[requirement.title],
        acceptance_criteria=body[:5] or [f"{requirement.title} 主流程可用"],
        constraints=["不得破坏现有已审核知识", "测试数据需可重复构造"],
        risks=["需求描述可能不完整，生成结果需人工确认"],
    ).model_dump()


def prd_parse(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    requirement = _get_requirement(db, str(args["requirement_id"]))
    fallback = _fallback_parsed(requirement)
    generated = llm.structured(
        "你是需求分析专家。从需求文本中抽取目标、验收标准、约束与风险。"
        "只返回 JSON：{goals:[...],acceptance_criteria:[...],constraints:[...],risks:[...]}。",
        f"标题：{requirement.title}\n需求：\n{requirement.content}",
        fallback,
    )
    try:
        parsed = ParsedRequirement.model_validate(generated).model_dump()
    except ValidationError:
        parsed = fallback
    requirement.parsed = parsed
    db.commit()
    return {"requirement_id": requirement.id, "title": requirement.title, "parsed": parsed}


def _fallback_testpoints(
    requirement: QaRequirement, parsed: dict[str, Any]
) -> list[dict[str, Any]]:
    text = f"{requirement.title}\n{requirement.content}"
    points: list[TestPoint] = []
    for keyword, rules in _DOMAIN_RULES:
        if keyword in text:
            points.extend(
                TestPoint(name=name, type=kind, rationale=f"需求涉及「{keyword}」")
                for name, kind in rules
            )
    points.append(
        TestPoint(
            name=f"{requirement.title} 主流程",
            type="functional",
            priority="P0",
            rationale="需求主链路",
        )
    )
    points.append(
        TestPoint(
            name=f"{requirement.title} 异常输入",
            type="exception",
            priority="P1",
            rationale="非法与边界输入",
        )
    )
    for criterion in (parsed.get("acceptance_criteria") or [])[:3]:
        points.append(
            TestPoint(
                name=f"验收：{criterion[:60]}",
                type="acceptance",
                priority="P0",
                rationale="验收标准",
            )
        )
    seen: set[str] = set()
    unique = []
    for point in points:
        if point.name not in seen:
            seen.add(point.name)
            unique.append(point.model_dump())
    return unique


def testpoint_generate(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    requirement = _get_requirement(db, str(args["requirement_id"]))
    task_id = args.get("task_id")
    parsed = requirement.parsed or _fallback_parsed(requirement)
    fallback = _fallback_testpoints(requirement, parsed)
    generated = llm.structured(
        "你是测试设计专家。根据需求与解析结果设计测试点，覆盖功能、边界、异常和验收标准。"
        "只返回 JSON：{testpoints:[{name,type,priority,rationale}]}。",
        f"标题：{requirement.title}\n需求：{requirement.content}\n解析：{parsed}",
        {"testpoints": fallback},
    )
    try:
        testpoints = TestPointDrafts.model_validate(generated).model_dump()["testpoints"]
        if not testpoints:
            raise ValidationError.from_exception_data("TestPointDrafts", [])
    except ValidationError:
        testpoints = fallback
    suite = _get_suite(db, requirement.id, task_id)
    if not suite:
        suite = QaTestSuite(
            requirement_id=requirement.id,
            task_id=task_id,
            name=f"{requirement.title} 测试套件",
        )
        db.add(suite)
        db.flush()
    suite.testpoints = testpoints
    suite.status = "testpoints_generated"
    db.commit()
    return {"requirement_id": requirement.id, "suite_id": suite.id, "testpoints": testpoints}


def _fallback_cases(
    requirement: QaRequirement, testpoints: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    cases = []
    for point in testpoints:
        name = point.get("name", "未命名测试点")
        cases.append(
            CaseDraft(
                name=f"{name}验证",
                precondition="被测环境可用，测试数据已按需求准备",
                steps=[
                    f"准备与「{name}」相关的输入数据",
                    f"执行「{name}」对应操作",
                    "检查响应结果与系统副作用",
                ],
                expected_result=f"「{name}」行为符合需求「{requirement.title}」的描述",
                priority=point.get("priority", "P2"),
            ).model_dump()
        )
    return cases


def _find_suite(db: Session, requirement_id: str, task_id: Any) -> QaTestSuite | None:
    suite = _get_suite(db, requirement_id, str(task_id) if task_id else None)
    return suite or _get_suite(db, requirement_id, None)


def testcase_generate(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    requirement = _get_requirement(db, str(args["requirement_id"]))
    suite = _find_suite(db, requirement.id, args.get("task_id"))
    if not suite or not suite.testpoints:
        raise ValueError("缺少测试点，请先执行 testpoint_generate")
    if suite.cases:
        return {
            "suite_id": suite.id,
            "case_count": len(suite.cases),
            "cases": [case.name for case in suite.cases],
            "reused": True,
        }
    knowledge = hybrid_search(db, requirement.title, top_k=3)
    fallback = _fallback_cases(requirement, suite.testpoints)
    generated = llm.structured(
        "你是测试设计专家。为每个测试点生成可执行的结构化测试用例。"
        "只返回 JSON：{cases:[{name,precondition,steps:[...],expected_result,priority}]}。",
        f"需求：{requirement.title}\n{requirement.content}\n测试点：{suite.testpoints}\n"
        f"知识上下文：{[hit.content for hit in knowledge.hits]}",
        {"cases": fallback},
    )
    try:
        cases = CaseDrafts.model_validate(generated).model_dump()["cases"]
        if not cases:
            raise ValidationError.from_exception_data("CaseDrafts", [])
    except ValidationError:
        cases = fallback
    for case in cases:
        db.add(
            QaTestCase(
                suite_id=suite.id,
                name=str(case.get("name") or "未命名用例")[:300],
                precondition=str(case.get("precondition") or ""),
                steps=[str(step) for step in case.get("steps") or []],
                expected_result=str(case.get("expected_result") or ""),
                priority=str(case.get("priority") or "P2")[:10],
            )
        )
    suite.status = "cases_generated"
    db.commit()
    return {"suite_id": suite.id, "case_count": len(cases), "reused": False}


def coverage_review(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    requirement = _get_requirement(db, str(args["requirement_id"]))
    suite = _find_suite(db, requirement.id, args.get("task_id"))
    if not suite:
        raise ValueError("测试套件不存在")
    cases = list(suite.cases)
    normalized = [case.name.strip().lower() for case in cases]
    duplicates = sorted({name for name in normalized if normalized.count(name) > 1})
    gaps = []
    for point in suite.testpoints:
        pname = str(point.get("name", ""))
        if not any(pname and pname in case.name for case in cases):
            gaps.append(pname)
    total = len(suite.testpoints)
    covered = total - len(gaps)
    summary = {
        "testpoint_count": total,
        "case_count": len(cases),
        "covered_testpoints": covered,
        "coverage": round(covered / total, 4) if total else 0.0,
        "gaps": gaps,
        "duplicates": duplicates,
    }
    suite.summary = summary
    suite.status = "reviewed"
    db.commit()
    return {"suite_id": suite.id, **summary}


def build_suite_report(db: Session, suite: QaTestSuite) -> str:
    requirement = db.get(QaRequirement, suite.requirement_id)
    title = requirement.title if requirement else suite.name
    summary = suite.summary or {}
    lines = [
        f"# {title} 测试报告",
        "",
        f"- 套件：{suite.name}",
        f"- 状态：{suite.status}",
        f"- 测试点：{summary.get('testpoint_count', len(suite.testpoints))}",
        f"- 用例数：{summary.get('case_count', len(suite.cases))}",
        f"- 覆盖率：{summary.get('coverage', 0)}",
        "",
        "## 测试点",
    ]
    for point in suite.testpoints:
        name = point.get("name")
        lines.append(
            f"- [{point.get('priority', 'P2')}] {name}（{point.get('type', 'functional')}）"
        )
    if summary.get("gaps"):
        lines.extend(["", "## 覆盖缺口"])
        lines.extend(f"- {gap}" for gap in summary["gaps"])
    lines.extend(["", "## 用例明细"])
    for case in suite.cases:
        lines.extend(
            ["", f"### {case.name}", f"优先级：{case.priority}", f"前置条件：{case.precondition}"]
        )
        lines.extend(f"{index}. {step}" for index, step in enumerate(case.steps, start=1))
        lines.append(f"预期结果：{case.expected_result}")
    return "\n".join(lines)


def report_generate(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    requirement = _get_requirement(db, str(args["requirement_id"]))
    suite = _find_suite(db, requirement.id, args.get("task_id"))
    if not suite:
        raise ValueError("测试套件不存在")
    markdown = build_suite_report(db, suite)
    suite.status = "reported"
    db.commit()
    return {
        "suite_id": suite.id,
        "markdown": markdown,
        "case_count": len(suite.cases),
        "summary": f"{suite.name}：{len(suite.cases)} 条用例",
    }


def task_notify_message(db: Session, task_id: str) -> str:
    task = db.get(AgentTask, task_id)
    if not task:
        return "Agent 任务不存在"
    return f"任务 {task.id} 状态 {task.status.value}：{task.instruction[:80]}"
