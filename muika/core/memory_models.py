"""记忆素材、日记、事实与持续状态的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from muika.models import Resource


class MemoryCategory(str, Enum):
    """按玩家、自身、世界和关系区分事实的归属。"""

    USER = "user"
    SELF = "self"
    WORLD = "world"
    RELATION = "relation"


@dataclass
class SessionTurn:
    """当前工作上下文中的一条对话。"""

    role: Literal["user", "muika", "agent"]
    """区分玩家输入、Muika 回复和行动观察。"""
    content: str
    """供后续模型请求使用的对话或观察正文。"""
    timestamp: datetime = field(default_factory=datetime.now)
    """原始经历发生的本地时间。"""
    resources: list[Resource] = field(default_factory=list)
    """随本条经历保存的图片、音频等资源。"""
    id: int = 0
    """对应的 experience 主键；未关联持久记录时为 0。"""


class SessionState(BaseModel):
    """保存工作会话标识与初次相遇状态。"""

    session_id: str = Field(default_factory=lambda: uuid4().hex)
    """当前工作会话的稳定标识，开始新会话时更换。"""
    started_at: datetime = Field(default_factory=datetime.now)
    """当前工作会话的开始时间。"""
    is_first_session: bool = True
    """是否尚无既往交往记录；程序重启本身不会重置为真。"""


class Experience(BaseModel):
    """表示可按来源回查的持久经历正文。"""

    id: int
    """experience 表的主键，也用于 experience:N 来源引用。"""
    session_id: str
    """记录写入时所属的工作会话。"""
    kind: Literal["user", "muika", "agent", "note", "state", "legacy"]
    """区分对话、行动观察、待整理笔记、状态更新和旧版素材。"""
    content: str
    """保存的素材正文；普通回查会另外过滤私有内容。"""
    occurred_at: datetime
    """经历发生的本地时间，用于划分自然日。"""
    source: str | None = None
    """可选的外部来源标识，用于任务结果等事件的重复投递去重。"""

    def describe(self) -> str:
        """为经历正文附上来源编号、发生时间与素材类型。"""
        return f"[experience:{self.id} | {self.occurred_at:%Y-%m-%d %H:%M:%S} | {self.kind}] {self.content}"


class Fact(BaseModel):
    """表示一个有效原子事实及其来源和回顾权重。"""

    id: int
    """当前事实版本的主键，修正后会产生新的版本编号。"""
    category: MemoryCategory
    """事实的归属类别，与回顾权重无关。"""
    key: str
    """包含主体与属性的键，例如 master.favorite_drink。"""
    value: str
    """原子事实的正文，常驻摘要直接使用此内容。"""
    source_refs: list[str] = Field(default_factory=list)
    """支持该事实的 experience:N、diary:N 或 fact:N 引用。"""
    weight: float = 1.0
    """在 weight_at 时刻的累计回顾权重，不表示事实可信度。"""
    weight_at: datetime = Field(default_factory=datetime.now)
    """权重衰减的计算基准时间。"""
    observed_at: datetime = Field(default_factory=datetime.now)
    """最近支持该版本的经历时间，防止补录旧素材覆盖新事实。"""
    last_recalled_at: datetime = Field(default_factory=datetime.now)
    """最近回顾日的计权时间，用于同权事实的排序。"""

    def score(self, now: datetime) -> float:
        """返回按 90 天半衰期衰减后的权重，不修改已保存的权重。"""
        days = max(0.0, (now - self.weight_at).total_seconds() / 86400)
        return self.weight * 2 ** (-days / 90)

    def describe(self) -> str:
        """为事实正文附上版本编号、类别与事实键。"""
        return f"[fact:{self.id}] {self.category.value}/{self.key}: {self.value}"


class Diary(BaseModel):
    """保存某一天的自省日记或带来源标记的旧会话摘要。"""

    id: int
    """diary 表的主键，也用于 diary:N 来源引用。"""
    day: date
    """日记对应的本地自然日，不是生成日记的日期。"""
    content: str
    """日记正文；不直接复制私有思考。"""
    source: str
    """唯一整理标识，如 dream:2026-09-12；旧摘要使用 legacy_archive 标记。"""
    source_refs: list[str] = Field(default_factory=list)
    """本次整理实际提供给模型的来源引用，便于继续回查。"""
    covered_through: int = 0
    """该日已处理素材的最大 experience 主键，重试据此去重。"""
    created_at: datetime
    """此记录首次保存的时间。"""


class Intention(BaseModel):
    """记录可跨会话延续的心愿及其真实任务关联。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    """同一心愿复用的稳定标识，由模型提出。"""
    description: str = Field(min_length=1, max_length=1200)
    """她想继续做、了解或解决的事情。"""
    status: Literal["open", "acting", "awaiting_feedback", "resolved", "abandoned"] = "open"
    """open 为待处理，acting 为执行中，awaiting_feedback 为等待反馈，resolved 为已解决，abandoned 为放下。"""
    source_refs: list[str] = Field(default_factory=list)
    """提出或更新心愿所依据的经历引用。"""
    task_id: str | None = None
    """由运行时关联的真实 Agent 任务编号，模型更新不能任意改写。"""
    updated_at: datetime = Field(default_factory=datetime.now)
    """最近一次状态变更的本地时间，由保存逻辑更新。"""


class StateUpdate(BaseModel):
    """表示主人格回复或日记整理提交的局部状态更新。"""

    model_config = ConfigDict(extra="forbid")

    mood: str | None = Field(default=None, max_length=500)
    """新的持续情绪描述；None 表示保留现有情绪。"""
    reason: str = Field(min_length=1, max_length=1200)
    """本次情绪或心愿变化的原因。"""
    intentions: list[Intention] = Field(default_factory=list, max_length=20)
    """按 id 合并的心愿更新；空列表不会清空已有心愿。"""


class PersistentState(BaseModel):
    """保存独立即时驱动的持续情绪、内在张力与心愿。"""

    mood: str = ""
    """自然语言描述的持续情绪，可同时包含生气与期待等感受。"""
    reason: str = ""
    """最近一次持续情绪变化的原因。"""
    mood_updated_at: datetime | None = None
    """情绪更新时间，较早日记不能覆盖更晚的白天更新。"""
    dissonance: float = Field(default=0.0, ge=0, le=1)
    """0 到 1 的未解决张力，做梦时依据新增经历调整。"""
    intentions: list[Intention] = Field(default_factory=list)
    """尚在追踪的心愿及已解决、已放下的处理记录。"""
    last_considered_at: datetime | None = None
    """最近一次主动思考时间，用于冷却；选择沉默也会记录。"""

    def describe(self) -> str:
        """将持续情绪、张力和各心愿的任务状态编入模型提示。"""
        lines = [f"Lasting feeling: {self.mood or 'No lasting feeling recorded.'}", f"Why: {self.reason}"]
        lines.append(f"Unresolved tension: {self.dissonance:.2f}")
        for intention in self.intentions:
            lines.append(
                f"Intention {intention.id} ({intention.status}): {intention.description}"
                + (f" [action task: {intention.task_id}]" if intention.task_id else "")
            )
        return "\n".join(lines)


class MemorySnapshot(BaseModel):
    """保存恢复运行所需的元数据、持续状态和工作摘要。"""

    session: SessionState = Field(default_factory=SessionState)
    """当前工作会话的信息。"""
    state: PersistentState = Field(default_factory=PersistentState)
    """跨会话和重启保留的持续状态。"""
    first_interaction_at: datetime | None = None
    """最早已知的交往时间，独立于事实排名保存。"""
    last_dream_at: datetime | None = None
    """最近一次成功提交日记的时间。"""
    legacy_imported: bool = False
    """旧版记忆是否已完成事务导入，防止重启后重复导入。"""
    legacy_consolidated: bool = False
    """导入后是否已成功完成首次新日记整理。"""
    working_summary: str = ""
    """用于缩短模型输入的工作摘要，不属于日记，也不增加事实权重。"""
    summary_through: int = 0
    """当前会话已被工作摘要覆盖的最大 experience 主键。"""


class FactUpdate(BaseModel):
    """描述新增、修正或归并一个原子事实的提案。"""

    model_config = ConfigDict(extra="forbid")

    category: MemoryCategory
    """事实归属类别，与 key 一起确定主体属性。"""
    key: str = Field(min_length=1, max_length=200)
    """包含主体与属性的稳定事实键。"""
    value: str = Field(min_length=1, max_length=1500)
    """拟保存的新事实正文。"""
    source_refs: list[str] = Field(min_length=1)
    """支持更新的来源；新增和修正须有当天经历，等价归并可引用旧事实。"""
    supersedes: list[int] = Field(default_factory=list)
    """被当前事实取代的旧版本编号；不能混合不同主体。"""


class FactRetraction(BaseModel):
    """描述依据新经历撤下一个失效事实的提案。"""

    fact_id: int
    """需要撤下的事实版本编号。"""
    source_refs: list[str] = Field(min_length=1)
    """证明该事实失效的来源，须包含当天的新依据。"""
    reason: str = Field(min_length=1)
    """撤下事实的原因。"""


class DreamResult(BaseModel):
    """表示一次日记整理中需要共同提交的日记、事实及状态变化。"""

    model_config = ConfigDict(extra="forbid")

    diary: str = Field(min_length=1)
    """本次生成的整篇日记正文。"""
    facts: list[FactUpdate] = Field(default_factory=list)
    """本次提出的原子事实新增、修正或归并。"""
    retractions: list[FactRetraction] = Field(default_factory=list)
    """本次提出的事实失效处理。"""
    recalled_fact_ids: list[int] = Field(default_factory=list)
    """日记明确回顾的已有事实编号，单日最多强化一次。"""
    state_update: StateUpdate | None = None
    """可选的持续情绪和心愿更新，保留晚于来源经历的既有状态。"""
    dissonance_delta: float = Field(default=0.0, ge=-1, le=1)
    """本次根据新增经历提出的张力变化量，提交后总值限制在 0 到 1。"""
    tension_reason: str = ""
    """张力变化的原因；变化量非零时必须提供。"""
    tension_source_refs: list[str] = Field(default_factory=list)
    """支持张力变化的来源，须包含尚未整理的新经历。"""
    relief: Literal["none", "action_without_feedback", "positive_feedback", "reflection"] = "none"
    """缓解依据；action_without_feedback 最多缓解 0.05，positive_feedback 须引用玩家反馈。"""


class MemoryQuery(BaseModel):
    """描述 SQLite 候选筛选使用的关键词与日期范围。"""

    terms: list[str] = Field(default_factory=list, max_length=8)
    """最多八个关键词或短语，任一词命中即可进入候选。"""
    start: date | None = None
    """包含在内的起始本地日期；None 表示不限制。"""
    end: date | None = None
    """包含在内的结束本地日期；None 表示不限制。"""


class RecallHit(BaseModel):
    """表示一条检索候选及其精确回查入口。"""

    ref: str
    """命中记录的来源引用，如 experience:12。"""
    content: str
    """经过私有内容过滤的正文或相关片段，不一定是完整原文。"""
    occurred_at: str
    """展示用日期或时间字符串，格式随来源类型而定。"""
    source_refs: list[str] = Field(default_factory=list)
    """事实或日记进一步引用的证据入口。"""

    def describe(self) -> str:
        """为检索正文附上可回查的来源引用与发生时间。"""
        return f"[{self.ref} | {self.occurred_at}] {self.content}"


class RecallResult(BaseModel):
    """返回语义筛选结果，或检索失败时保留的关键词候选。"""

    hits: list[RecallHit] = Field(default_factory=list)
    """本次可供使用和继续回查的记忆候选。"""
    degraded: bool = False
    """查询扩写或语义筛选是否失败；为真时保留可用的日期和关键词结果。"""
    error: str | None = None
    """降级的具体原因；正常检索时为 None。"""

    def describe(self) -> str:
        """汇总检索命中，并在降级时说明结果仅来自日期和关键词筛选。"""
        prefix = "Semantic recall is unavailable; these are date/keyword matches.\n" if self.degraded else ""
        return prefix + "\n".join(hit.describe() for hit in self.hits)
