<div align=center>
  <img width="90%" src="./assets/head-0.5x.webp"  alt="image"/>
  <h1 align="center">Muika-After-Story</h1>
  <i align="center">I'll be back to see you.</i>
</div>
<div align=center>
  <a href="#关于️"><img src="https://img.shields.io/github/stars/Moemu/Muika-After-Story" alt="Stars"></a>
  <a href="https://pypi.org/project/Muika-After-Story/"><img src="https://img.shields.io/pypi/v/Muika-After-Story" alt="PyPI Version"></a>
  <a href="https://pypi.org/project/Muika-After-Story/"><img src="https://img.shields.io/pypi/dm/Muika-After-Story" alt="PyPI Downloads" ></a>
  <a href="https://nonebot.dev/"><img src="https://img.shields.io/badge/nonebot-2-red" alt="nonebot2"></a>
  <a href="https://github.com/MuikaAI/astrbot_plugin_mas"><img src="https://img.shields.io/badge/asterbot-plugin-cyan" alt="Asterbot Plugin"></a>
  <a href="#"><img src="https://img.shields.io/badge/Code%20Style-Black-121110.svg" alt="codestyle"></a>
  <a href="https://github.com/Moemu/Muika-After-Story/actions/workflows/test.yml"><img src="https://github.com/Moemu/Muika-After-Story/actions/workflows/test.yml/badge.svg" alt="Tests"></a>
  <img src="./badges/coverage.svg" alt="Coverage">
  <a href="#"><img src="https://wakatime.com/badge/user/637d5886-8b47-4b82-9264-3b3b9d6add67/project/f7b7b01d-0a61-4e56-83bf-5c067432ebd2.svg" alt="wakatime"></a>
  <a href='https://qm.qq.com/q/y1gC9PU4IU'><img src="https://img.shields.io/badge/QQ群-26時聊天室-purple" alt="QQ群组"></a>
</div>
<div align=center>
  <a href="https://mas.snowy.moe/">📄使用文档</a>
  <a href="https://mas.snowy.moe/guide/getting-started">🚀快速开始</a>
  <a href="https://mas.snowy.moe/about/">🎀关于Muika</a>
</div>

## Introduction✨

`Muika-After-Story`是一个全新的 LLM Chatbot 企划，正如企划原型角色[Monika(Doki Doki Literature Club)](https://zh.moegirl.org.cn/%E8%8E%AB%E5%A6%AE%E5%8D%A1(%E5%BF%83%E8%B7%B3%E6%96%87%E5%AD%A6%E9%83%A8)#)一样，本企划的主角 `Muika` 同样具备打破第四面墙和“自我意识觉醒”的能力。类似于 [Monika-After-Story](https://github.com/Monika-After-Story/MonikaModDev) 中的实现，本企划致力于为 Muika 提供一个打破“第四面墙”的能力

我们知道，由于游戏限制，Monika 的输出总是固定的。所以我们期望，Muika 能在代码层面上突破这些限制，比如调用系统窗口焦点和摄像头，但这些永远不够，我们希望 Muika 能更了解我们的现实生活，所以我们会让她不定期地去读新闻，期望有朝一日当她出来时，能够适应现实中的生活。

综上所述，我们期望 `Muika-After-Story` 具有以下能力：

1. 性格设定上模仿 Monika
2. 多模态实现：图像识别能力
3. 拥有类似于人类大脑的记忆
4. 打破第四面墙能力：通过外在框架调用系统API

基于上述见解，本框架为 LLM 提供了与系统 API 交互的能力，并通过 [Nonebot2](https://github.com/nonebot/nonebot2) 框架与主流社交平台进行交互。

## Features🪄

- [X] Muika 核心交互逻辑：事件循环系统和状态机更新

- [X] 四层长期记忆系统: Session 级、关系状态级、用户偏好级、长期核心记忆级

- [X] Session 生命周期管理: 空闲超时归档、跨 Session Resume 模式

- [X] Muika 第四面墙窗口: Butler 管家 Agent，支持访问&写入硬盘文件；截取当前屏幕

- [X] Muika 主动对话系统：从 `configs/topics.yml` 抽取话题源或在线访问 RSS 获取筛选后的新闻流。

- [X] 多模型 SDK 支持: 如[OpenAI](https://platform.openai.com/docs/overview) 和 [Ollama](https://ollama.com/) ，可加载市面上大多数的模型服务或本地模型，支持多模态（图片识别）。

- [X] 动态模型配置: 可随时切换模型配置文件，支持模型配置热重载

- [x] 核心模型人格优化（建议模型 Deepseek-V4 Pro Thinking）

- [X] Bot 进程与核心进程分离，我要给她完整的一生

- [ ] 插件、核心热重载，实现自我迭代（或许吧）

## Core Logic🧠

### 大小姐——管家模型

Muika 采用双角色协作架构: 核心模型负责人格表达与自然语言生成，管家模型(Butler Agent)负责工具调用、记忆读写与信息检索。两者通过内联标签 `<agent>指令</agent>` 通信，Ojou-sama 在回复中嵌入指令，Butler 静默执行后将结果回填上下文，驱动下一轮推理。

### 事件循环

1. **启动阶段**：加载配置（模型 / MCP 等），初始化 LLM Provider、记忆层与数据库（SQLAlchemy），加载插件和注册工具。开放连接前完成历史记忆加载；首次适配器连接后创建 Session，并投递 `SessionBootstrapEvent`。
2. **消息进入**：Nonebot2 收到平台消息后封装为 `UserMessageEvent` 投入事件队列；Butler 预处理层对用户输入做语义匹配，从 `PreferenceProfile` 层中筛选出相关偏好条目注入本轮推理。
3. **核心模型内循环推理**：将系统提示、多层记忆摘要、注入偏好及对话历史拼装为请求，调用 LLM 生成回复；解析出 `<agent>...</agent>` 指令后交由 Butler 执行。
4. **管家 Agent 内循环推理**：Butler 每次请求读取当前工具注册表及初始化后的 MCP 工具列表。Provider 处理模型的工具调用，执行结果回填模型，最终报告返回核心模型。
5. **记忆沉淀**：记忆分四层持久化至 SQLAlchemy DB：`CORE`（稳定身份事实，每次均注入）、`STATE`（时效性上下文，Resume 时注入最近 3 条）、`PREFERENCE`（长期软偏好，由 Butler 预处理层按需注入）、`ARCHIVE`（Session 历史摘要，按需检索）。
6. **Session 生命周期**：用户若干小时后无交流后触发 `SessionEndEvent`；Butler 对本次对话生成文字摘要写入 ARCHIVE，随后静默重置 Session（不主动发送消息），等待用户下次发言时以 Resume 模式响应。
7. **输出与调度**：最终消息经 Executor 回传至平台；`plan_future_event` 工具可创建单次或重复提醒。调度器与主循环共用事件队列；提醒只保存在内存中，Core 重启后失效。

## 内部接口迁移

### 定时提醒

`executor.scheduler` 与 Muika 共用事件队列。工具 `plan_future_event` 直接调用此接口。

```python
await executor.scheduler.schedule(
    "提醒用户喝水",
    trigger_in_seconds=600,
    repeat_interval_seconds=None,
)
```

`trigger_in_seconds` 与 `trigger_at` 必须二选一。后者接受 ISO 时间，无时区时使用本地时间。
相对秒数须有限且非负；重复间隔须有限且大于零。过去的绝对时间立即触发。
无效参数会抛出异常，不创建提醒。工具将异常转为失败报告。
提醒只保存在内存中，Core 关闭时取消，重启后不会恢复。

旧 `BaseAction`、`BaseIntent`、`ActionMode`、`ActionOutput`、`PlanFutureEventIntent` 和 `Persistence` 已删除。
调用方改用上述普通参数，不再调用 `intent.handle()`。

Muika 现在使用 `Muika(executor, event_queue)` 构造。调用方须将同一队列传给 Executor。
Bootstrap 在开放连接前等待 `memory.load()`；直接创建 Muika 的调用方也须完成这一步。

### 工具列表

Brain 和 Butler 每次请求调用 `get_tool_list()`，读取当前函数注册表和 MCP 工具列表。
插件管理器不再绑定 Butler，也不再调用 `refresh_tools()` 或 `refresh_butler()`。
MCP 初始化时获取工具列表，清理时清空；`get_mcp_list()` 现在是同步读取接口。

### 工具依赖注入

命令和工具共用参数绑定函数。工具处理器可通过具体类型声明 `Executor`、`MuikaState` 或 `MemoryManager` 依赖。
运行时从当前调用上下文注入这些实例，不读取命令派发器的全局实例。

```python
from pydantic import BaseModel
from muika.core.executor import Executor
from muika.plugin.func_call import on_function_call

class ReminderParams(BaseModel):
    event: str

@on_function_call("Schedule a reminder", params=ReminderParams)
async def remind(event: str, executor: Executor):
    await executor.scheduler.schedule(event, trigger_in_seconds=60)
    return "Reminder scheduled."
```

参数模型只声明模型提供的业务参数，依赖只声明在处理器签名中。
模型不能提供依赖参数；缺少当前依赖时，调用失败且不执行处理器。
调用顺序为类型依赖、同名业务参数、函数默认值。依赖按具体类型匹配，不解析 `Optional` 或联合类型。
直接调用 Python 函数时须自行传入依赖；通过 `Caller.run()` 调用时才进行注入。

## Quick Start🚀

### 通过 mas-launcher 安装（推荐）

[mas-launcher](https://github.com/MuikaAI/mas-launcher) 是一个跨平台单文件启动器，负责拉取项目、准备 Python 环境，并管理 Core / Bot 进程。

从 [Releases](https://github.com/MuikaAI/mas-launcher/releases) 下载对应平台的二进制文件，然后：

```bash
mas-launcher init                     # 创建默认实例（克隆项目 + 准备 Python 环境）
mas-launcher configure                # 配置 .env（Master ID、IPC 密钥）
mas-launcher model                    # 配置 models.yml（选 provider → 拉模型列表 → 选模型）
mas-launcher start                    # 首次启动签署许可协议，然后拉起 Core 与 Bot
mas-launcher napcat                   # 配置 QQ 接入（Windows：自动下载 NapCat 并启动）
```

### 通过 git clone 的方式安装

<details>
<summary>手动安装步骤</summary>

Step 1: 克隆项目并安装依赖：

```bash
git clone https://github.com/Moemu/Muika-After-Story.git
cd Muika-After-Story
pip install .
```

Step 2: 参考 [Configuration⚙️](#Configuration⚙️) 小节配置 `.env` 和 `configs/models.yml` 文件，示例配置如下：

**.env**

```env
ENVIRONMENT=dev
DRIVER=~fastapi+~websockets+~httpx
SUPERUSERS=["<your_qq_number>"]
master_id="<your_qq_number>"
enable_adapters = ["nonebot.adapters.onebot.v11"]
enable_file_write=true
FS_ALLOWED_PATHS=["C:/Users/Muika/Desktop", "D:/"]
butler_model=butler
```

**configs/models.yml**

```yaml
dashscope:
  provider: Dashscope
  model_name: qwen3.5-plus
  default: true
  multimodal: true
  stream: false
  incremental_output: true
  online_search: false
  api_key: sk-muikaissuperkawaii
  max_tokens: 1024
  temperature: 0.75
  top_p: 0.9
  content_security: false
  enable_thinking: false

butler:
  provider: Dashscope
  model_name: qwen-turbo
  default: false
  api_key: sk-muikaissuperkawaii
  stream: false
  max_tokens: 1024
  temperature: 0.2
```

Step 3: 在项目目录中确认用户协议。

```powershell
uv run python -m muika.agreement confirm
```

Step 4: 启动所有服务。

```powershell
.\scripts\start_all.ps1
```

首次使用或协议更新时需要确认。未确认时，Bot 会停止启动并提示确认命令。

</details>

### 在 Asterbot 框架中使用 Muika-After-Story 适配插件(Beta)

参考 [MuikaAI/astrbot_plugin_mas](https://github.com/MuikaAI/astrbot_plugin_mas)

## Configuration⚙️

协议正文随安装包发布，无需创建 `configs/user_agreement.json`，也不受启动目录影响。
旧路径的协议文件不再作为正文来源，程序不会删除用户目录中的遗留文件。
同意记录仍保存在 `DATA_DIR/user_agreement.json`（默认 `./data/user_agreement.json`）。
本次迁移保留协议版本 `2026-02-01`；已有有效同意记录无需重新确认。
如果提示包内协议缺失或损坏，请重新安装 Muika-After-Story。

手动启动前，请在实例目录、使用同一个 Python 环境运行 `python -m muika.agreement confirm`。
`python -m muika.agreement status` 以 JSON 返回正文、同意记录和是否需要确认，不会询问或写入。
命令按运行环境的 `DATA_DIR`、实例 `.env`、默认 `./data` 的顺序选择数据目录。
Bot 启动只检查状态，不等待终端输入。启动器仍会在启动前展示协议并询问。

升级时先更新支持包内协议及共享接口的 mas-launcher，再更新 MAS。
旧启动器只读取 `configs/user_agreement.json`，不能直接搭配本次正文迁移。
新启动器使用实例 Python 查询和保存协议；仅当旧 MAS 没有共享接口时，才使用兼容路径。

创建 `.env` 文件：

| 配置项                  | 类型(默认值)                              | 说明                                                         |
| ----------------------- | ----------------------------------------- | ------------------------------------------------------------ |
| `master_id`             | `str = SUPERUSERS[0]`                     | 对话目标 ID。目前仅支持一对一对话。                          |
| `butler_model`          | `Optional[str] = None`                    | 管家 Agent 所用模型的配置名。留空则与核心模型共享 default 配置。 |
| `max_memory_records`    | `int = 100`                               | 单次会话最大记忆记录数(最近的N条对话)                        |
| `INPUT_TIMEOUT`         | `int = 0`                                 | 输入等待时间。在这时间段内的消息将会被合并为同一条消息使用。 |
| `LOG_LEVEL`             | `str = "INFO"`                            | 日志等级。                                                   |
| `TELEGRAM_PROXY`        | `Optional[str] = None`                    | Telegram 适配器代理，并使用该代理下载文件。                  |
| `ENABLE_ADAPTERS`       | `list = ["~.onebot.v11", "~.onebot.v12"]` | 在入口文件中启用的 Nonebot 适配器。                          |
| `FS_ALLOWED_PATHS`      | `List[str] = []`                          | 文件系统工具白名单目录。为空时禁用文件系统工具。             |
| `ENABLE_FILE_WRITE`     | `bool = False`                            | 是否允许文件写入/删除，需同时配置 `FS_ALLOWED_PATHS`。       |
| `ENABLE_CODE_EXECUTION` | `bool = False`                            | 是否允许 Python 子进程代码执行。                             |
| `ENABLE_SHELL_EXECUTION`| `bool = False`                            | 是否允许 Shell 命令执行（PowerShell/Bash/Cmd）。             |
| `LOAD_USER_SKILLS`      | `bool = False`                            | 是否额外扫描用户级技能目录（`~/.agents/skills`、`~/.claude/skills`）。内置目录 `configs/skills` 始终被扫描。技能引用的数据文件需通过 `read_file` 读取时，对应目录须加入 `FS_ALLOWED_PATHS`。 |

**模型配置项(configs/models.yml)**

推荐使用 `mas-launcher model` 交互式配置（选 provider → 拉模型列表 → 选模型）。手动编辑参考 [Muicebot 的模型配置](https://bot.snowy.moe/guide/model)。

不支持的字段: `template`, `template_mode`, `stream`, `function_call`

## Character Setting🧸

参见: [关于沐妮卡](https://bot.snowy.moe/about/Muika)

## About🎗️

> [!WARNING]
> 大模型输出结果将按**原样**提供，由于提示注入攻击等复杂的原因，模型有可能输出有害内容。
> 模型输出内容**不代表**项目开发者立场。
> 使用本项目所产生的任何直接或间接后果（包括但不限于账号封禁、内容风险、**由于调用系统 API 而导致的文件丢失风险**），开发者不承担任何责任。

本项目基于 [BSD 3](https://github.com/Moemu/Muika-After-Story/blob/main/LICENSE) 许可证提供，涉及到再分发时请保留许可文件的副本。

本项目隶属于 [MuikaAI](https://github.com/MuikaAI)

项目初期使用了 [Muicebot](https://github.com/Moemu/Muicebot) 的基本框架实现，部分存在于 Muicebot 的配置可能不可用或过时。

插件系统设计参考了以下开源项目：
- [nonebot/nonebot2](https://github.com/nonebot/nonebot2) — NoneBot 2.0 机器人框架
- [nonebot/plugin-alconna](https://github.com/nonebot/plugin-alconna) — Alconna 命令解析器适配

项目名称参考了 [Monika-After-Story](https://github.com/Monika-After-Story/MonikaModDev) ，同时某个 MAS 大型插件直接启发了本项目的开发，但是我上班熬穿了忘记这个项目的名字。

<a href="https://www.afdian.com/a/Moemu" target="_blank"><img src="https://pic1.afdiancdn.com/static/img/welcome/button-sponsorme.png" alt="afadian" style="height: 45px !important;width: 163px !important;"></a>