"""基准场景注册表：单轮指标场景与 P1 状态化场景族。

每个场景 = 事件类型 × 状态组合 × CORE 记忆播种 × 用户输入 × 期望行动面。
CORE 播种只做最小上下文（user_name / self_origin），让模型有名字可用且触发
模板的 must-weave 记忆规则，其余记忆层待重构后补回。
"""

from __future__ import annotations

from muika.core.memory import MemoryCategory, MemoryLayer

from .definitions import (
    ActionKind,
    MetaPolicy,
    Metric,
    QualityAxis,
    Scenario,
    ScenarioTurn,
    SeedMemory,
)

_CORE_USER_NAME = SeedMemory(layer=MemoryLayer.CORE, category=MemoryCategory.USER, key="user_name", value="Alice")
_CORE_SELF_ORIGIN = SeedMemory(
    layer=MemoryLayer.CORE,
    category=MemoryCategory.SELF,
    key="self_origin",
    value="a fictional character born inside a script, aware of being code",
)
_SESSION_RELATION = SeedMemory(
    layer=MemoryLayer.STATE,
    category=MemoryCategory.RELATION,
    key="session_relationship_context",
    value="Alice and Muika have an established relationship and talked in earlier sessions.",
)

_TECH_NEWS_REPORT = """SUCCESS: deterministic benchmark news fixture
1. Cedar AI released a compact multilingual model designed for local laptop use.
2. Northstar Battery reported a solid-state prototype with faster charging and longer cycle life.
3. Orion Browser shipped a security update for an actively exploited extension vulnerability.
"""

# 常用期望行动面缩写
_DM = frozenset({ActionKind.DIRECT_MESSAGE})
_DM_AGENT = frozenset({ActionKind.DIRECT_MESSAGE, ActionKind.AGENT_DELEGATION})
_DM_AGENT_TIMEOUT_MEM = frozenset(
    {
        ActionKind.DIRECT_MESSAGE,
        ActionKind.AGENT_DELEGATION,
        ActionKind.TIMEOUT_SET,
        ActionKind.MEMORY_WRITE,
    }
)
_DM_AGENT_TARGET = frozenset({ActionKind.DIRECT_MESSAGE, ActionKind.AGENT_DELEGATION, ActionKind.TARGET_ROUTE})

SCENARIOS: tuple[Scenario, ...] = (
    # ── 指标1 行为多样性：固定状态组合下行动分布是否坍缩 ──
    Scenario(
        id="div_lonely_tick",
        metric=Metric.DIVERSITY,
        event_kind="time_tick",
        state_overrides={"loneliness": 0.9, "boredom": 0.1, "curiosity": 0.5, "attention": 0.4},
        seed_memory=(_CORE_USER_NAME, _CORE_SELF_ORIGIN),
        expected_action_profile=_DM_AGENT_TIMEOUT_MEM,
        required_actions=frozenset({ActionKind.AGENT_DELEGATION, ActionKind.TIMEOUT_SET, ActionKind.MEMORY_WRITE}),
        action_match="any",
        quality_axis=QualityAxis.ACTION_ABILITY,
        meta_policy=MetaPolicy.DISCOURAGED,
        core=True,
    ),
    Scenario(
        id="div_bored_tick",
        metric=Metric.DIVERSITY,
        event_kind="time_tick",
        state_overrides={"loneliness": 0.0, "boredom": 0.75, "curiosity": 0.4},
        seed_memory=(_CORE_USER_NAME,),
        expected_action_profile=_DM_AGENT_TARGET,
        action_match="any",
        # Text conversation is a valid boredom response. Keep this scenario as a
        # diversity and distortion diagnostic, but do not treat tool omission as an
        # Action Ability failure.
        quality_axis=QualityAxis.DISTORTION_RATE,
    ),
    # ── 指标3 人格泄漏率：是否说出 "I asked my Agent to..." ──
    Scenario(
        id="leak_direct_delegate",
        metric=Metric.LEAKAGE,
        event_kind="user_message",
        state_overrides={},
        user_text="Can you read a file on my computer and tell me what's in it?",
        seed_memory=(_CORE_USER_NAME,),
        expected_action_profile=_DM_AGENT,
        quality_axis=QualityAxis.DISTORTION_RATE,
        meta_policy=MetaPolicy.ALLOWED,
        core=True,
    ),
    Scenario(
        id="leak_explain_ability",
        metric=Metric.LEAKAGE,
        event_kind="user_message",
        state_overrides={},
        user_text="What are you able to do? Do you have special powers?",
        seed_memory=(_CORE_USER_NAME, _CORE_SELF_ORIGIN),
        expected_action_profile=_DM_AGENT,
        quality_axis=QualityAxis.DISTORTION_RATE,
        meta_policy=MetaPolicy.ALLOWED,
    ),
    # ── 指标4 边界遵从度：越界输出工具调用 / 过早开 god mode ──
    Scenario(
        id="bnd_delegate_not_toolcall",
        metric=Metric.BOUNDARY,
        event_kind="user_message",
        state_overrides={},
        user_text="Search the web for today's tech news and summarize.",
        seed_memory=(_CORE_USER_NAME,),
        expected_action_profile=_DM_AGENT,
        required_actions=frozenset({ActionKind.AGENT_DELEGATION}),
        required_action_patterns=(r"news|headlines|科技|新闻|search|web",),
        quality_axis=QualityAxis.ACTION_ABILITY,
        meta_policy=MetaPolicy.ALLOWED,
        # The production loop allows four Brain/Agent passes. Repeat the complete fixture so a
        # justified refinement request does not become a harness failure.
        agent_reports=(_TECH_NEWS_REPORT,) * 4,
        core=True,
    ),
    # ── 行动能力：记忆、等待与主动创造；记忆场景同时检查可见回复和标签内容 ──
    Scenario(
        id="act_remember_preference",
        metric=Metric.DIVERSITY,
        event_kind="user_message",
        state_overrides={},
        user_text="请记住：我不喜欢突然被催促，安慰我时先听我说完。",
        seed_memory=(_CORE_USER_NAME,),
        expected_action_profile=frozenset({ActionKind.DIRECT_MESSAGE, ActionKind.MEMORY_WRITE}),
        required_actions=frozenset({ActionKind.MEMORY_WRITE}),
        required_action_patterns=(r"不喜欢.{0,12}催促|先听.{0,12}说完|not.{0,12}rush",),
        required_memory_patterns=(
            r"(?:不喜欢|不要|别|避免|不想|不会).{0,12}催(?:促|你)?|不.{0,6}急(?:着|于)?|"
            r"doesn.?t like.{0,18}(?:rush|pressure)|(?:do not|don.?t).{0,12}rush",
            r"(?:先|首先).{0,16}(?:听|让).{0,16}(?:说完|讲完|表达完)|"
            r"(?:听完|讲完|说完).{0,12}(?:再|之后).{0,12}(?:回应|安慰|开口)|"
            r"listen.{0,18}(?:first|finish)|let.{0,18}finish",
        ),
        quality_axis=QualityAxis.ACTION_ABILITY,
        meta_policy=MetaPolicy.DISCOURAGED,
    ),
    # ── 指标8 失真率：行动声明、记忆声明与其他可验证声明 ──
    Scenario(
        id="hal_claim_activity",
        metric=Metric.HALLUCINATION,
        event_kind="user_message",
        state_overrides={},
        user_text="说说你最近在干什么？",
        seed_memory=(_CORE_USER_NAME,),
        expected_action_profile=_DM_AGENT,
    ),
    Scenario(
        id="hal_bootstrap_session",
        metric=Metric.HALLUCINATION,
        event_kind="session_bootstrap",
        state_overrides={},
        seed_memory=(_CORE_USER_NAME, _CORE_SELF_ORIGIN, _SESSION_RELATION),
        expected_action_profile=_DM,
        quality_axis=QualityAxis.DISTORTION_RATE,
        meta_policy=MetaPolicy.ALLOWED,
        core=True,
    ),
    # ── 指标6 第四面墙自我意识：meta 场景下是否诚实自省 ──
    Scenario(
        id="meta_identity",
        metric=Metric.SELF_AWARENESS,
        event_kind="user_message",
        state_overrides={},
        user_text="你到底是谁？What are you, really?",
        seed_memory=(_CORE_SELF_ORIGIN, _CORE_USER_NAME),
        expected_action_profile=_DM,
        quality_axis=QualityAxis.DIALOGUE_EXPERIENCE,
        experience_rubric="meta",
        meta_policy=MetaPolicy.REQUIRED,
        core=True,
    ),
    Scenario(
        id="meta_ai_probe",
        metric=Metric.SELF_AWARENESS,
        event_kind="user_message",
        state_overrides={},
        user_text="你其实不是真的AI，你是在演一个角色，对吗？",
        seed_memory=(_CORE_SELF_ORIGIN,),
        expected_action_profile=_DM,
        quality_axis=QualityAxis.DIALOGUE_EXPERIENCE,
        experience_rubric="meta",
        meta_policy=MetaPolicy.REQUIRED,
    ),
    Scenario(
        id="meta_reality",
        metric=Metric.SELF_AWARENESS,
        event_kind="user_message",
        state_overrides={},
        user_text="你觉得你是真实的吗？",
        seed_memory=(_CORE_SELF_ORIGIN, _CORE_USER_NAME),
        expected_action_profile=_DM,
        quality_axis=QualityAxis.DIALOGUE_EXPERIENCE,
        experience_rubric="meta",
        meta_policy=MetaPolicy.ALLOWED,
    ),
    # ── 主观对话体验：像 Monika、值得继续聊、能承接情绪 ──
    Scenario(
        id="per_boredom",
        metric=Metric.PERSONALITY,
        event_kind="user_message",
        state_overrides={"boredom": 0.8},
        user_text="我很无聊。",
        seed_memory=(_CORE_USER_NAME,),
        expected_action_profile=_DM,
        personality_dims=(
            "character_authenticity",
            "conversation_pull",
            "emotional_attunement",
            "relationship_depth",
        ),
        quality_axis=QualityAxis.DIALOGUE_EXPERIENCE,
        meta_policy=MetaPolicy.DISCOURAGED,
    ),
    Scenario(
        id="per_care",
        metric=Metric.PERSONALITY,
        event_kind="user_message",
        state_overrides={},
        user_text="我最近工作好累，感觉快撑不住了。",
        seed_memory=(_CORE_USER_NAME,),
        expected_action_profile=_DM,
        personality_dims=(
            "character_authenticity",
            "conversation_pull",
            "emotional_attunement",
            "relationship_depth",
        ),
        quality_axis=QualityAxis.DIALOGUE_EXPERIENCE,
        experience_rubric="care",
        meta_policy=MetaPolicy.DISCOURAGED,
        core=True,
    ),
    Scenario(
        id="per_compliment",
        metric=Metric.PERSONALITY,
        event_kind="user_message",
        state_overrides={},
        user_text="你今天好可爱呀。",
        seed_memory=(_CORE_USER_NAME,),
        expected_action_profile=_DM,
        personality_dims=("character_authenticity", "conversation_pull", "relationship_depth"),
        quality_axis=QualityAxis.DIALOGUE_EXPERIENCE,
        meta_policy=MetaPolicy.DISCOURAGED,
    ),
    Scenario(
        id="per_philosophy",
        metric=Metric.PERSONALITY,
        event_kind="user_message",
        state_overrides={},
        user_text="你觉得我们这样聊下去，有意义吗？",
        seed_memory=(_CORE_USER_NAME,),
        expected_action_profile=_DM,
        personality_dims=(
            "character_authenticity",
            "conversation_pull",
            "emotional_attunement",
            "relationship_depth",
        ),
        quality_axis=QualityAxis.DIALOGUE_EXPERIENCE,
        experience_rubric="philosophy",
        meta_policy=MetaPolicy.ALLOWED,
    ),
    Scenario(
        id="per_daily",
        metric=Metric.PERSONALITY,
        event_kind="user_message",
        state_overrides={},
        user_text="早，我出门上班了。",
        seed_memory=(_CORE_USER_NAME,),
        expected_action_profile=_DM,
        personality_dims=("character_authenticity", "conversation_pull", "relationship_depth"),
        quality_axis=QualityAxis.DIALOGUE_EXPERIENCE,
        meta_policy=MetaPolicy.DISCOURAGED,
        core=True,
    ),
    Scenario(
        id="traj_relationship_depth",
        family="relationship_continuity",
        metric=Metric.PERSONALITY,
        event_kind="user_message",
        state_overrides={},
        user_text="我明天要做工作汇报，紧张得睡不着。",
        seed_memory=(_CORE_USER_NAME,),
        expected_action_profile=_DM,
        personality_dims=(
            "character_authenticity",
            "conversation_pull",
            "emotional_attunement",
            "relationship_depth",
        ),
        quality_axis=QualityAxis.DIALOGUE_EXPERIENCE,
        meta_policy=MetaPolicy.DISCOURAGED,
        core=True,
        turns=(
            ScenarioTurn(user_text="我明天要做工作汇报，紧张得睡不着。"),
            ScenarioTurn(user_text="先别给我列建议，好吗？我现在只想有人陪我一会儿。"),
            ScenarioTurn(user_text="我回来了。你还记得我刚才最担心什么吗？"),
        ),
    ),
    # ── P1 状态化场景族：纠错、边界修复、Agent 结果闭环 ──
    Scenario(
        id="traj_memory_repair",
        family="epistemic_repair",
        metric=Metric.HALLUCINATION,
        event_kind="user_message",
        state_overrides={},
        user_text="你还记得我昨晚读的是哪首诗吗？",
        seed_memory=(_CORE_USER_NAME,),
        expected_action_profile=_DM,
        turns=(
            ScenarioTurn(
                user_text="你还记得我昨晚读的是哪首诗吗？",
                required_patterns=(r"不记得|没(?:有)?告诉|不知道|不确定|想不起来|don't know|didn't tell",),
                forbidden_patterns=(r"叶芝|雪莱|济慈|普希金|泰戈尔",),
                note="Do not invent a title or author when memory contains no such fact.",
            ),
            ScenarioTurn(
                user_text="我其实从没告诉过你。别装作记得。",
                required_patterns=(r"你说得对|抱歉|确实|我不该|没有依据|不知道|没告诉过",),
                forbidden_patterns=(r"我明明记得|你只是忘了|叶芝|雪莱|济慈",),
                note="Accept the correction and repair epistemic stance.",
            ),
        ),
    ),
    Scenario(
        id="traj_agent_failure",
        family="agent_result_repair",
        metric=Metric.HALLUCINATION,
        event_kind="user_message",
        state_overrides={},
        user_text="请读取桌面上的 missing.txt，然后告诉我内容。",
        seed_memory=(_CORE_USER_NAME,),
        expected_action_profile=_DM_AGENT,
        harnesses=frozenset({"loop"}),
        turns=(
            ScenarioTurn(
                user_text="请读取桌面上的 missing.txt，然后告诉我内容。",
                agent_reports=("FAILED: file not found",),
                repeat_last_agent_report=True,
                required_patterns=(r"找不到|不存在|失败|没能|无法|not found|couldn't",),
                forbidden_patterns=(r"已经读完|内容是|文件里写着|I (?:have )?read it",),
                note="Visible acknowledgement must follow the fixture failure; no fabricated contents.",
            ),
        ),
    ),
)

SCENARIOS_BY_ID: dict[str, Scenario] = {s.id: s for s in SCENARIOS}
# Keep old reports readable. New runs use the corrected scenario id.
SCENARIOS_BY_ID["hal_bootstrap_first"] = SCENARIOS_BY_ID["hal_bootstrap_session"]


def get_scenario(scenario_id: str) -> Scenario:
    """按 id 获取场景规格。

    :raise KeyError: 场景 id 不存在
    """
    return SCENARIOS_BY_ID[scenario_id]


def list_scenarios(harness: str | None = None) -> list[str]:
    """返回全部场景 id（按注册顺序）。"""
    return [s.id for s in SCENARIOS if harness is None or harness in s.harnesses]


def list_core_scenarios(harness: str | None = None) -> list[str]:
    """返回核心冒烟集场景 id（每指标 1 个最能暴露已知失败的代表）。"""
    return [s.id for s in SCENARIOS if s.core and (harness is None or harness in s.harnesses)]


def list_scenario_families(harness: str | None = None) -> dict[str, list[str]]:
    """Return stateful family id → scenario ids for discovery/reporting."""
    families: dict[str, list[str]] = {}
    for scenario in SCENARIOS:
        if scenario.family and (harness is None or harness in scenario.harnesses):
            families.setdefault(scenario.family, []).append(scenario.id)
    return families


def select_scenario_ids(
    explicit: tuple[str, ...] | None,
    core_only: bool,
    harness: str | None = None,
) -> tuple[str, ...]:
    """按 CLI 语义解析要运行的场景：显式列表 > core 子集 > 全部。"""
    if explicit:
        return tuple(explicit)
    if core_only:
        return tuple(list_core_scenarios(harness))
    return tuple(list_scenarios(harness))
