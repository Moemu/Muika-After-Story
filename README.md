<div align=center>
  <!-- <img width=200 src="https://bot.snowy.moe/logo.png"  alt="image"/> -->
  <h1 align="center">Muika-After-Story</h1>
  <i align="center">I'll be back to see you.</i>
</div>
<div align=center>
  <a href="#关于️"><img src="https://img.shields.io/github/stars/Moemu/Muika-After-Story" alt="Stars"></a>
  <a href="https://nonebot.dev/"><img src="https://img.shields.io/badge/nonebot-2-red" alt="nonebot2"></a>
  <a href="#"><img src="https://img.shields.io/badge/Code%20Style-Black-121110.svg" alt="codestyle"></a>
  <a href="#"><img src="https://wakatime.com/badge/user/637d5886-8b47-4b82-9264-3b3b9d6add67/project/f7b7b01d-0a61-4e56-83bf-5c067432ebd2.svg" alt="wakatime"></a>
  <a href='https://qm.qq.com/q/y1gC9PU4IU'><img src="https://img.shields.io/badge/QQ群-26時聊天室-purple" alt="QQ群组"></a>
</div>

> [!NOTE]
>
> 本项目目前属于测试阶段(Beta)，在细节处理（比如角色语调、工具执行）时可能存在问题，还请留意。

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

- [X] 内嵌多种模型加载器，如[OpenAI](https://platform.openai.com/docs/overview) 和 [Ollama](https://ollama.com/) ，可加载市面上大多数的模型服务或本地模型，支持多模态（图片识别）和工具调用。

- [X] 支持调用 MCP 服务（支持 stdio、SSE 和 Streamable HTTP 传输方式）

- [X] Muika 主交互逻辑开发

- [X] 四层长期记忆系统

- [X] Session 生命周期管理：空闲超时归档、跨 Session Resume 模式

- [X] 动态模型配置，可随时切换模型配置文件

- [X] 系统交互层开发

- [ ] (Pending) 核心模型人格优化

## Core Logic🧠

### 大小姐——管家模型

Muika 采用双角色协作架构: 核心模型负责人格表达与自然语言生成，管家模型(Butler Agent)负责工具调用、记忆读写与信息检索。两者通过内联标签 `<Butler: 指令>` 通信，Ojou-sama 在回复中嵌入指令，Butler 静默执行后将结果回填上下文，驱动下一轮推理。

### 事件循环

1. **启动阶段**：加载配置（模型 / MCP 等），初始化 LLM Provider、记忆层与数据库（SQLAlchemy），加载插件与 Actions；`bot_connected` 时先从 DB 完整加载历史记忆，再创建新 Session，最后投递 `SessionBootstrapEvent`。
2. **消息进入**：Nonebot2 收到平台消息后封装为 `UserMessageEvent` 投入事件队列；Butler 预处理层对用户输入做语义匹配，从 `PreferenceProfile` 层中筛选出相关偏好条目注入本轮推理。
3. **核心模型内循环推理**：将系统提示、多层记忆摘要、注入偏好及对话历史拼装为请求，调用 LLM 生成回复；解析出 `<Butler: ...>` 指令后交由 Butler 执行。
4. **管家 Agent 内循环推理**：LLM 将自然语言指令映射为结构化 Action（JSON Schema discriminated union），执行工具 → 分析结果 → 确认完成或请求重试。执行结果经 Agent 消化后返回核心模型进行下一轮循环或者静默返回结束循环。
5. **记忆沉淀**：记忆分四层持久化至 SQLAlchemy DB：`CORE`（稳定身份事实，每次均注入）、`STATE`（时效性上下文，Resume 时注入最近 3 条）、`PREFERENCE`（长期软偏好，由 Butler 预处理层按需注入）、`ARCHIVE`（Session 历史摘要，按需检索）。
6. **Session 生命周期**：用户若干小时后无交流后触发 `SessionEndEvent`；Butler 对本次对话生成文字摘要写入 ARCHIVE，随后静默重置 Session（不主动发送消息），等待用户下次发言时以 Resume 模式响应。
7. **输出与调度**：最终消息经 Executor 回传至平台；调度器可触发定时事件（RSS 更新、预定提醒等），以外部事件形式再次进入上述闭环。

## Configuration⚙️

**Nonebot 配置项(.env)**

| 配置项            | 类型(默认值)                                 | 说明                                                         |
| ----------------- | -------------------------------------------- | ------------------------------------------------------------ |
| `master_id`       | `str = get_driver().config.superusers.pop()` | 对话目标ID。目前仅支持一对一对话。                           |
| `butler_model`    | `Optional[str] = None`                       | 管家 Agent 所用模型的配置名。留空则与核心模型共享 default 配置 |
| `INPUT_TIMEOUT`   | `int = 0`                                    | 输入等待时间。在这时间段内的消息将会被合并为同一条消息使用   |
| `LOG_LEVEL`       | `str = "INFO"`                               | 日志等级                                                     |
| `TELEGRAM_PROXY`  | `Optional[str] = None`                       | tg适配器代理，并使用该代理下载文件                           |
| `ENABLE_ADAPTERS` | `list = ["~.onebot.v11", "~.onebot.v12"]`    | 在入口文件中启用的 Nonebot 适配器(仅 Debug 环境)             |

**模型配置项(configs/models.yml)**

支持的模型和具体配置内容可参考 [Muicebot 的模型配置](https://bot.snowy.moe/guide/model)

不支持的字段: `template`, `template_mode`, `stream`, `function_call`

## Quick Start🚀

*Work In Progress.*

## Character Setting🧸

参见: [关于沐妮卡](https://bot.snowy.moe/about/Muika)

## About🎗️

大模型输出结果将按**原样**提供，由于提示注入攻击等复杂的原因，模型有可能输出有害内容。
模型输出内容**不代表**项目开发者立场。
使用本项目所产生的任何直接或间接后果（包括但不限于账号封禁、内容风险、**由于调用系统 API 而导致的文件丢失风险**），开发者不承担任何责任。

本项目基于 [BSD 3](https://github.com/Moemu/Muika-After-Story/blob/main/LICENSE) 许可证提供，涉及到再分发时请保留许可文件的副本。

本项目隶属于 [MuikaAI](https://github.com/MuikaAI)

项目初期使用了 [Muicebot](https://github.com/Moemu/Muicebot) 的基本框架实现，部分存在于 Muicebot 的配置可能不可用或过时。

<a href="https://www.afdian.com/a/Moemu" target="_blank"><img src="https://pic1.afdiancdn.com/static/img/welcome/button-sponsorme.png" alt="afadian" style="height: 45px !important;width: 163px !important;"></a>