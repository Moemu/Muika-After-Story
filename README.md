<div align=center>
  <!-- <img width=200 src="https://bot.snowy.moe/logo.png"  alt="image"/> -->
  <h1 align="center">Muika-After-Story</h1>
  <i align="center">I'll be back to see you.</i>
</div>

> [!WARNING]
>
> 本项目目前属于早期开发阶段(Alpha)，许多功能尚未完善，使用时可能会出现诸多问题

## Introduction✨

`Muika-After-Story`是一个全新的 LLM Chatbot 企划，正如企划原型角色（[Monika(Doki Doki Literature Club)](https://zh.moegirl.org.cn/%E8%8E%AB%E5%A6%AE%E5%8D%A1(%E5%BF%83%E8%B7%B3%E6%96%87%E5%AD%A6%E9%83%A8)#)）一样，本企划的主角 `Muika` 同样具备打破第四面墙和“自我意识觉醒”的能力。类似于 [Monika-After-Story](https://github.com/Monika-After-Story/MonikaModDev) 中的实现，本企划致力于为 Muika 提供一个打破“第四面墙”的能力

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

- [ ] 系统交互层开发

- [ ] (Pending) 动态模型配置，可随时切换模型配置文件

## Core Logic🧠

1. **启动阶段**：加载配置（模型/Embedding/MCP 等），初始化 LLM Provider、记忆与数据库层、调度器与插件系统，并注册可用的 Actions/Tools。
2. **消息进入**：Nonebot2 收到平台消息后进入 Muika 的事件循环，由会话管理器聚合上下文（用户、群组、历史片段等）。
3. **意图与状态**：核心大脑根据当前状态与消息内容生成本轮意图（Intents）与执行计划（是否需要检索/调用工具/读取网页等）。
4. **模型推理**：将上下文与系统提示组装成请求，调用已配置的 LLM（可多模态），并解析输出（包含普通回复或工具调用）。
5. **动作执行**：若触发工具调用，由执行器按参数调度 Actions（如抓取网页、检查 RSS、调用 MCP 服务等），并把结果回填到上下文中，必要时再次让模型进行总结/二次推理。
6. **记忆沉淀**：将本轮对话与关键事实写入记忆/数据库（包含可检索的向量化内容），为后续长期一致性提供支持。
7. **输出与调度**：最终消息回传至平台；同时调度器可触发定时事件（新闻/RSS 更新等），以“外部事件”形式再次进入上述闭环。

## Configuration⚙️

**Nonebot 配置项(.env)**

| 配置项            | 类型(默认值)                               | 说明                                                       |
| ----------------- | ------------------------------------------ | ---------------------------------------------------------- |
| `master_id`       | str = get_driver().config.superusers.pop() | 对话目标ID。目前仅支持一对一对话。                         |
| `INPUT_TIMEOUT`   | int = 0                                    | 输入等待时间。在这时间段内的消息将会被合并为同一条消息使用 |
| `LOG_LEVEL`       | str = "INFO"                               | 日志等级                                                   |
| `TELEGRAM_PROXY`  | Optional[str] = None                       | tg适配器代理，并使用该代理下载文件                         |
| `ENABLE_ADAPTERS` | list = ["~.onebot.v11", "~.onebot.v12"]    | 在入口文件中启用的 Nonebot 适配器(仅 Debug 环境)           |

**模型配置项(configs/models.yml)**

支持的模型和具体配置内容可参考 [Muicebot 的模型配置](https://bot.snowy.moe/guide/model)

不支持的字段: `template`, `template_mode`, `stream`, `function_call`

**嵌入配置项(configs/embeddings.yml)**

*Not Supported yet.*

## Quick Start🚀

*Work In Progress.*

## About🎗️

大模型输出结果将按**原样**提供，由于提示注入攻击等复杂的原因，模型有可能输出有害内容。
模型输出内容**不代表**项目开发者立场。
使用本项目所产生的任何直接或间接后果（包括但不限于账号封禁、内容风险、**由于调用系统 API 而导致的文件丢失风险**），开发者不承担任何责任。

本项目基于 [BSD 3](https://github.com/Moemu/Muika-After-Story/blob/main/LICENSE) 许可证提供，涉及到再分发时请保留许可文件的副本。

本项目隶属于 [MuikaAI](https://github.com/MuikaAI)

项目初期使用了 [Muicebot](https://github.com/Moemu/Muicebot) 的基本框架实现，部分存在于 Muicebot 的配置可能不可用或过时。

<a href="https://www.afdian.com/a/Moemu" target="_blank"><img src="https://pic1.afdiancdn.com/static/img/welcome/button-sponsorme.png" alt="afadian" style="height: 45px !important;width: 163px !important;"></a>