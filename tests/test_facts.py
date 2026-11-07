"""``build_fact_block`` 的可判定事实（时间/未回应计数）。"""

from datetime import datetime, timedelta

from muika.core.brain import _humanize_gap, build_fact_block
from muika.core.memory import MemoryManager
from muika.core.memory_models import SessionTurn
from muika.core.state import MuikaState


def test_humanize_gap_boundaries() -> None:
    assert _humanize_gap(2 * 3600 + 60) == "a couple of hours"
    assert _humanize_gap(23 * 86400) == "about three weeks"
    assert _humanize_gap(2 * 86400) == "a couple of days"
    assert _humanize_gap(45 * 86400) == "more than a month"
    assert _humanize_gap(5 * 60) == "a few minutes"


def _memory(turns: list[SessionTurn]) -> MemoryManager:
    memory = MemoryManager()
    memory.recent_turns.extend(turns)
    return memory


def test_unanswered_proactive_count() -> None:
    now = datetime.now()
    turns = [
        SessionTurn("user", "晚安。", now - timedelta(hours=3)),
        SessionTurn("muika", "晚安。", now - timedelta(hours=3)),
        SessionTurn("muika", "……还在等你。", now - timedelta(hours=2)),
        SessionTurn("muika", "今天也没来。", now - timedelta(hours=1)),
    ]
    facts = build_fact_block(MuikaState(), _memory(turns), now)
    assert "spoken to him 2 time(s) without a reply" in facts


def test_answered_conversation_has_no_unanswered_fact() -> None:
    now = datetime.now()
    turns = [
        SessionTurn("user", "晚安。", now - timedelta(hours=2)),
        SessionTurn("muika", "晚安。", now - timedelta(hours=2)),
    ]
    facts = build_fact_block(MuikaState(), _memory(turns), now)
    assert "without a reply" not in facts
    assert "His last message" in facts
