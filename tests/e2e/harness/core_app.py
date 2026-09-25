"""进程内 Core 编排器：真实事件循环、真实 SQLite 与真实模板，仅替换 LLM 与外发通道。"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

from muika.config import mas_config
from muika.core.events import (
    Event,
    SessionBootstrapEvent,
    SessionEndEvent,
    TimeTickEvent,
    UserMessageEvent,
    UserMessagePayload,
)
from muika.core.executor import Executor
from muika.core.loop import Muika
from muika.database.db import close_db, init_db
from muika.ipc.protocol import SendMessage
from muika.models import Message, Resource
from muika.plugin.command import CommandDispatcher

from .scripted_llm import ScriptedLLM, ScriptedTurn
from .trace import TraceRecorder


def _summarize_event(event: Event) -> str:
    """提取事件的可读摘要用于轨迹记录。"""
    if event.type == "user_message":
        return event.payload.message.message
    if event.type == "scheduled_trigger":
        return event.payload.what
    if event.type == "agent_task":
        return f"{event.task_id[:8]} {event.status}"
    return ""


class _StubConfigManager:
    """替代模型配置管理器：不启动文件 watcher，仅提供可调的心跳强度。"""

    def __init__(self, heart_intensity: str) -> None:
        self.heart_intensity = heart_intensity

    def register_listener(self, callback: Callable) -> None:
        """E2E 中模型配置不会热更新，注册为空操作。"""


class _StubSkillManager:
    """替代技能管理器：不扫描目录、不启动 watchdog 监听线程。"""

    def render_prompt_section(self) -> str:
        return ""


class CoreApp:
    """一个进程内运行的真实 Core，外发消息被收集，LLM 由剧本驱动。

    :param monkeypatch: 测试的 monkeypatch 夹具，所有补丁随测试结束自动还原
    :param recorder: 轨迹记录器
    :param turns: 场景剧本回合
    :param heart_intensity: 注入人设模板的心跳强度（off/low/medium/high）
    """

    def __init__(
        self,
        monkeypatch,
        recorder: TraceRecorder,
        *,
        turns: Sequence[ScriptedTurn] = (),
        heart_intensity: str = "off",
    ) -> None:
        self._monkeypatch = monkeypatch
        self._recorder = recorder
        self.recorder = recorder
        """供传输层等外部观察者记录 wire 帧；与 ``_recorder`` 同一实例。"""
        self.scripted = ScriptedLLM(turns, recorder=recorder)
        self._install_plumbing_routes()
        self.sent: list[str] = []
        self.command_replies: list[str] = []
        self._outbox: asyncio.Queue[str] = asyncio.Queue()
        self._processed: asyncio.Queue[str] = asyncio.Queue()
        self._apply_patches(heart_intensity)
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self.executor = Executor(self._queue, self._collect)
        self.muika: Optional[Muika] = None
        self._stopped = False

    def _install_plumbing_routes(self) -> None:
        """为记忆检索等内部管线安装默认路由，使其不消耗场景剧本。"""
        self.scripted.add_route(
            when=lambda req: "Expand a memory query" in (req.system or ""),
            text='{"terms": []}',
            name="memory_query_expansion",
        )
        self.scripted.add_route(
            when=lambda req: "Select source references" in (req.system or ""),
            text='{"refs": []}',
            name="memory_recall_selection",
        )

    def _apply_patches(self, heart_intensity: str) -> None:
        """在构造 Muika 前替换 LLM 加载与后台监听类副作用。"""
        mp = self._monkeypatch
        config = self.scripted.config

        def load_scripted(cfg=None) -> ScriptedLLM:
            return self.scripted

        def get_config(name=None):
            return config

        manager = _StubConfigManager(heart_intensity)
        mp.setattr("muika.core.brain.load_model", load_scripted)
        mp.setattr("muika.core.brain.get_model_config", get_config)
        mp.setattr("muika.core.brain.get_model_config_manager", lambda: manager)
        mp.setattr("muika.core.agent.agent.load_model", load_scripted)
        mp.setattr("muika.core.agent.agent.get_model_config", get_config)
        mp.setattr("muika.core.agent.agent.get_skill_manager", _StubSkillManager)
        mp.setattr("muika.core.digest_agent.get_model_config", get_config)
        mp.setattr("muika.core.digest_agent.load_model", load_scripted)
        # 消除消息分段间延迟，保证场景快速且确定
        mp.setattr("muika.core.executor.DELAYED_SECOND_PER_PARAGRAPH", 0)
        # 推迟后台阅读摘要的首次触发，避免测试期间发生网络抓取
        mp.setattr("muika.core.loop.DIGEST_STARTUP_DELAY", 24 * 3600)
        mp.setattr(mas_config, "enable_auto_reflection", False)
        # 命令派发器是进程级单例，测试结束后复位，避免跨用例引用已停止的 Muika
        mp.setattr(CommandDispatcher, "_instance", None)

    def bridge_executor(self) -> None:
        """把 Executor 外发改道为 ``_bridge``：生产路径的 send_to_bot 逻辑仅在 IpcWire 可用处复用。

        场景在 ``start()`` 之后、消息到达之前调用，把外发内容同时记入 ``sent`` 和
        wire 的 ``outbound`` 队列；测试结束随 monkeypatch 自动还原。
        """
        assert self.muika is not None, "call start() first"
        self._monkeypatch.setattr(self.muika.executor, "_send_func", self._bridge)

    async def _bridge(
        self, content: str, resources: Optional[list[Resource]] = None, target: Optional[str] = None
    ) -> None:
        """生产路径的外发回调：先走真实 Executor 收集，再镜像为 SendMessage 压入 wire 队列。"""
        await self._collect(content, resources, target)
        wire = getattr(self, "_ipc_wire_outbound", None)
        if wire is not None:
            wire.append(SendMessage(content=content, resources=[r.to_dict() for r in resources or []]))

    def attach_ipc_outbound(self, outbound: list[SendMessage]) -> None:
        """把 IpcWire 的外发队列挂到 CoreApp 上；随用例结束丢弃引用，无需清理。"""
        self._ipc_wire_outbound: list[SendMessage] = outbound

    async def start(self) -> None:
        """初始化真实数据库（含 Alembic 迁移），构造并启动 Muika。"""
        # Alembic 的 script_location 相对当前工作目录解析；沙箱 cwd 下需临时切回仓库根
        repo_root = Path(__file__).resolve().parents[3]
        previous_cwd = Path.cwd()
        os.chdir(repo_root)
        try:
            await init_db()
        finally:
            os.chdir(previous_cwd)
        self.muika = Muika(self.executor, self._queue)
        await self.muika.memory.load()
        original_process = self.muika._process_event

        async def traced_process(event: Event, dt: float) -> None:
            self._recorder.record("event_in", type=event.type, summary=_summarize_event(event))
            try:
                await original_process(event, dt)
            finally:
                self._processed.put_nowait(event.type)
                self._recorder.record("event_done", type=event.type)

        self._monkeypatch.setattr(self.muika, "_process_event", traced_process)
        self.muika.start()
        # 与 CoreBootstrap 一致：命令直达派发器，不进入认知管线
        CommandDispatcher.setup(self.muika, self._collect_command)
        self._recorder.record("lifecycle", action="start")

    async def stop(self) -> None:
        """停止核心任务、释放数据库，并记录各表行数快照。幂等。"""
        if self._stopped:
            return
        self._stopped = True
        if self.muika is not None:
            await self.muika.stop()
            self.muika = None
        await close_db()
        self._recorder.record("lifecycle", action="stop")
        self._recorder.record("db_snapshot", tables=self._table_counts())

    def _table_counts(self) -> dict[str, int]:
        """读取测试数据库各表行数，作为轨迹中的持久化快照。"""
        db_path = Path(mas_config.data_dir) / "muika.db"
        if not db_path.exists():
            return {}
        try:
            with sqlite3.connect(db_path) as conn:
                tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
        except sqlite3.Error:
            return {}

    async def _collect(
        self, content: str, resources: Optional[list[Resource]] = None, target: Optional[str] = None
    ) -> None:
        """Executor 的外发回调：收集消息并写入轨迹。"""
        self._recorder.record("message_out", text=content, target=target)
        self.sent.append(content)
        self._outbox.put_nowait(content)

    async def _collect_command(self, content: str, resources: Optional[list[dict]] = None) -> None:
        """CommandDispatcher 的回复回调：命令结果与对话外发分开收集。"""
        self._recorder.record("command_out", text=content)
        self.command_replies.append(content)

    def db_query(self, sql: str, params: Sequence[object] = ()) -> list[dict]:
        """只读直查测试数据库，用于断言真实持久化结果。"""
        db_path = Path(mas_config.data_dir) / "muika.db"
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, params)]

    async def bootstrap(self, last_chat_time: Optional[datetime] = None) -> None:
        """投递会话启动事件；显式指定上次对话时间以获得确定的缺席语义。"""
        assert self.muika is not None, "call start() first"
        await self.muika.create_event(SessionBootstrapEvent(last_chat_time=last_chat_time))

    async def user_says(self, text: str) -> None:
        """投递一条用户消息事件。"""
        assert self.muika is not None, "call start() first"
        message = Message(userid=mas_config.master_id, message=text)
        await self.muika.create_event(UserMessageEvent(payload=UserMessagePayload(message=message)))

    async def say_command(self, raw: str) -> None:
        """派发一条命令（等价于 Bot 转发 command 事件），直达派发器并等待处理完成。"""
        assert self.muika is not None, "call start() first"
        self._recorder.record("command_in", raw=raw)
        await CommandDispatcher.get().dispatch(raw)

    async def end_session(self) -> None:
        """投递会话结束事件。"""
        assert self.muika is not None, "call start() first"
        await self.muika.create_event(SessionEndEvent())

    async def advance_time(self) -> None:
        """显式投递一次时间流逝事件，替代全局时钟冻结。"""
        assert self.muika is not None, "call start() first"
        await self.muika.create_event(TimeTickEvent())

    async def next_reply(self, timeout: float = 15.0) -> str:
        """等待下一条外发消息；超时时报出已发生的 LLM 调用以便排查。"""
        try:
            return await asyncio.wait_for(self._outbox.get(), timeout)
        except asyncio.TimeoutError:
            calls = [f"{call['name']} <- {call['prompt_head']!r}" for call in self.scripted.calls]
            raise TimeoutError(f"No reply within {timeout}s. LLM calls so far: {calls}") from None

    async def wait_processed(self, event_type: str, timeout: float = 10.0) -> None:
        """等待某类事件被主循环处理完成（含其数据库写入等副作用）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"Event {event_type!r} was not processed within {timeout}s")
            got = await asyncio.wait_for(self._processed.get(), remaining)
            if got == event_type:
                return
