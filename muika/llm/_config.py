from importlib.util import find_spec
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, field_validator


class ModelConfig(BaseModel):
    provider: str
    """所使用模型提供者的名称，位于 llm/providers 下"""
    default: bool = False
    """是否默认启用"""

    max_tokens: int = 4096
    """最大回复 Tokens """
    temperature: float = 0.75
    """模型的温度系数"""
    top_p: float = 0.95
    """模型的 top_p 系数"""
    top_k: float = 3
    """模型的 top_k 系数"""
    frequency_penalty: Optional[float] = None
    """模型的频率惩罚"""
    presence_penalty: Optional[float] = None
    """模型的存在惩罚"""
    repetition_penalty: Optional[float] = None
    """模型的重复惩罚"""
    stream: bool = False
    """是否使用流式输出"""
    online_search: bool = False
    """是否启用联网搜索（原生实现）"""
    content_security: bool = False
    """是否启用内容安全"""

    model_path: str = ""
    """本地模型路径"""
    adapter_path: str = ""
    """基于 model_path 的微调模型或适配器路径"""

    model_name: str = ""
    """所要使用模型的名称"""
    api_key: str = ""
    """在线服务的 API KEY"""
    api_secret: str = ""
    """在线服务的 api secret """
    api_host: str = ""
    """自定义 API 地址"""

    extra_body: Optional[dict] = None
    """OpenAI 的 extra_body"""
    enable_thinking: Optional[bool] = None
    """Dashscope 的 enable_thinking"""
    thinking_budget: Optional[int] = None
    """Dashscope 的 thinking_budget"""
    incremental_output: bool = stream
    """Dashscope 的 incremental_output，默认为 stream 的值"""

    multimodal: bool = False
    """是否为（或启用）多模态模型"""
    modalities: List[Literal["text", "audio", "image"]] = ["text"]
    """生成模态"""
    audio: Optional[Any] = None
    """多模态音频参数"""

    # ── 成本估算（每 1M tokens 的价格，None 表示不启用） ──
    input_price: Optional[float] = None
    """每百万输入 token 价格"""
    output_price: Optional[float] = None
    """每百万输出 token 价格"""
    cached_price: Optional[float] = None
    """每百万缓存命中 token 价格"""

    def __hash__(self) -> int:
        return hash(f"{self.provider}::{self.api_host}::{self.model_name}")

    def __eq__(self, config: object) -> bool:
        if not isinstance(config, ModelConfig):
            return False

        return (
            self.provider == config.provider
            and self.api_host == config.api_host
            and self.model_name == config.model_name
        )

    @field_validator("provider")
    @classmethod
    def check_model_loader(cls, provider: str) -> str:
        if not provider:
            raise ValueError("provider is required")

        provider = provider.lower()

        # Check if the specified loader exists
        module_path = f"muika.llm.providers.{provider}"

        # 使用 find_spec 仅检测模块是否存在，不实际导入
        if find_spec(module_path) is None:
            raise ValueError(f"指定的模型加载器 '{provider}' 不存在于 llm 目录中")

        return provider
