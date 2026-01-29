from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import total_ordering
from io import BytesIO
from mimetypes import guess_extension
from typing import List, Literal, Optional, Union


@dataclass
class Resource:
    """多模态消息"""

    type: Literal["image", "video", "audio", "file"]
    """消息类型"""
    path: str = field(default_factory=str)
    """本地存储地址(对于模型处理是必需的)"""
    url: Optional[str] = field(default=None)
    """远程存储地址(一般不传入模型处理中)"""
    raw: Optional[Union[bytes, BytesIO]] = field(default=None)
    """二进制数据（只使用于模型返回且不保存到数据库中）"""
    mimetype: Optional[str] = field(default=None)
    """文件元数据类型(eg. `image/jpeg`)"""
    extension: Optional[str] = field(default=None)
    """文件扩展名(eg. `.jpg`)"""

    def __post_init__(self):
        self.ensure_mimetype()

    def __hash__(self) -> int:
        return hash(self.get_file())

    def get_file(self) -> Union[str, bytes, BytesIO]:
        """
        从所有可能的值中获得一个文件对象

        :raise FileNotFoundError: 此实例没有引用任何一个文件
        """
        result = self.path or self.url or self.raw
        if result is not None:
            return result
        raise FileNotFoundError("该实例没有一个具体的文件对象！")

    def ensure_mimetype(self):
        """
        保证 mimetype 是确定的
        """
        from .utils.utils import guess_mimetype

        self.mimetype = guess_mimetype(self)
        if self.mimetype:
            self.extension = guess_extension(self.mimetype)

    def to_dict(self) -> dict:
        """
        落库时存储的数据
        (注意：与模型进行交互的多模态文件必须在本地拥有备份)
        """
        return {"type": self.type, "path": self.path, "mimetype": self.mimetype}


@total_ordering
@dataclass
class Message:
    """格式化后的 bot 消息"""

    id: int | None = None
    """每条消息的唯一ID"""
    time: str = field(default_factory=lambda: datetime.strftime(datetime.now(), "%Y.%m.%d %H:%M:%S"))
    """
    字符串形式的时间数据：%Y.%m.%d %H:%M:%S
    若要获取格式化的 datetime 对象，请使用 format_time
    """
    userid: str = ""
    """Nonebot 的用户id"""
    groupid: str = "-1"
    """群组id，私聊设为-1"""
    message: str = ""
    """消息主体"""
    respond: str = ""
    """模型回复（不包含思维过程）"""
    history: int = 1
    """消息是否可用于对话历史中，以整数形式映射布尔值"""
    resources: List[Resource] = field(default_factory=list)
    """多模态消息内容"""
    usage: int = -1
    """使用的总 tokens, 若模型加载器不支持则设为-1"""
    profile: str = "_default"
    """消息所属存档"""

    @property
    def format_time(self) -> datetime:
        """将时间字符串转换为 datetime 对象"""
        return datetime.strptime(self.time, "%Y.%m.%d %H:%M:%S")

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "Message":
        return Message(**data)

    def __hash__(self) -> int:
        return hash(self.id)

    def __lt__(self, other: "Message") -> bool:
        return self.format_time < other.format_time
