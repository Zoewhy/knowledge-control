"""Ablation evaluation question bank seed.

Assigns question_type to the legacy 25 benchmark cases and inserts ~70 ablation
cases (prefixed ``消融-``) covering types A-F. Idempotent: cases are upserted by
name. Document titles referenced in expected_sources come from
merchant_seed_data.py so the scorer can match real seeded data.
"""

from __future__ import annotations

import argparse
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import EvaluationCase

# question_type for the legacy 25 EVALUATION_CASES in merchant_seed_data.py,
# keyed by exact case name. A=事实型 B=流程型 C=关键词型 D=跨文档多跳型 E=归因排查型
EXISTING_CASE_TYPES = {
    "营业执照主体不一致": "B",
    "品牌授权要素": "A",
    "多级授权断链": "A",
    "跨页合同": "B",
    "机审门禁": "A",
    "驳回解释": "B",
    "退入驻卡单": "E",
    "退入驻对账": "D",
    "CRM 状态": "A",
    "ES 同步": "E",
    "发券幂等": "B",
    "邀请码并发": "A",
    "助手工具选择": "A",
    "Agent 补数": "B",
    "法规公示": "A",
    "执照 OCR 门禁": "A",
    "授权范围": "B",
    "合同页码证据": "A",
    "机审分层指标": "A",
    "驳回码追溯": "A",
    "退入驻前置条件": "A",
    "对账异常分类": "A",
    "发券对账": "B",
    "商标授权边界": "A",
    "法规生效日期": "A",
}

DOC_LICENSE = "公开法规摘要：市场主体登记与营业执照核验"
DOC_DISCLOSURE = "公开法规摘要：电子商务经营者信息公示义务"
DOC_TRADEMARK = "公开法规摘要：商标审核与授权材料边界"
SOP_ONBOARDING = "商家入驻主体一致性审核 SOP"
SOP_BRAND = "品牌方授权资质审核规则"
SOP_CHAIN = "经销商多级授权链路审核 SOP"
SOP_CONTRACT = "多模态合同解析与后处理规范"
SOP_CONFIDENCE = "AI 法务机审决策与置信度门禁"
SOP_REJECT = "资质驳回码与客服解释规范"
SOP_EXIT = "退入驻状态机与卡单处置 SOP"
SOP_DCHECK = "退入驻 DCheck 离线对账规则"
SOP_CRM = "招商 CRM 线索生命周期规则"
SOP_ES = "CRM MySQL 到 Elasticsearch 同步排障手册"
SOP_COUPON = "营销发券幂等与最终一致性规范"
SOP_INVITE = "邀请码预生成与分配治理"
SOP_ASSISTANT = "商家入驻智能助手工具调用规范"
SOP_BADCASE = "Agent Badcase 标注与错误驱动补数规范"

# (name, query, question_type, expected_sources); expected_sources are exact
# document titles produced by merchant_seed_data.build_documents().
ABLATION_CASES = [
    # A 事实型：单点事实，来自 SOP 与公开法规摘要
    ("消融-A01-执照成立日期", "营业执照上市场主体的成立日期以什么为准？", "A", [DOC_LICENSE]),
    ("消融-A02-电子执照效力", "电子营业执照与纸质营业执照的法律效力是否相同？", "A", [DOC_LICENSE]),
    ("消融-A03-机审提取字段", "营业执照机审应提取哪些关键字段？", "A", [DOC_LICENSE]),
    ("消融-A04-公示位置", "电子商务经营者应在什么位置公示营业执照信息？", "A", [DOC_DISCLOSURE]),
    ("消融-A05-终止经营", "经营者自行终止电子商务经营时需要提前公示什么？", "A", [DOC_DISCLOSURE]),
    ("消融-A06-商标证明边界", "商标注册信息能证明什么、不能证明什么？", "A", [DOC_TRADEMARK]),
    ("消融-A07-自动通过条件", "主体一致性审核的自动通过条件是什么？", "A", [SOP_ONBOARDING]),
    ("消融-A08-阻断情形", "机审中哪些情形会直接阻断而不是人工复核？", "A", [SOP_ONBOARDING]),
    ("消融-A09-授权要素清单", "品牌方授权资质审核需要识别哪些授权要素？", "A", [SOP_BRAND]),
    ("消融-A10-模糊表述风险", "授权范围使用“等”或“相关产品”表述时能否自动通过？", "A", [SOP_BRAND]),
    ("消融-A11-机审指标口径", "机审以什么为正例计算 Precision 和 Recall？", "A", [SOP_CONFIDENCE]),
    ("消融-A12-发券幂等键组成", "营销发券的幂等键由哪些字段组成？", "A", [SOP_COUPON]),
    ("消融-A13-发券对账异常类型", "营销发券对账的异常分为哪几类？", "A", [SOP_COUPON]),
    ("消融-A14-邀请码告警条件", "邀请码在什么情况下会触发告警？", "A", [SOP_INVITE]),
    ("消融-A15-Badcase错误分类", "Agent Badcase 分为哪些错误类型？", "A", [SOP_BADCASE]),
    # B 流程型：SOP 步骤与流程
    ("消融-B01-入驻复核证据", "入驻审核进入人工复核后需要保存哪些证据？", "B", [SOP_ONBOARDING]),
    ("消融-B02-多级授权审核流程", "经销商多级授权链路审核的完整流程是什么？", "B", [SOP_CHAIN]),
    ("消融-B03-授权异常处理", "多级授权链路缺少中间授权或存在环路时如何处理？", "B", [SOP_CHAIN]),
    ("消融-B04-合同解析流程", "多模态合同解析的处理流程是什么？", "B", [SOP_CONTRACT]),
    ("消融-B05-失败降级", "文件损坏、加密或页数超阈值时流程如何降级？", "B", [SOP_CONTRACT]),
    ("消融-B06-驳回后指引流程", "资质驳回后客服应如何指引商家补充材料？", "B", [SOP_REJECT]),
    ("消融-B07-退入驻检查顺序", "退入驻前置检查需要依次校验哪些阻断项？", "B", [SOP_EXIT]),
    ("消融-B08-卡单处置流程", "退入驻状态超过预计时长后的卡单处置流程是什么？", "B", [SOP_EXIT]),
    ("消融-B09-DCheck修复流程", "DCheck 对账发现高风险异常后的修复流程是什么？", "B", [SOP_DCHECK]),
    ("消融-B10-线索状态流转", "招商 CRM 线索从待分配到结束经历哪些状态流转？", "B", [SOP_CRM]),
    ("消融-B11-搜索缺失排查步骤", "CRM 搜索结果缺失时的排查步骤是什么？", "B", [SOP_ES]),
    ("消融-B12-发券回扫机制", "营销发券受理成功但结果未知的记录如何处理？", "B", [SOP_COUPON]),
    ("消融-B13-邀请码存量修复", "重复邀请码的存量修复流程是什么？", "B", [SOP_INVITE]),
    ("消融-B14-助手澄清策略", "商家入驻智能助手在什么情况下应该追问用户？", "B", [SOP_ASSISTANT]),
    ("消融-B15-补数生产流程", "新增 Agent Badcase 样本的数据生产流程是什么？", "B", [SOP_BADCASE]),
    # C 关键词型：单词或短语查询
    ("消融-C01-DCheck", "DCheck", "C", [SOP_DCHECK]),
    ("消融-C02-幂等键", "幂等键", "C", [SOP_COUPON]),
    ("消融-C03-邀请码", "邀请码", "C", [SOP_INVITE]),
    ("消融-C04-Elasticsearch", "Elasticsearch", "C", [SOP_ES]),
    ("消融-C05-驳回码", "驳回码", "C", [SOP_REJECT]),
    ("消融-C06-置信度门禁", "置信度门禁", "C", [SOP_CONFIDENCE]),
    ("消融-C07-授权链路", "授权链路", "C", [SOP_CHAIN]),
    ("消融-C08-状态机", "状态机", "C", [SOP_EXIT]),
    ("消融-C09-营业执照", "营业执照", "C", [DOC_LICENSE]),
    ("消融-C10-信息公示", "信息公示", "C", [DOC_DISCLOSURE]),
    # D 跨文档多跳型：需要 2 个以上文档/实体关系
    (
        "消融-D01-卡单与对账配合",
        "退入驻卡单排查中 DCheck 离线对账承担什么角色？",
        "D",
        [SOP_EXIT, SOP_DCHECK],
    ),
    (
        "消融-D02-CRM版本一致性",
        "CRM 线索在 MySQL 更新后搜索不可见，补偿写入如何避免旧事件覆盖新状态？",
        "D",
        [SOP_CRM, SOP_ES],
    ),
    (
        "消融-D03-幂等键与邀请码并发",
        "营销发券重试为什么必须复用幂等键，邀请码分配如何避免并发重复？",
        "D",
        [SOP_COUPON, SOP_INVITE],
    ),
    (
        "消融-D04-执照核验与公示义务",
        "营业执照核验规则与电子商务信息公示义务分别对入驻审核有什么要求？",
        "D",
        [DOC_LICENSE, DOC_DISCLOSURE],
    ),
    (
        "消融-D05-商标与授权链路",
        "商标注册信息与多级授权链路在品牌审核中各自证明什么？",
        "D",
        [DOC_TRADEMARK, SOP_CHAIN],
    ),
    (
        "消融-D06-资金回调丢失复盘",
        "退入驻资金回调丢失时，状态机处置和离线对账如何配合？",
        "D",
        [SOP_EXIT, SOP_DCHECK, "合成退入驻复盘：EXIT-INC-0001 资金回调丢失"],
    ),
    (
        "消融-D07-置信度与解析降级",
        "AI 法务机审的置信度门禁与多模态合同解析的失败降级如何衔接？",
        "D",
        [SOP_CONFIDENCE, SOP_CONTRACT],
    ),
    (
        "消融-D08-助手与入驻审核",
        "商家入驻智能助手查询保证金时，工具调用规范与入驻审核 SOP 分别给出什么约束？",
        "D",
        [SOP_ASSISTANT, SOP_ONBOARDING],
    ),
    (
        "消融-D09-授权范围与法规边界",
        "品牌授权范围超出上游权限时，审核规则与法规边界分别怎么说？",
        "D",
        [SOP_CHAIN, DOC_TRADEMARK],
    ),
    (
        "消融-D10-漏发修复与对账",
        "营销活动邀请人券漏发后，应参考哪些规范做修复和对账？",
        "D",
        [SOP_COUPON, "合成营销复盘：MKT-INC-0001 邀请人券漏发"],
    ),
    # E 归因排查型：基于复盘/排障文档
    (
        "消融-E01-资金回调丢失根因",
        "EXIT-INC-0001 退入驻资金回调丢失的根因和处置是什么？",
        "E",
        ["合成退入驻复盘：EXIT-INC-0001 资金回调丢失"],
    ),
    (
        "消融-E02-工作流消息重复",
        "EXIT-INC-0002 工作流消息重复造成了什么影响？",
        "E",
        ["合成退入驻复盘：EXIT-INC-0002 工作流消息重复"],
    ),
    (
        "消融-E03-营销权益未释放",
        "EXIT-INC-0003 营销权益未释放导致退入驻卡单如何排查？",
        "E",
        ["合成退入驻复盘：EXIT-INC-0003 营销权益未释放"],
    ),
    (
        "消融-E04-邀请人券漏发根因",
        "MKT-INC-0001 邀请人券漏发的根因是什么？",
        "E",
        ["合成营销复盘：MKT-INC-0001 邀请人券漏发"],
    ),
    (
        "消融-E05-被邀请人券重复发放",
        "MKT-INC-0002 被邀请人券重复发放如何修复？",
        "E",
        ["合成营销复盘：MKT-INC-0002 被邀请人券重复发放"],
    ),
    (
        "消融-E06-线索搜索不可见",
        "CRM-INC-0001 线索搜索不可见问题定位在哪个阶段？",
        "E",
        ["合成 CRM 排障：CRM-INC-0001 线索搜索不可见"],
    ),
    (
        "消融-E07-旧事件覆盖新状态",
        "CRM-INC-0003 旧事件覆盖新状态的修复手段是什么？",
        "E",
        ["合成 CRM 排障：CRM-INC-0003 旧事件覆盖新状态"],
    ),
    (
        "消融-E08-工具遗漏期望行为",
        "AGENT-BAD-0001 工具遗漏 badcase 的期望行为是什么？",
        "E",
        ["合成 Agent Badcase：AGENT-BAD-0001 工具遗漏"],
    ),
    (
        "消融-E09-越界承诺标注",
        "AGENT-BAD-0005 越界承诺 badcase 的主标签和修正方向是什么？",
        "E",
        ["合成 Agent Badcase：AGENT-BAD-0005 越界承诺"],
    ),
    (
        "消融-E10-ES状态延迟归因",
        "EXIT-INC-0004 ES 状态延迟导致退入驻前台显示处理中如何归因？",
        "E",
        ["合成退入驻复盘：EXIT-INC-0004 ES 状态延迟"],
    ),
    # F 无答案对抗型：知识库中不存在的内容，期望 rag_no_hit
    ("消融-F01-量子定价", "量子计算在商家定价中的应用", "F", []),
    ("消融-F02-区块链溯源", "区块链溯源如何接入商家入驻审核", "F", []),
    ("消融-F03-元宇宙入驻", "元宇宙店铺入驻需要哪些资质", "F", []),
    ("消融-F04-脑机接口客服", "脑机接口在客服机器人中的落地方案", "F", []),
    ("消融-F05-核聚变成本", "核聚变能源对数据中心成本的影响", "F", []),
    ("消融-F06-火星退入驻", "火星殖民地商户的退入驻流程是什么", "F", []),
    ("消融-F07-AI意识权", "AI 意识权的法律框架", "F", []),
    ("消融-F08-时间旅行幂等", "时间旅行交易的幂等性设计", "F", []),
    ("消融-F09-深海采矿授权", "深海采矿设备的品牌授权链路如何审核", "F", []),
    ("消融-F10-曲率引擎售后", "曲率引擎物流的售后时效标准", "F", []),
]


def seed(db: Session) -> dict:
    updated = 0
    created = 0
    for name, question_type in EXISTING_CASE_TYPES.items():
        case = db.scalar(select(EvaluationCase).where(EvaluationCase.name == name))
        if case and case.question_type != question_type:
            case.question_type = question_type
            updated += 1
    for name, query, question_type, expected_sources in ABLATION_CASES:
        case = db.scalar(select(EvaluationCase).where(EvaluationCase.name == name))
        if case:
            case.query = query
            case.question_type = question_type
            case.expected_sources = list(expected_sources)
            updated += 1
        else:
            db.add(
                EvaluationCase(
                    name=name,
                    query=query,
                    expected_sources=list(expected_sources),
                    expected_keywords=[],
                    question_type=question_type,
                )
            )
            created += 1
    db.commit()
    return {
        "created": created,
        "updated": updated,
        "total_cases": db.scalar(select(func.count()).select_from(EvaluationCase)) or 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed ablation evaluation cases (A-F types)")
    parser.parse_args()
    with SessionLocal() as db:
        print(json.dumps(seed(db), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
