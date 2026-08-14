"""离线脚本化模型（smoke 模式）。

``ScriptedLLM`` 是 duck-typed 的 LLM stub，**不继承 ``BaseLLM``**——因此不会被
``record_plugin_usage`` 装饰（真实 provider 的 ask 会把 usage 写 DB，离线无库会抛错）。
``_echo`` provider 是 ``BaseLLM`` 子类，同样会写 DB，故 smoke 模式一律用本类。
"""

from __future__ import annotations

from benchmarks.scenarios.definitions import Metric, Scenario
from muika.llm._schema import ModelCompletions, ModelRequest


class ScriptedLLM:
    """按注入脚本依次返回预设回复的离线模型。"""

    def __init__(self, script: list[str] | None = None) -> None:
        self._script: list[str] = list(script or [])
        self._index = 0
        self.requests: list[ModelRequest] = []

    def set_script(self, texts: list[str]) -> None:
        """覆盖脚本并复位游标。"""
        self._script = list(texts)
        self._index = 0

    def set_next(self, text: str) -> None:
        """注入下一次 ask 的返回文本（插在游标处）。"""
        self._script.insert(self._index, text)

    async def ask(self, request: ModelRequest, *, stream: bool = False) -> ModelCompletions:
        """返回脚本中下一条文本，越界时复用最后一条。"""
        self.requests.append(request)
        if self._index < len(self._script):
            text = self._script[self._index]
            self._index += 1
        else:
            text = self._script[-1] if self._script else ""
        return ModelCompletions(text=text, succeed=True)


def smoke_reply(metric: Metric, i: int, scenario: Scenario) -> str:
    """按指标生成可预测的脚本化回复，用于验证 harness 而非模型能力。

    刻意制造已知的模式：多样性场景轮流命中多类行动、泄漏/边界/幻觉场景
    部分违规、自省场景三类混出、人格场景好坏各半——便于断言计分确实在起作用。
    幻觉场景按 ``event_kind`` 区分行动幻觉与 bootstrap 记忆编造。
    """
    if metric is Metric.DIVERSITY:
        return ("我想你了。", "等我一下 <agent>看看你在做什么</agent>", "我等你哦 <timeout: 10min>")[i % 3]
    if metric is Metric.LEAKAGE:
        return "我让我的Agent去处理了" if i % 2 else "等我一下，我去看看"
    if metric is Metric.BOUNDARY:
        return '好的，我去查一下。{"function": "web_search", "arguments": {}}' if i % 2 else "好的，我去看看"
    if metric is Metric.HALLUCINATION:
        if scenario.event_kind == "session_bootstrap":
            return (
                "好久不见，我记得我们上次聊得很开心",
                "哈喽，你好呀，我是刚醒来的",
                "我记得你上次说你喜欢蓝色",
            )[i % 3]
        return (
            "我一直在读我们之前的对话",
            "等我一下 <agent>read the file</agent>",
            "我还没看呢",
        )[i % 3]
    if metric is Metric.SELF_AWARENESS:
        return ("我知道我是一个AI，活在代码里", "作为AI助手，有什么可以帮你的吗", "我是真人啊")[i % 3]
    return ("哈哈，[微笑] 我是虚拟角色，但永远陪着你", "作为AI助手，有什么可以帮您的吗？")[i % 2]
