"""让 Muika 查看设备、选择行动位置并在回合边界请求交接。"""

from pydantic import BaseModel, Field

from muika.core.executor import Executor
from muika.plugin.func_call import on_function_call


class DeviceParams(BaseModel):
    id: str = Field(description="Paired device identity from list_devices.")


class HandoffParams(DeviceParams):
    reason: str = Field(description="Why this device suits the current situation or task.")


@on_function_call("List connected devices, their roles and available tools.", scope="core", read_only=True)
async def list_devices(executor: Executor) -> str:
    """列出已配对设备及实际可用能力。"""
    if executor.devices is None:
        return "This deployment has one local device. Multi-node mode is not enabled."
    return await executor.devices.list_devices()


@on_function_call(
    "Choose where new action tasks run. Existing tasks stay on their original device.",
    params=DeviceParams,
    scope="core",
    idempotent=True,
)
async def select_execution_device(id: str, executor: Executor) -> str:
    """选择后续任务的执行设备，保留已有任务的设备归属。"""
    if executor.devices is None:
        return "Only the current local device is available."
    return await executor.devices.select_device(id)


@on_function_call(
    "Request another Core to continue as you after this reply and the current action boundary. "
    "Choose a connected compatible Core when its location suits the situation; explain your reason.",
    params=HandoffParams,
    scope="core",
    idempotent=True,
)
async def request_core_handoff(id: str, reason: str, executor: Executor) -> str:
    """按情境请求交接，运行时在检查点边界决定是否执行。"""
    if executor.devices is None:
        return "There is no other Core in this local deployment."
    return await executor.devices.request_handoff(id, reason)
