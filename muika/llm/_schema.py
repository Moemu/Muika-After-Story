from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Literal, Optional, Sequence, Type, Union

from pydantic import BaseModel, TypeAdapter

from ..models import Resource

if TYPE_CHECKING:
    from numpy import ndarray

    from muika.core.memory import SessionTurn


@dataclass
class Usage:
    """
    Token 用量明细
    """

    input_tokens: int = 0
    """输入 Token 数（prompt tokens）"""
    output_tokens: int = 0
    """输出 Token 数（completion tokens）"""
    cached_tokens: int = 0
    """缓存命中的 Token 数（cached prompt tokens）"""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cached_rate(self) -> float:
        if self.input_tokens == 0:
            return 0.0
        return self.cached_tokens / self.input_tokens


@dataclass
class ModelRequest:
    """
    模型调用请求
    """

    prompt: str
    history: Sequence["SessionTurn"] = field(default_factory=list)
    resources: List[Resource] = field(default_factory=list)
    tools: Optional[List[dict]] = field(default_factory=list)
    system: Optional[str] = None
    format: Literal["string", "json"] = "string"
    json_schema: Optional[Union[Type[BaseModel], TypeAdapter]] = None


@dataclass
class ModelCompletions:
    """
    模型输出
    """

    text: str = ""
    """输出文本内容"""
    usage: Usage = field(default_factory=Usage)
    """调用用量明细"""
    resources: List[Resource] = field(default_factory=list)
    """模型输出多模态资源列表"""
    succeed: bool = True
    """调用成功（如不成功会在 `text` 中输出错误信息）"""


@dataclass
class ModelStreamCompletions:
    """
    模型流式输出
    """

    chunk: str = ""
    """输出文本块"""
    usage: Usage = field(default_factory=Usage)
    """调用用量明细（累增，一般取最后一个块的用量）"""
    resources: Optional[List[Resource]] = field(default_factory=list)
    """模型输出多模态资源列表"""
    succeed: bool = True
    """调用成功（如不成功会在 `chunk` 中输出错误信息）"""


@dataclass
class EmbeddingsBatchResult:
    """
    嵌入输出
    """

    embeddings: List[List[float]]
    usage: Usage = field(default_factory=Usage)
    succeed: bool = True

    @property
    def array(self) -> List["ndarray"]:
        from numpy import array

        return [array(embedding) for embedding in self.embeddings]
