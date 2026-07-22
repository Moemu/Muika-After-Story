from __future__ import annotations

import atexit
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import yaml
from nonebot import logger
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from muika.config import BUILTIN_SKILLS_PATH, USER_SKILL_PATHS, mas_config

_SKILL_FILENAME = "SKILL.md"
"""技能定义文件名"""

_DESCRIPTION_MAX_CHARS = 200
"""frontmatter 缺失时，回退描述的最大长度"""

_FRONTMATTER_PATTERN = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
"""匹配文件开头的 YAML frontmatter 块"""


@dataclass(frozen=True)
class AgentSkill:
    """一个可用技能的元数据"""

    name: str
    description: str
    location: Path  # 指向 SKILL.md 的绝对路径


def _parse_skill_md(path: Path) -> tuple[str, str]:
    """
    解析 SKILL.md，提取技能名与描述。

    优先读取 YAML frontmatter 中的 name / description；
    frontmatter 缺失或损坏时回退：name 取目录名，description 取首个 Markdown 标题，
    再退化为首个非空行（截断至 _DESCRIPTION_MAX_CHARS）。

    :param path: SKILL.md 文件路径
    :return: (name, description)
    """
    fallback_name = path.parent.name
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"读取技能文件失败 {path}: {e}")
        return fallback_name, ""

    name: Optional[str] = None
    description: Optional[str] = None

    match = _FRONTMATTER_PATTERN.match(text)
    if match:
        try:
            meta = yaml.safe_load(match.group(1))
            if isinstance(meta, dict):
                if isinstance(meta.get("name"), str) and meta["name"].strip():
                    name = meta["name"].strip()
                if isinstance(meta.get("description"), str) and meta["description"].strip():
                    description = meta["description"].strip()
        except yaml.YAMLError:
            # frontmatter 损坏时静默回退
            pass

    if name is None:
        name = fallback_name

    if description is None:
        body = text[match.end() :] if match else text
        description = ""
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # 首个 Markdown 标题优先，否则取首个非空行
            description = stripped.lstrip("#").strip() if stripped.startswith("#") else stripped
            break
        description = description[:_DESCRIPTION_MAX_CHARS]

    return name, description


def _scan_roots(roots: Iterable[Path]) -> dict[str, AgentSkill]:
    """
    扫描各根目录下的 <skill-dir>/SKILL.md，构建技能注册表。

    后出现的根目录覆盖先前的（调用方应按 内置 -> 用户 的顺序传入，
    使用户级技能优先于内置技能）。

    :param roots: 技能根目录列表
    :return: name -> AgentSkill 的映射
    """
    skills: dict[str, AgentSkill] = {}
    for root in roots:
        if not root.is_dir():
            logger.debug(f"技能目录不存在，跳过: {root}")
            continue
        for skill_md in sorted(root.glob(f"*/{_SKILL_FILENAME}")):
            name, description = _parse_skill_md(skill_md)
            if name in skills:
                logger.warning(f"技能名冲突: '{name}' ({skill_md}) 将覆盖 ({skills[name].location})")
            skills[name] = AgentSkill(name=name, description=description, location=skill_md.resolve())
    return skills


class SkillFileHandler(FileSystemEventHandler):
    """技能目录变化处理器：任何文件变动都触发一次全量重扫（带冷却防抖）"""

    def __init__(self, callback):
        self.callback = callback
        self.last_triggered = time.time()
        # 防止编辑器一次保存触发多次回调
        self.cooldown = 1  # 冷却时间（秒）

    def _on_any_event(self, event):
        current_time = time.time()
        if current_time - self.last_triggered > self.cooldown:
            self.last_triggered = current_time
            self.callback()

    on_created = _on_any_event
    on_modified = _on_any_event
    on_deleted = _on_any_event
    on_moved = _on_any_event


class SkillManager:
    """技能管理器：启动时扫描技能目录，并通过文件监听实现热重载"""

    _instance: Optional["SkillManager"] = None
    _lock = threading.Lock()
    _initialized: bool

    def __new__(cls):
        """确保实例在单例模式下运行"""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(SkillManager, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return

        self._skills: dict[str, AgentSkill] = {}
        """当前技能注册表（name -> AgentSkill），替换时整体原子交换"""
        self._skills_lock = threading.Lock()
        """保护 _skills 替换（观察者线程写入，asyncio 线程读取）"""
        self.observer: Optional[BaseObserver] = None
        """文件监视器"""
        self._watched_roots: set[Path] = set()
        """已调度的监听根目录，避免重复 schedule"""

        self.reload()
        self._start_watcher()

        self._initialized = True

    def _skill_roots(self) -> list[Path]:
        """按优先级从低到高返回技能根目录（用户目录在后，扫描时覆盖内置同名技能）"""
        roots = [BUILTIN_SKILLS_PATH]
        if mas_config.load_user_skills:
            roots.extend(USER_SKILL_PATHS)
        return roots

    def reload(self) -> None:
        """全量重扫技能目录，原子替换注册表；同时为新出现的根目录补充监听"""
        skills = _scan_roots(self._skill_roots())
        with self._skills_lock:
            self._skills = skills
        logger.debug(f"技能扫描完成，共 {len(skills)} 个: {', '.join(skills) or '(无)'}")
        self._watch_new_roots()

    def _start_watcher(self) -> None:
        """启动文件监视器"""
        if self.observer is not None:
            self.observer.stop()
            self.observer.join()

        self.observer = Observer()
        self._event_handler = SkillFileHandler(self._on_skills_changed)
        self._watched_roots = set()
        self._watch_new_roots()
        self.observer.start()

    def _watch_new_roots(self) -> None:
        """为尚未监听的已存在根目录调度监听（处理运行期间才创建的目录）"""
        if self.observer is None:
            return
        for root in self._skill_roots():
            if root.is_dir() and root not in self._watched_roots:
                self.observer.schedule(self._event_handler, str(root), recursive=True)
                self._watched_roots.add(root)

    def _on_skills_changed(self) -> None:
        """技能目录变化回调"""
        try:
            self.reload()
        except Exception as e:
            logger.error(f"重新扫描技能目录失败: {e}")

    def get(self, name: str) -> Optional[AgentSkill]:
        """按名称获取技能元数据，不存在时返回 None"""
        with self._skills_lock:
            return self._skills.get(name)

    @property
    def skills(self) -> list[AgentSkill]:
        """当前全部技能的快照副本"""
        with self._skills_lock:
            return list(self._skills.values())

    def render_prompt_section(self) -> str:
        """
        生成注入系统提示的技能元数据段落（仅 name + description）。
        无可用技能时返回空字符串。
        """
        skills = self.skills
        if not skills:
            return ""
        lines = [
            "Available skills (use the load_skill tool with the exact skill name "
            "to load full instructions before attempting the task):"
        ]
        lines.extend(f"- {s.name}: {s.description}" for s in skills)
        return "\n".join(lines)

    def stop_watcher(self) -> None:
        """停止文件监视器"""
        if self.observer is None:
            return
        self.observer.stop()
        self.observer.join()


_skill_manager: Optional[SkillManager] = None


def get_skill_manager() -> SkillManager:
    """获取技能管理器单例（首次调用时执行启动扫描并启动监听）"""
    global _skill_manager
    if _skill_manager is None:
        _skill_manager = SkillManager()
        atexit.register(_skill_manager.stop_watcher)
    return _skill_manager
