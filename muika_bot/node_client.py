"""将 NoneBot 的现有 IPC 接口接到框架无关的持久客户端。"""

import asyncio
from uuid import uuid4

from muika.config import mas_config
from muika.ipc.bot_client import DeliveryNotStarted, DurableBotClient
from muika.models import Resource
from muika.node.config import NodeProfile
from muika.node.models import IncomingMessage, OutgoingMessage

from .ipc_client import IpcClient


class NodeBotIpcClient(IpcClient):
    """保持现有处理器接口，配对身份不随适配器重连而改变。"""

    def __init__(self, profile: NodeProfile) -> None:
        if profile.role != "bot":
            raise ValueError("NoneBot requires a paired Bot profile.")
        super().__init__(profile.address, client_name=profile.id)
        self.durable = DurableBotClient(
            profile.address,
            profile.token,
            profile.id,
            profile.directory,
            self.deliver,
            input_timeout=mas_config.input_timeout,
            on_status=self.status,
            ca_file=profile.ca_file,
        )
        self._runner: asyncio.Task | None = None
        self.offline_notice_sent = False

    async def deliver(self, message: OutgoingMessage, resources: list[Resource]) -> None:
        handler = self._handlers.get(message.kind)
        if handler is None:
            raise DeliveryNotStarted("No platform handler is ready.")
        await handler(
            {
                "type": message.kind,
                "id": message.id,
                "content": message.text,
                "conversation_id": message.conversation_id,
                "resources": [resource.to_dict() for resource in resources],
            }
        )

    async def status(self, status: str) -> None:
        if status == "core_available":
            self.offline_notice_sent = False

    @property
    def is_connected(self) -> bool:
        return self.durable.connected.is_set()

    def set_client_info(self, name: str) -> None:
        """配对身份保持不变；平台名称仅由适配器自身使用。"""

    async def connect(self) -> None:
        self._runner = asyncio.current_task()
        await self.durable.run()

    async def wait_connected(self, timeout: float = 10) -> bool:
        try:
            await asyncio.wait_for(self.durable.connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def disconnect(self) -> None:
        self.durable.stopping.set()
        if self._runner is not None:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        await self.durable.close()

    async def send_user_message(
        self,
        message: str,
        resources: list[dict] | None = None,
        *,
        message_id: str | None = None,
        conversation_id: str = "master",
    ) -> bool:
        await self.durable.queue_input(
            IncomingMessage(
                id=message_id or uuid4().hex, client_id=self.client_name, conversation_id=conversation_id, text=message
            ),
            [Resource(**resource) for resource in resources or []],
        )
        return True

    async def send_command(self, raw: str, *, message_id: str | None = None, conversation_id: str = "master") -> bool:
        await self.durable.queue_input(
            IncomingMessage(
                id=message_id or uuid4().hex,
                client_id=self.client_name,
                conversation_id=conversation_id,
                kind="command",
                text=raw,
            )
        )
        return True

    async def send_session_bootstrap(self) -> bool:
        await self.durable.queue_input(
            IncomingMessage(
                id=uuid4().hex, client_id=self.client_name, conversation_id="master", kind="session_bootstrap"
            )
        )
        return True

    async def send_session_end(self) -> bool:
        await self.durable.queue_input(
            IncomingMessage(id=uuid4().hex, client_id=self.client_name, conversation_id="master", kind="session_end")
        )
        return True
