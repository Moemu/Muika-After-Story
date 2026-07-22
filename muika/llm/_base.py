from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    List,
    Literal,
    Sequence,
    Union,
    overload,
)

from ._config import ModelConfig
from ._schema import (
    ModelCompletions,
    ModelRequest,
    ModelStreamCompletions,
)

if TYPE_CHECKING:
    from muika.core.memory import SessionTurn


class BaseLLM(ABC):
    """
    模型基类，所有模型加载器都必须继承于该类

    推荐使用该基类中定义的方法构建模型加载器类，但无论如何都必须实现 `ask` 方法
    """

    def __init__(self, model_config: ModelConfig) -> None:
        """
        统一在此处声明变量
        """
        self.config = model_config
        """模型配置"""
        self.is_running = False
        """模型状态"""

    def __init_subclass__(cls, **kwargs):
        """
        对实现类中的 `ask` 函数包装 `record_plugin_usage` 装饰器
        """
        from ._wrapper import record_plugin_usage

        super().__init_subclass__(**kwargs)

        # 1. Get the original 'ask' method from the new subclass
        original_ask = cls.ask

        # 2. Wrap it with the decorator
        decorated_ask = record_plugin_usage(original_ask)

        # 3. Replace the original method on the subclass with the decorated version
        setattr(cls, "ask", decorated_ask)

    def _require(self, *require_fields: str):
        """
        通用校验方法：检查指定的配置项是否存在，不存在则抛出错误

        :param require_fields: 需要检查的字段名称（字符串）
        """
        missing_fields = [field for field in require_fields if not getattr(self.config, field, None)]
        if missing_fields:
            raise ValueError(f"对于 {self.config.provider} 以下配置是必需的: {', '.join(missing_fields)}")

    @staticmethod
    def _normalize_session_turns(
        turns: Sequence["SessionTurn"],
        *,
        merge_agent=True,
    ) -> List["SessionTurn"]:
        """
        将任意顺序的 SessionTurn 转换为 SDK 可接受的 user/assistant 交替格式。

        规则:
        - agent -> assistant
        - 连续 assistant 合并
        - 连续 user 合并
        """
        from muika.core.memory import SessionTurn

        normalized: List[SessionTurn] = []
        turns = deepcopy(turns)

        for current in turns:

            # agent 归一化
            if merge_agent and current.role == "agent":
                current.role = "user"

            # 处理开头 assistant
            if not normalized and current.role == "muika":
                normalized.append(SessionTurn(role="user", content="[conversation resumed]"))

            # 合并连续相同 role
            if normalized and normalized[-1].role == current.role:
                normalized[-1].content += "\n" + current.content
                normalized[-1].resources.extend(current.resources)
                continue

            normalized.append(current)

        # 保证 user 开头
        if normalized and normalized[0].role != "user":
            normalized.insert(
                0,
                SessionTurn(
                    role="user",
                    content="",
                ),
            )

        return normalized

    def _build_messages(self, request: "ModelRequest") -> list:
        """
        构建对话上下文历史的函数
        """
        raise NotImplementedError

    async def _ask_sync(
        self, messages: list, tools: Any, response_format: Any, total_tokens: int = 0
    ) -> "ModelCompletions":
        """
        同步模型调用
        """
        raise NotImplementedError

    def _ask_stream(
        self, messages: list, tools: Any, response_format: Any, total_tokens: int = 0
    ) -> AsyncGenerator["ModelStreamCompletions", None]:
        """
        流式输出
        """
        raise NotImplementedError

    @overload
    async def ask(self, request: "ModelRequest", *, stream: Literal[False] = False) -> "ModelCompletions": ...

    @overload
    async def ask(
        self, request: "ModelRequest", *, stream: Literal[True] = True
    ) -> AsyncGenerator["ModelStreamCompletions", None]: ...

    @abstractmethod
    async def ask(
        self, request: "ModelRequest", *, stream: bool = False
    ) -> Union["ModelCompletions", AsyncGenerator["ModelStreamCompletions", None]]:
        """
        模型交互询问

        :param request: 模型调用请求体
        :param stream: 是否开启流式对话

        :return: 模型输出体
        """
        pass
