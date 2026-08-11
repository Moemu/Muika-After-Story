"""``_filesystem._apply_edit`` 纯字符串编辑算法测试。

按基线旧行为断言 delete_lines 边界：``line_start`` 越界静默 no-op、
``line_end`` 越界静默截断。若后续合入 WIP 的边界变更，需同步更新。
"""

import pytest

from muika.core.actions.tools._filesystem import _apply_edit


def test_replace_ok():
    assert _apply_edit("a\nb\nc", "replace", "b", "B", None, None, None) == "a\nB\nc"


def test_replace_not_found_raises():
    with pytest.raises(ValueError):
        _apply_edit("a\nb\nc", "replace", "z", "B", None, None, None)


def test_replace_ambiguous_raises():
    with pytest.raises(ValueError):
        _apply_edit("a a", "replace", "a", "b", None, None, None)


def test_replace_missing_args_raises():
    with pytest.raises(ValueError):
        _apply_edit("a\nb", "replace", None, None, None, None, None)


def test_insert_basic():
    assert _apply_edit("a\nc\n", "insert", None, "b", 2, None, None) == "a\nb\nc\n"


def test_insert_at_zero_prepends():
    assert _apply_edit("a\nc\n", "insert", None, "b", 0, None, None) == "b\na\nc\n"


def test_insert_beyond_eof_appends():
    assert _apply_edit("a\nc\n", "insert", None, "b", 99, None, None) == "a\nc\nb\n"


def test_insert_auto_newline():
    assert _apply_edit("a\nc\n", "insert", None, "b", 2, None, None) == "a\nb\nc\n"


def test_delete_lines_basic():
    assert _apply_edit("l1\nl2\nl3\nl4\n", "delete_lines", None, None, None, 2, 3) == "l1\nl4\n"


def test_delete_line_start_beyond_total_noop():
    assert _apply_edit("a\nb\nc\n", "delete_lines", None, None, None, 10, 12) == "a\nb\nc\n"


def test_delete_line_end_beyond_total_truncates():
    assert _apply_edit("a\nb\nc\n", "delete_lines", None, None, None, 2, 99) == "a\n"


def test_delete_lines_invalid_range_raises():
    with pytest.raises(ValueError):
        _apply_edit("a\nb\n", "delete_lines", None, None, None, 0, 2)
    with pytest.raises(ValueError):
        _apply_edit("a\nb\n", "delete_lines", None, None, None, 3, 2)


def test_unknown_operation_raises():
    with pytest.raises(ValueError):
        _apply_edit("a\n", "foo", None, None, None, None, None)
