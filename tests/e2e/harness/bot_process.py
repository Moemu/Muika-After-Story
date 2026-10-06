"""独立 Bot IPC 进程，不登录外部聊天平台。"""

import asyncio
import contextlib
import json
import sys
from pathlib import Path

with contextlib.redirect_stdout(sys.stderr):
    import nonebot

    nonebot.init(log_level="ERROR")
    from muika.config import mas_config
    from muika_bot import ipc_client
    from muika_bot.ipc_client import IpcClient

    ipc_client._INITIAL_RECONNECT_DELAY = 0.05
    ipc_client._MAX_RECONNECT_DELAY = 0.1


async def main():
    primary, fallback, directory = sys.argv[1:]
    mas_config.data_dir = Path(directory)
    client = IpcClient(primary, "test-ipc-secret", "endpoint-bot", fallback_urls=[fallback] if fallback else [])
    outputs = []

    async def receive(message):
        outputs.append(message)

    client.on_message("send_message", receive)
    runner = asyncio.create_task(client.connect())
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = json.loads(line)
            if command["action"] == "stop":
                break
            if command["action"] == "send":
                await client.send_user_message(command["text"])
            print(
                json.dumps({"connected": client.is_connected, "endpoint": client.endpoint, "outputs": outputs}),
                flush=True,
            )
    finally:
        await client.disconnect()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
