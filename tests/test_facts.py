"""``build_fact_block`` 的可判定事实（时间/未回应计数）。"""

from datetime import datetime, timedelta

from muika.core.brain import build_fact_block
from muika.core.memory import MemoryManager
from muika.core.memory_models import SessionTurn
from muika.core.state import MuikaState


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
