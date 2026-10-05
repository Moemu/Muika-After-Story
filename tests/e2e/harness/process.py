"""收集独立 E2E 进程的响应、错误和退出轨迹。"""

import asyncio
import json


class MemoryProcess:
    def __init__(self, process, recorder, name):
        self.process, self.recorder, self.name = process, recorder, name
        self._errors = asyncio.create_task(process.stderr.read())

    async def command(self, **command):
        self.process.stdin.write((json.dumps(command) + "\n").encode())
        await self.process.stdin.drain()
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), 10)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()
            raise AssertionError((await self._errors).decode(errors="replace")) from None
        if not line:
            await self.process.wait()
            raise AssertionError((await self._errors).decode(errors="replace"))
        result = json.loads(line)
        self.recorder.record("sync_command", node=self.name, command=command, result=result)
        return result

    async def close(self):
        if self.process.returncode is not None:
            return
        self.process.stdin.write(b'{"action":"stop"}\n')
        await self.process.stdin.drain()
        try:
            await asyncio.wait_for(self.process.wait(), 10)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()
        errors = (await self._errors).decode(errors="replace")
        self.recorder.record("process_exit", node=self.name, stderr=errors)
        assert self.process.returncode == 0, errors
        assert "[Loop] Event " not in errors, errors
