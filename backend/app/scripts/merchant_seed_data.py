"""Deterministic merchant-domain data factory.

Public regulations are paraphrased and retain authoritative source URLs. All other
records are synthetic and are explicitly marked as such in document metadata.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Iterator

from app.schemas import DocumentCreate

PUBLIC_DOCUMENTS = [
    DocumentCreate(
        title="公开法规摘要：市场主体登记与营业执照核验",
        owner="法务合规",
        source_type="public_regulation",
        source_uri=(
            "https://www.samr.gov.cn/djzcj/zyfb/zjfb/art/2023/"
            "art_2a71fe3f890947e4860918946cb60386.html"
        ),
        metadata={
            "provenance": "公开资料改写",
            "synthetic": False,
            "authority": "国家市场监督管理总局",
            "business_line": "商家入驻",
        },
        content="""# 适用边界
本摘要用于商家入驻材料的基础一致性校验，不能替代登记机关认定或法务意见。

# 登记与执照
市场主体原则上应依法办理登记，登记包括设立、变更和注销。营业执照签发日期为市场主体成立日期，电子营业执照与纸质营业执照具有同等法律效力。

# 机审核验建议
机审应提取市场主体名称、统一社会信用代码、主体类型、法定代表人、住所、经营范围、登记机关和成立日期。OCR 结果与权威查询结果不一致、执照疑似涂改或关键字段无法识别时，不应自动通过，应进入人工复核。

# 变更与公示
涉及营业执照记载事项的变更应关注是否已换发执照。从事电子商务的市场主体还应持续公示营业执照信息或相关链接。""",
        compile=False,
    ),
    DocumentCreate(
        title="公开法规摘要：电子商务经营者信息公示义务",
        owner="法务合规",
        source_type="public_regulation",
        source_uri=(
            "https://www.samr.gov.cn/zfjcj/tzgg/art/2023/art_d337c3291e8b40459ca03dea54395856.html"
        ),
        metadata={
            "provenance": "公开资料改写",
            "synthetic": False,
            "authority": "国家市场监督管理总局",
            "business_line": "平台合规",
        },
        content="""# 信息公示
电子商务经营者应在显著位置持续公示营业执照信息、相关行政许可信息或对应链接标识；公示信息发生变化时应及时更新。

# 商品与服务信息
经营者应全面、真实、准确、及时地披露商品或服务信息，不得通过虚构交易或编造评价进行误导性宣传。

# 终止经营
经营者自行终止电子商务经营时，应按法规要求提前公示相关信息。平台内部退入驻流程应将业务停止、在途订单、资金结算、信息公示和数据留存分别校验。""",
        compile=False,
    ),
    DocumentCreate(
        title="公开法规摘要：商标审核与授权材料边界",
        owner="法务合规",
        source_type="public_regulation",
        source_uri="https://www.cnipa.gov.cn/art/2019/7/30/art_95_28179.html",
        metadata={
            "provenance": "公开资料改写",
            "synthetic": False,
            "authority": "国家知识产权局",
            "business_line": "品牌资质",
            "legal_version": "2019修正；2026修订版自2027-01-01施行",
        },
        content="""# 审核边界
商标注册信息只能证明登记状态和核定使用范围，不能单独证明申请商家已获得品牌经营授权。平台仍需核验授权主体、被授权主体、授权品牌、商品范围、销售渠道、区域和有效期。

# 授权链路
经销商提交多级授权时，应校验每一级授权方与被授权方是否首尾衔接，授权范围是否逐级收窄且未超出上游权限。缺少中间层授权、主体名称无法对应或授权范围扩大时，应驳回或转人工复核。

# 版本提示
本知识页基于当前生效规则整理。已经公布但尚未生效的法规修订不得提前作为自动驳回依据，规则切换应配置生效日期并保留历史版本。""",
        compile=False,
    ),
]

SYNTHETIC_SOPS = [
    (
        "商家入驻主体一致性审核 SOP",
        "商家入驻",
        "入驻审核",
        """# 自动通过条件
营业执照主体名称、统一社会信用代码与权威查询一致，执照状态正常，申请主体与店铺结算主体一致时可以进入下一审核节点。
# 人工复核条件
主体发生近期变更、OCR 置信度低于 0.90、法定代表人或经营范围存在歧义时进入人工复核。证件疑似涂改、查询无结果或主体状态异常时直接阻断。
# 证据要求
审核结果必须保存原始文件、解析字段、外部查询快照、命中规则版本和最终决策，不允许只记录模型自然语言结论。""",
    ),
    (
        "品牌方授权资质审核规则",
        "品牌资质",
        "法务机审",
        """# 授权要素
品牌方申请需要识别授权方、被授权方、商标或品牌、授权类目、渠道、区域、起止时间、转授权权限和签章。
# 决策规则
被授权主体与申请主体完全一致、授权范围覆盖申请类目和平台渠道、有效期覆盖审核日且签章完整时可自动通过。存在名称别称时必须有主体关系证明。
# 风险门禁
授权范围使用“等”“相关产品”等模糊表述、合同跨页信息矛盾、签章被遮挡或有效期无法确定时禁止自动通过。""",
    ),
    (
        "经销商多级授权链路审核 SOP",
        "品牌资质",
        "法务机审",
        """# 链路构建
以品牌权利人作为根节点，按授权方和被授权方构建有向链路，申请商家必须是可达叶子节点。每一级主体名称应经过标准化和别名映射。
# 范围传递
下游获得的类目、渠道、区域、期限和转授权能力不得超过上游。任一级授权过期会使其下游链路失效。
# 异常处理
缺少中间授权、存在环路、授权主体无法唯一匹配或同一层材料结论冲突时进入人工法务复核。""",
    ),
    (
        "多模态合同解析与后处理规范",
        "品牌资质",
        "算法工程",
        """# 解析流程
PDF 先进行版面分析和页级索引，再按授权要素调用视觉语言模型提取。跨页表格不得只根据单页局部结果决策。
# 后处理
日期统一转换为 ISO 格式，主体名称去除无意义空格但保留原文，商标注册号必须通过格式校验。模型输出的页码和证据框需要落库。
# 失败降级
文件损坏、加密、页数超过阈值或模型结果无法通过确定性校验时转人工，不得使用空字段继续工作流。""",
    ),
    (
        "AI 法务机审决策与置信度门禁",
        "品牌资质",
        "算法质量",
        """# 自动决策
只有材料完整性、字段一致性、授权链路和规则判断全部通过，且关键字段置信度达到阈值时才能自动通过。
# 不通过策略
确定性硬规则命中可自动不通过；依赖语义解释、材料存在冲突或规则版本不确定时必须人工复核。
# 指标口径
以不通过作为正例分别计算 Precision 和 Recall，同时跟踪自动审核覆盖率、人工改判率和不同申请类型的分层指标。""",
    ),
    (
        "资质驳回码与客服解释规范",
        "商家客服",
        "审核运营",
        """# 驳回码
每个驳回结果必须绑定稳定驳回码，驳回码对应可执行的补充材料指引，不允许直接向商家展示模型推理过程。
# 客服解释
客服应说明缺失或不一致的材料要素、重新提交方式和预计处理时间。涉及法务判断时不得承诺一定通过。
# 规则追溯
同一申请多次驳回应关联历史规则版本和材料版本，避免新材料仍引用旧结论。""",
    ),
    (
        "退入驻状态机与卡单处置 SOP",
        "退入驻",
        "交易平台",
        """# 前置检查
退入驻需要依次检查在途订单、售后、保证金、待结算资金、营销权益和违规处罚。任何阻断项未清零不得完成退出。
# 卡单治理
状态超过预计时长后创建诊断任务，查询工作流节点、下游受理记录和消息消费状态。重试必须携带原业务幂等键。
# 职责边界
资金结算由资金域给出最终状态，商家域负责流程编排和状态展示。跨团队异常必须记录责任系统和下一步动作。""",
    ),
    (
        "退入驻 DCheck 离线对账规则",
        "退入驻",
        "数据质量",
        """# 对账集合
每日比对商家退出主表、工作流实例、资金结算、营销权益和搜索索引中的状态。以商家退出申请号作为跨系统关联键。
# 异常类型
主流程完成但下游未完成标记为状态不一致；下游完成但主流程未推进标记为回调丢失；多条生效记录标记为幂等异常。
# 处置要求
对账结果按风险分级，高风险自动告警但修复动作需要审批。修复后下一批次必须再次校验。""",
    ),
    (
        "招商 CRM 线索生命周期规则",
        "招商CRM",
        "招商产品",
        """# 状态管理
线索从待分配、已分配、跟进中、意向确认、入驻转化或无效结束等状态流转。所有状态变更必须由允许的事件触发。
# 并发控制
客户认领和跟进人变更需要加锁并校验版本，禁止同一有效线索同时归属多个跟进人。
# 审计
状态事件应记录操作者、来源、前后状态、业务参数和关联任务，离线导入不得绕过领域校验。""",
    ),
    (
        "CRM MySQL 到 Elasticsearch 同步排障手册",
        "招商CRM",
        "平台稳定性",
        """# 延迟定位
先比较数据库更新时间、消息发送时间、消费时间和 Elasticsearch refresh 时间，定位延迟位于生产、消费还是索引刷新阶段。
# 数据一致性
搜索结果缺失时应按主键回查数据库，不得直接认定数据丢失。补偿写入使用版本号避免旧事件覆盖新状态。
# 性能治理
慢查询需检查查询条件、字段映射、分片数量和深分页。高频计数可由 Redis 承担实时读，但数据库保留最终事实。""",
    ),
    (
        "营销发券幂等与最终一致性规范",
        "营销发券",
        "营销平台",
        """# 幂等键
发券记录以活动、任务、用户、券类型和业务批次组成幂等键。策略重试和定时回扫必须复用同一幂等键。
# 回扫机制
受理成功但结果未知的记录进入半小时回扫；明确失败可按错误类型决定重试或人工处理；已经成功不得重复请求营销中台。
# 对账
分别核对邀请人券、被邀请人券和返佣券，异常分为漏发、多发、错券和金额不一致。""",
    ),
    (
        "邀请码预生成与分配治理",
        "营销发券",
        "营销平台",
        """# 唯一性
邀请码生成后必须建立唯一约束，分配操作使用条件更新或分布式锁，禁止先查后写导致并发重复。
# 存量修复
重复邀请码按创建时间和实际使用关系确定保留记录，修复过程保存影响用户、原值、新值和补偿结果。
# 告警
可用邀请码库存低于安全水位、单码关联多个用户或分配失败率升高时触发告警。""",
    ),
    (
        "商家入驻智能助手工具调用规范",
        "智能助手",
        "Agent平台",
        """# 工具选择
类目准入、保证金和审核进度属于实时信息，必须调用对应工具；材料准备和驳回说明可先检索知识库。
# 澄清策略
缺少主体类型、经营类目或品牌关系且会影响结论时才追问。已经从历史对话或工具结果获得的参数不得重复询问。
# 安全边界
助手不得承诺审核结果，不得编造类目规则。写操作和包含商家敏感信息的查询必须进行权限校验。""",
    ),
    (
        "Agent Badcase 标注与错误驱动补数规范",
        "智能助手",
        "算法质量",
        """# 错误分类
Badcase 分为意图错误、工具遗漏、工具选择错误、参数错误、过度追问、工具结果未利用、幻觉和越界回答。
# 数据生产
新增样本应保留原始对话、期望动作、关键参数、错误标签和修正理由；改写数据不能改变业务条件和最终结论。
# 评测
数据集按商家和会话隔离，历史线上样本按时间切分，防止同一问题改写版本同时出现在训练集与测试集。""",
    ),
]

PROFILES = {
    "small": {"brand": 6, "onboarding": 5, "exit": 4, "coupon": 4, "crm": 3, "agent": 5},
    "standard": {
        "brand": 36,
        "onboarding": 28,
        "exit": 16,
        "coupon": 16,
        "crm": 14,
        "agent": 20,
    },
    "full": {
        "brand": 80,
        "onboarding": 55,
        "exit": 35,
        "coupon": 35,
        "crm": 25,
        "agent": 30,
    },
}


def _metadata(line: str, scenario: str, record_id: str, severity: str = "P2") -> dict:
    return {
        "provenance": "合成数据",
        "synthetic": True,
        "business_line": line,
        "scenario": scenario,
        "record_id": record_id,
        "severity": severity,
        "pii": "已脱敏",
    }


def _brand_cases(count: int, rng: random.Random) -> Iterator[DocumentCreate]:
    reasons = [
        ("PASS", "授权链路完整，申请主体、授权类目、平台渠道和有效期均一致"),
        ("REJECT", "缺少品牌方到一级经销商的中间授权，链路不可达"),
        ("REVIEW", "授权区域使用模糊表述，无法确定是否覆盖线上渠道"),
        ("REJECT", "申请主体与被授权主体名称不一致且未提交关系证明"),
        ("REVIEW", "合同签章跨页且清晰度不足，模型置信度低于自动决策阈值"),
        ("REJECT", "下游申请类目超出上游授权范围"),
    ]
    categories = ["运动鞋服", "美妆护肤", "数码配件", "箱包", "食品", "家居"]
    start = date(2026, 1, 1)
    for index in range(count):
        decision, reason = reasons[index % len(reasons)]
        category = categories[index % len(categories)]
        submitted = start + timedelta(days=index % 180)
        expiry = submitted + timedelta(days=30 + (index * 17) % 540)
        case_id = f"BR-{submitted:%Y%m%d}-{index + 1:04d}"
        yield DocumentCreate(
            title=f"合成审核案例：{case_id} {category} {decision}",
            owner="资质审核",
            source_type="synthetic_review_case",
            source_uri=f"synthetic://qualification/{case_id}",
            metadata=_metadata(
                "品牌资质", "授权审核", case_id, "P1" if decision == "REJECT" else "P2"
            ),
            content=f"""# 申请信息
案例编号 {case_id}，申请类目为{category}，材料提交日期为{submitted.isoformat()}。申请主体为示例商贸{index + 1:03d}号有限公司，材料已完成脱敏。
# 解析结果
授权文件共 {2 + index % 8} 页，识别到 {1 + index % 3} 级授权。授权截止日期为 {expiry.isoformat()}，关键字段最低置信度为 {0.78 + rng.random() * 0.21:.3f}。
# 审核结论
决策为 {decision}。主要依据：{reason}。
# 复核提示
该案例为合成评测数据，只用于验证规则召回、证据引用和决策解释，不代表真实商家申请。""",
            compile=False,
        )


def _onboarding_cases(count: int) -> Iterator[DocumentCreate]:
    statuses = ["正常", "名称不一致", "经营范围待确认", "执照变更中", "证件疑似涂改"]
    entity_types = ["有限责任公司", "个体工商户", "合伙企业", "分公司"]
    for index in range(count):
        status = statuses[index % len(statuses)]
        case_id = f"ONB-2026-{index + 1:05d}"
        action = (
            "进入下一节点" if status == "正常" else ("人工复核" if index % 2 else "阻断并补充材料")
        )
        yield DocumentCreate(
            title=f"合成入驻案例：{case_id} {status}",
            owner="商家入驻",
            source_type="synthetic_onboarding_case",
            source_uri=f"synthetic://onboarding/{case_id}",
            metadata=_metadata("商家入驻", "主体审核", case_id),
            content=f"""# 主体信息
申请编号 {case_id}，主体类型为{entity_types[index % len(entity_types)]}，统一社会信用代码已脱敏，OCR 置信度为 {0.82 + (index % 17) / 100:.2f}。
# 核验结果
权威查询状态为{status}。申请填写名称与执照 OCR 名称的标准化匹配结果为 {"一致" if status == "正常" else "不一致或不确定"}。
# 处置
该申请应{action}。审核记录需要保存执照原图、解析字段、查询快照和规则版本。
# 数据声明
本记录为合成数据，不包含真实商家或自然人信息。""",
            compile=False,
        )


def _exit_incidents(count: int) -> Iterator[DocumentCreate]:
    causes = ["资金回调丢失", "工作流消息重复", "营销权益未释放", "ES 状态延迟", "售后单未完结"]
    for index in range(count):
        cause = causes[index % len(causes)]
        incident_id = f"EXIT-INC-{index + 1:04d}"
        severity = "P1" if index % 7 == 0 else "P2"
        yield DocumentCreate(
            title=f"合成退入驻复盘：{incident_id} {cause}",
            owner="交易平台",
            source_type="synthetic_incident",
            source_uri=f"synthetic://incident/exit/{incident_id}",
            metadata=_metadata("退入驻", "卡单排查", incident_id, severity),
            content=f"""# 现象
退入驻申请 {incident_id} 在 WAITING_DEPENDENCY 状态停留 {35 + index * 3} 分钟，前台持续显示处理中。
# 归因
根因为{cause}。主流程幂等键存在且重复执行未产生额外退出记录，下游状态与主表出现短暂不一致。
# 处置
先通过 DCheck 确认受影响范围，再补偿缺失事件并重放当前节点。高风险写操作经审批后执行。
# 改进
增加节点超时告警、跨系统状态对账和回调消费指标。该复盘为合成数据。""",
            compile=False,
        )


def _coupon_incidents(count: int) -> Iterator[DocumentCreate]:
    problems = ["邀请人券漏发", "被邀请人券重复发放", "返佣券金额错误", "邀请码重复占用"]
    for index in range(count):
        problem = problems[index % len(problems)]
        incident_id = f"MKT-INC-{index + 1:04d}"
        yield DocumentCreate(
            title=f"合成营销复盘：{incident_id} {problem}",
            owner="营销平台",
            source_type="synthetic_incident",
            source_uri=f"synthetic://incident/coupon/{incident_id}",
            metadata=_metadata(
                "营销发券", "一致性治理", incident_id, "P1" if index % 8 == 0 else "P2"
            ),
            content=f"""# 异常
活动批次 BATCH-{index % 6 + 1:02d} 出现{problem}，合成影响记录数为 {3 + index * 11}。
# 根因
客户端重试与服务端受理超时叠加，旧逻辑未使用稳定业务幂等键，结果回调和发券记录之间存在时间窗口。
# 修复
以活动、任务、用户、券类型和批次重建幂等键，对结果未知记录执行半小时回扫，成功记录禁止再次请求。
# 对账
离线任务分别核对邀请人券、被邀请人券和返佣券。该记录为合成事故复盘。""",
            compile=False,
        )


def _crm_incidents(count: int) -> Iterator[DocumentCreate]:
    issues = ["线索搜索不可见", "跟进人计数偏差", "旧事件覆盖新状态", "深分页查询超时"]
    for index in range(count):
        issue = issues[index % len(issues)]
        incident_id = f"CRM-INC-{index + 1:04d}"
        yield DocumentCreate(
            title=f"合成 CRM 排障：{incident_id} {issue}",
            owner="招商CRM",
            source_type="synthetic_incident",
            source_uri=f"synthetic://incident/crm/{incident_id}",
            metadata=_metadata("招商CRM", "搜索与同步", incident_id),
            content=f"""# 告警
招商人员反馈{issue}，接口 P95 为 {130 + index * 19}ms，MySQL 与 Elasticsearch 最大版本差为 {1 + index % 9}。
# 定位
按数据库提交、消息发送、消费和索引刷新四个时间点排查，确认问题位于 {"消息积压" if index % 2 else "查询条件与索引映射"}。
# 修复
补偿任务按主键和版本号重建索引，禁止旧事件覆盖新状态。实时计数使用 Redis，数据库作为最终事实。
# 结果
修复后应回归即席查询、客户认领并发和状态机事件审计。该记录为合成数据。""",
            compile=False,
        )


def _agent_badcases(count: int) -> Iterator[DocumentCreate]:
    errors = ["工具遗漏", "参数错误", "过度追问", "未利用工具结果", "越界承诺", "知识引用错误"]
    intents = ["查询保证金", "查询审核进度", "判断类目准入", "解释资质驳回", "准备品牌材料"]
    for index in range(count):
        error = errors[index % len(errors)]
        intent = intents[index % len(intents)]
        case_id = f"AGENT-BAD-{index + 1:04d}"
        yield DocumentCreate(
            title=f"合成 Agent Badcase：{case_id} {error}",
            owner="Agent平台",
            source_type="synthetic_agent_badcase",
            source_uri=f"synthetic://agent/badcase/{case_id}",
            metadata=_metadata("智能助手", "Badcase", case_id),
            content=f"""# 用户意图
用户希望{intent}，会话中已提供主体类型和经营类目，商家标识已脱敏。
# 错误表现
Agent 出现{error}，导致端到端任务未成功。错误步骤为第 {1 + index % 4} 步。
# 期望行为
先复用会话已有参数，再检索规则或调用实时工具；工具返回后应提取状态、原因和下一步动作，不得承诺审核一定通过。
# 标注
主标签为{error}，样本进入错误驱动补数集。该对话为合成数据。""",
            compile=False,
        )


def build_documents(scale: str = "standard", seed: int = 202602) -> list[DocumentCreate]:
    if scale not in PROFILES:
        raise ValueError(f"unknown scale: {scale}")
    rng = random.Random(seed)
    config = PROFILES[scale]
    documents = list(PUBLIC_DOCUMENTS)
    for title, line, owner, content in SYNTHETIC_SOPS:
        slug = str(len(documents) + 1).zfill(3)
        documents.append(
            DocumentCreate(
                title=title,
                owner=owner,
                source_type="synthetic_sop",
                source_uri=f"synthetic://sop/{slug}",
                metadata=_metadata(line, "SOP", f"SOP-{slug}"),
                content=content
                + "\n\n# 数据声明\n该 SOP 为结合业务背景生成的演示知识，不代表任何公司的现行制度。",
                compile=False,
            )
        )
    documents.extend(_brand_cases(config["brand"], rng))
    documents.extend(_onboarding_cases(config["onboarding"]))
    documents.extend(_exit_incidents(config["exit"]))
    documents.extend(_coupon_incidents(config["coupon"]))
    documents.extend(_crm_incidents(config["crm"]))
    documents.extend(_agent_badcases(config["agent"]))
    return documents


EVALUATION_CASES = [
    (
        "营业执照主体不一致",
        "营业执照名称与申请主体不一致应该如何处理？",
        ["商家入驻主体一致性审核 SOP"],
    ),
    ("品牌授权要素", "品牌方授权材料需要提取哪些关键信息？", ["品牌方授权资质审核规则"]),
    ("多级授权断链", "经销商缺少中间一级授权能否自动通过？", ["经销商多级授权链路审核 SOP"]),
    ("跨页合同", "授权信息跨页且字段矛盾时怎么决策？", ["多模态合同解析与后处理规范"]),
    ("机审门禁", "什么条件下 AI 法务机审可以自动通过？", ["AI 法务机审决策与置信度门禁"]),
    ("驳回解释", "客服应如何向商家解释资质驳回？", ["资质驳回码与客服解释规范"]),
    ("退入驻卡单", "退入驻卡在依赖节点时如何排查？", ["退入驻状态机与卡单处置 SOP"]),
    ("退入驻对账", "退出主表和资金状态不一致如何发现？", ["退入驻 DCheck 离线对账规则"]),
    ("CRM 状态", "客户认领并发时如何避免多个跟进人？", ["招商 CRM 线索生命周期规则"]),
    (
        "ES 同步",
        "MySQL 已更新但 CRM 搜索不到线索怎么排查？",
        ["CRM MySQL 到 Elasticsearch 同步排障手册"],
    ),
    ("发券幂等", "营销任务重试如何避免重复发券？", ["营销发券幂等与最终一致性规范"]),
    ("邀请码并发", "邀请码分配为什么不能先查后写？", ["邀请码预生成与分配治理"]),
    ("助手工具选择", "查询保证金是否可以只回答知识库内容？", ["商家入驻智能助手工具调用规范"]),
    (
        "Agent 补数",
        "工具参数错误的 Badcase 应如何标注和补数？",
        ["Agent Badcase 标注与错误驱动补数规范"],
    ),
    (
        "法规公示",
        "电子商务经营者需要公示哪些主体信息？",
        ["公开法规摘要：电子商务经营者信息公示义务"],
    ),
    ("执照 OCR 门禁", "营业执照 OCR 置信度不足时应自动通过还是人工复核？", ["商家入驻主体一致性审核 SOP"]),
    ("授权范围", "下游授权渠道超过上游权限时如何处理？", ["经销商多级授权链路审核 SOP"]),
    ("合同页码证据", "多模态合同解析需要保存哪些定位证据？", ["多模态合同解析与后处理规范"]),
    ("机审分层指标", "AI 法务机审需要跟踪哪些质量和分层指标？", ["AI 法务机审决策与置信度门禁"]),
    ("驳回码追溯", "多次驳回时为什么要关联历史规则版本？", ["资质驳回码与客服解释规范"]),
    ("退入驻前置条件", "退入驻完成前必须检查哪些阻断项？", ["退入驻状态机与卡单处置 SOP"]),
    ("对账异常分类", "DCheck 如何区分状态不一致、回调丢失和幂等异常？", ["退入驻 DCheck 离线对账规则"]),
    ("发券对账", "营销发券漏发和重复发放应如何发现？", ["营销发券幂等与最终一致性规范"]),
    ("商标授权边界", "只有商标注册信息能否证明商家已经获得授权？", ["公开法规摘要：商标审核与授权材料边界"]),
    ("法规生效日期", "尚未生效的法规修订能否提前用于自动驳回？", ["公开法规摘要：商标审核与授权材料边界"]),
]


AGENT_INSTRUCTIONS = [
    "查询品牌经销商授权链路断裂时的审核规则和证据要求",
    "分析退入驻状态长时间未推进的常见原因",
    "查询 CRM 中 MySQL 已更新但搜索不可见的排查步骤",
    "解释营销发券为什么必须使用稳定幂等键",
    "查询 AI 法务机审自动通过的置信度门禁",
    "为品牌授权范围超出上游权限设计测试用例",
    "为退入驻资金回调丢失设计回归测试用例",
    "为商家助手工具参数错误设计多轮测试用例",
]
