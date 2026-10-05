"""让 Muika 查看活动设备，并请求切换自己的活动位置。"""

from pydantic import BaseModel, Field

from muika.core.state import MuikaState
from muika.plugin.func_call import on_function_call


@on_function_call("List connected devices and your current activity location.", read_only=True)
def list_devices(state: MuikaState) -> str:
    """返回真实连接状态；失联时其他设备的状态未知。"""
    nodes = state.nodes
    if nodes is None:
        return "Single-device deployment; all tools run locally."
    if not nodes.connected:
        return f"Local device: {nodes.name}; active: {nodes.active}. Other devices are unreachable."
    return f"Active device: {nodes.name}. Connected devices: {', '.join(nodes.nodes)}."


class HandoffParams(BaseModel):
    target: str = Field(description="Connected device name. Files and processes remain on their original device.")


@on_function_call(
    "Request another connected device to take over your activity. Wait for the device change event.",
    params=HandoffParams,
)
async def request_handoff(target: str, state: MuikaState) -> str:
    """请求交接；返回请求结果，不把请求视为已经完成。"""
    if state.nodes is None:
        raise ValueError("Only one device is configured")
    await state.nodes.handoff(target)
    return f"Core handoff requested to {target}. The device change event will report the outcome."
