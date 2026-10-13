"""执行 Muika 的行动意图，并连接记忆工具和日记整理。"""

from __future__ import annotations

import asyncio
import platform
import sys
from pathlib import Path

from muika.config import get_model_config, mas_config

# 导入工具模块以完成注册。
from muika.core.actions import tools as _tools  # noqa: F401
from muika.core.memory_reasoning import MemoryReasoner
from muika.core.state import MuikaState
from muika.llm import ModelRequest, load_model
from muika.llm.loader import refresh_model
from muika.plugin.func_call import get_tool_list
from muika.plugin.skills import get_skill_manager
from muika.template.loader import generate_prompt_from_template


class Agent:
    """提供行动模型、执行提示及记忆处理能力。"""

    def __init__(self) -> None:
        self.action_lock = asyncio.Lock()
        agent_cfg = get_model_config(mas_config.agent_model)
        summarize_model_cfg = get_model_config(mas_config.session_summarize_model or mas_config.agent_model)
        self.model = load_model(agent_cfg)
        self.summarize_model = load_model(summarize_model_cfg)
        self.memory_reasoner = MemoryReasoner(self.summarize_model)
        self.model.compactor = self.memory_reasoner.compactor

        # 技能管理器：启动时扫描技能目录并启动热重载监听
        self._skill_manager = get_skill_manager()

    def refresh_models(self) -> None:
        """在调用边界更新行动、日记和工作摘要使用的模型。"""
        model = refresh_model(self.model, get_model_config(mas_config.agent_model))
        summarize_model = refresh_model(
            self.summarize_model, get_model_config(mas_config.session_summarize_model or mas_config.agent_model)
        )
        self.model, self.summarize_model = model, summarize_model
        self.memory_reasoner.summarize_model = summarize_model
        self.memory_reasoner.compactor.model = summarize_model
        model.compactor = self.memory_reasoner.compactor

    def build_request(self, command: str, state: MuikaState | None = None) -> ModelRequest:
        """组装当前模板、技能、工具和实际运行环境。"""
        self.refresh_models()
        system = generate_prompt_from_template(mas_config.agent_template)
        if state is not None and state.memory is not None:
            system += "\n[Remembered context]\n" + state.memory.get_memory_prompt()
            system += "\n[Lasting state]\n" + state.memory.persistent.describe()
        skills_section = self._skill_manager.render_prompt_section()
        if skills_section:
            system += f"\n\n{skills_section}"
        system += (
            f"\n\nExecution environment: OS={platform.system()}; cwd={Path.cwd()}; "
            f"Python={sys.executable}. Default shell={'powershell' if sys.platform == 'win32' else 'bash'}. "
            "Use this environment's syntax. A running process is not a completed check."
            f" Action permission={mas_config.action_permission}; code review={mas_config.code_review_mode}; "
            f"allowed file roots={mas_config.fs_allowed_paths}."
        )
        return ModelRequest(prompt=f"Command: {command}", system=system, tools=get_tool_list(), purpose="agent_step")
