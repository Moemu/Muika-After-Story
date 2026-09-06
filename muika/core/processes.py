"""管理工具启动的子进程、输出文件和退出清理。"""

from __future__ import annotations

import asyncio
import codecs
import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from muika.config import mas_config

if sys.platform == "win32":
    import win32api
    import win32con
    import win32job


class WindowsJob:
    """在关闭时终止本次执行的所有 Windows 子进程。"""

    def __init__(self, pid: int) -> None:
        """将指定进程加入关闭时自动终止成员的 Windows 作业。

        :param pid: 要加入作业的进程 ID。
        :raises RuntimeError: 当前平台不是 Windows。
        """
        if sys.platform == "win32":
            self.handle = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation, info)
            process = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid)
            try:
                win32job.AssignProcessToJobObject(self.handle, process)
            finally:
                process.Close()
        else:
            raise RuntimeError("Windows jobs are unavailable on this platform")

    def close(self) -> None:
        """关闭作业句柄并终止作业中的进程。"""
        if sys.platform == "win32":
            self.handle.Close()


class ProcessResult(BaseModel):
    """返回执行状态与按字节续读的输出。"""

    process_id: str
    status: Literal["running", "completed", "stopped", "timeout"]
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_offset: int = 0
    stderr_offset: int = 0
    stdout_more: bool = False
    stderr_more: bool = False
    output_directory: str


@dataclass
class RunningProcess:
    process: asyncio.subprocess.Process
    directory: Path
    owner: str | None
    job: WindowsJob | None = None
    monitor: asyncio.Task[None] | None = None
    status: Literal["running", "completed", "stopped", "timeout"] = "running"


class ProcessManager:
    """拥有工具启动的进程，等待操作不会终止仍在运行的工作。"""

    def __init__(self) -> None:
        """初始化当前运行期间的进程注册表。"""
        self._processes: dict[str, RunningProcess] = {}

    @staticmethod
    def _save_record(running: RunningProcess) -> None:
        """将进程归属、状态和退出码原子写入执行记录。"""
        temporary = running.directory / "record.tmp"
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(
                {
                    "owner": running.owner,
                    "pid": running.process.pid,
                    "status": running.status,
                    "exit_code": running.process.returncode,
                },
                file,
            )
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(running.directory / "record.json")

    def read_record(
        self,
        process_id: str,
        *,
        owner: str | None,
        stdout_offset: int = 0,
        stderr_offset: int = 0,
        max_bytes: int = 12000,
    ) -> dict[str, object]:
        """读取保存的执行状态和输出，不恢复旧进程的控制权。

        :param process_id: 执行记录的唯一标识。
        :param owner: 请求任务的标识；为 None 时不检查归属。
        :param stdout_offset: 标准输出的起始字节偏移。
        :param stderr_offset: 标准错误的起始字节偏移。
        :param max_bytes: 每个输出流的分页字节数。
        :return: 执行记录、可控状态、输出内容和续读位置。
        :raises ValueError: 标识、归属或分页参数无效。
        :raises OSError: 无法读取记录或输出文件。
        """
        if len(process_id) != 32 or any(char not in "0123456789abcdef" for char in process_id):
            raise ValueError("Invalid process ID")
        directory = mas_config.data_dir.resolve() / "agent_processes" / process_id
        record = json.loads((directory / "record.json").read_text(encoding="utf-8"))
        if owner is not None and record["owner"] != owner:
            raise ValueError("This execution record belongs to another task")
        record["process_id"] = process_id
        record["controllable"] = process_id in self._processes
        if not record["controllable"] and record["status"] == "running":
            record["status"] = "unknown_after_restart"
        for stream, offset in (("stdout", stdout_offset), ("stderr", stderr_offset)):
            body, following, more = self._read(directory / f"{stream}.log", offset, max_bytes)
            record[stream] = body
            record[f"{stream}_offset"] = following
            record[f"{stream}_more"] = more
        return record

    async def start(
        self, command: list[str], *, env: dict[str, str], cwd: str, owner: str | None, timeout: float
    ) -> str:
        """启动子进程，将输出保存到文件并监控执行期限。

        :param command: 可执行文件及其参数，不经过额外的 shell 解析。
        :param env: 子进程的环境变量。
        :param cwd: 子进程的工作目录。
        :param owner: 所属任务的标识，无所属任务时为 None。
        :param timeout: 执行期限，单位为秒。
        :return: 本次执行的唯一标识。
        :raises OSError: 创建输出文件或启动进程失败。
        """
        process_id = uuid4().hex
        directory = mas_config.data_dir.resolve() / "agent_processes" / process_id
        directory.mkdir(parents=True)
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        with (directory / "stdout.log").open("wb") as stdout, (directory / "stderr.log").open("wb") as stderr:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=stdout,
                stderr=stderr,
                env=env,
                cwd=cwd,
                start_new_session=sys.platform != "win32",
                creationflags=creationflags,
            )
        running = RunningProcess(process, directory, owner)
        self._processes[process_id] = running
        try:
            if sys.platform == "win32" and process.returncode is None:
                running.job = WindowsJob(process.pid)
            self._save_record(running)
        except Exception:
            await self._terminate_tree(running)
            del self._processes[process_id]
            raise
        running.monitor = asyncio.create_task(self._monitor(running, timeout))
        return process_id

    async def _monitor(self, running: RunningProcess, timeout: float) -> None:
        """等待进程退出或超时，清理进程树并保存最终状态。"""
        try:
            await asyncio.wait_for(running.process.wait(), timeout)
            if running.status == "running":
                running.status = "completed"
        except asyncio.TimeoutError:
            running.status = "timeout"
        finally:
            await self._terminate_tree(running)
            self._save_record(running)

    async def _terminate_tree(self, running: RunningProcess) -> None:
        """终止受管进程树并等待主进程退出。"""
        if running.job is not None:
            running.job.close()
            running.job = None
        elif sys.platform != "win32":
            try:
                os.killpg(running.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif running.process.returncode is None:
            running.process.kill()
        await running.process.wait()

    def _get(self, process_id: str, owner: str | None) -> RunningProcess:
        """获取当前运行期间的进程并检查任务归属。

        :param owner: 请求任务的标识；为 None 时不检查归属。
        :raises ValueError: 进程不存在或属于其他任务。
        """
        running = self._processes.get(process_id)
        if running is None:
            raise ValueError("Unknown process ID in this runtime. Inspect saved outputs; do not relaunch blindly.")
        if owner is not None and owner != running.owner:
            raise ValueError("This process belongs to another task")
        return running

    async def wait(
        self,
        process_id: str,
        *,
        owner: str | None,
        seconds: float = 1.0,
        stdout_offset: int = 0,
        stderr_offset: int = 0,
        max_bytes: int = 12000,
    ) -> ProcessResult:
        """有限等待进程并读取一页输出，等待结束不会终止进程。

        :param process_id: 当前运行期间的执行标识。
        :param owner: 请求任务的标识；为 None 时不检查归属。
        :param seconds: 等待秒数，最多 30 秒；非正数表示立即读取。
        :param stdout_offset: 标准输出的起始字节偏移。
        :param stderr_offset: 标准错误的起始字节偏移。
        :param max_bytes: 每个输出流的分页字节数。
        :return: 当前状态、退出码、输出内容和续读位置。
        :raises ValueError: 进程不存在、归属不符或分页参数无效。
        :raises OSError: 无法读取输出文件。
        """
        running = self._get(process_id, owner)
        if running.monitor and not running.monitor.done() and seconds > 0:
            await asyncio.wait({running.monitor}, timeout=min(seconds, 30.0))
        stdout, stdout_next, stdout_more = self._read(running.directory / "stdout.log", stdout_offset, max_bytes)
        stderr, stderr_next, stderr_more = self._read(running.directory / "stderr.log", stderr_offset, max_bytes)
        return ProcessResult(
            process_id=process_id,
            status=running.status,
            exit_code=running.process.returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_offset=stdout_next,
            stderr_offset=stderr_next,
            stdout_more=stdout_more,
            stderr_more=stderr_more,
            output_directory=str(running.directory),
        )

    @staticmethod
    def _read(path: Path, offset: int, limit: int) -> tuple[str, int, bool]:
        """按字节分页读取 UTF-8 输出，保留跨页字符的完整性。

        :param path: 输出文件路径。
        :param offset: 起始字节偏移。
        :param limit: 分页字节数，上限为 100000；必要时补齐首个字符。
        :return: 文本、下一字节偏移和是否仍有未读内容。
        :raises ValueError: 偏移为负数或分页字节数小于 1。
        :raises OSError: 无法读取输出文件。
        """
        if offset < 0 or limit < 1:
            raise ValueError("Output offset must be nonnegative and max_bytes must be positive")
        with path.open("rb") as file:
            file.seek(offset)
            data = file.read(min(limit, 100000))
            following = file.read(1)
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            text = decoder.decode(data, final=not following)
            while not text and following:
                data += following
                text += decoder.decode(following)
                following = file.read(1)
            pending, _ = decoder.getstate()
        return text, offset + len(data) - len(pending), bool(following or pending)

    async def stop(self, process_id: str, *, owner: str | None) -> ProcessResult:
        """停止指定进程树，等待清理完成并返回状态和首屏输出。

        :param owner: 请求任务的标识；为 None 时不检查归属。
        :raises ValueError: 进程不存在或属于其他任务。
        """
        running = self._get(process_id, owner)
        if running.status == "running":
            running.status = "stopped"
            await self._terminate_tree(running)
        if running.monitor:
            await running.monitor
        return await self.wait(process_id, owner=owner, seconds=0)

    async def stop_owner(self, owner: str) -> None:
        """停止指定任务所属的所有运行中进程。"""
        for process_id, running in list(self._processes.items()):
            if running.owner == owner and running.status == "running":
                await self.stop(process_id, owner=owner)

    def active_for(self, owner: str) -> list[str]:
        """返回指定任务仍在运行或尚未完成退出清理的执行标识。"""
        return [
            key
            for key, item in self._processes.items()
            if item.owner == owner
            and (item.status == "running" or item.monitor is not None and not item.monitor.done())
        ]

    async def close(self) -> None:
        """停止所有受管进程，等待清理完成并清空注册表。"""
        for process_id in list(self._processes):
            await self.stop(process_id, owner=None)
        self._processes.clear()


_process_manager = ProcessManager()


def get_process_manager() -> ProcessManager:
    """返回当前核心进程共享的子进程管理器。"""
    return _process_manager
