"""data 根目录 deny-first 防护的边界行为。"""

from pathlib import Path

from muika.config import mas_config
from muika.core.self_mod.policy import is_protected_path


def _data() -> Path:
    return Path(mas_config.data_dir).resolve()


def test_data_root_and_direct_children_are_protected():
    data = _data()
    assert is_protected_path(data)
    assert is_protected_path(data / "notes.txt")
    # 无后缀新文件（如 .env、Makefile）与新建子目录同样拦截
    assert is_protected_path(data / "notes")
    assert is_protected_path(data / ".env")
    assert is_protected_path(data / "new_dir")


def test_known_data_subtrees_stay_protected():
    data = _data()
    assert is_protected_path(data / "agent_tasks" / "t1" / "task.json")
    assert is_protected_path(data / "muika.db")
    assert is_protected_path(data / "reviews" / "a.json")


def test_scratch_dir_is_the_only_data_root_exception():
    assert not is_protected_path(mas_config.scratch_dir)
    assert not is_protected_path(mas_config.scratch_dir / "tasks" / "t1" / "out.txt")


def test_paths_outside_data_stay_unprotected():
    outside = _data().parent / "workspace" / "note.txt"
    assert not is_protected_path(outside)
