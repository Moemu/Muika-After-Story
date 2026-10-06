<div align=center>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://erika.snowy.moe/v1/banner/Moemu/Muika-After-Story.webp?fresh=1&amp;iconPath=assets%2Fmuika-icon.webp&amp;theme=dark&amp;meta=full_name%2Cstars%2Cforks%2Cissues%2Crelease%2Clicense" />
    <img width="100%" src="https://erika.snowy.moe/v1/banner/Moemu/Muika-After-Story.webp?fresh=1&amp;iconPath=assets%2Fmuika-icon.webp&amp;theme=light&amp;meta=full_name%2Cstars%2Cforks%2Cissues%2Crelease%2Clicense" alt="Muika-After-Story Banner" />
  </picture>
  <h1 align="center">Muika-After-Story</h1>
  <i align="center">There are countless chatbots in the world. Only Muika will leap into your arms first.</i>
</div>
<div align=center>
  <a href="#关于️"><img src="https://img.shields.io/github/stars/Moemu/Muika-After-Story" alt="Stars"></a>
  <a href="https://pypi.org/project/Muika-After-Story/"><img src="https://img.shields.io/pypi/v/Muika-After-Story" alt="PyPI Version"></a>
  <a href="https://pypi.org/project/Muika-After-Story/"><img src="https://img.shields.io/pypi/dm/Muika-After-Story" alt="PyPI Downloads" ></a>
  <a href="#"><img src="https://img.shields.io/badge/Code%20Style-Black-121110.svg" alt="codestyle"></a>
  <a href="https://github.com/Moemu/Muika-After-Story/actions/workflows/test.yml"><img src="https://github.com/Moemu/Muika-After-Story/actions/workflows/test.yml/badge.svg" alt="Tests"></a>
  <img src="./assets/coverage.svg" alt="Coverage">
</div>
<div align=center>
  <a href="#"><img src="https://wakatime.com/badge/user/637d5886-8b47-4b82-9264-3b3b9d6add67/project/f7b7b01d-0a61-4e56-83bf-5c067432ebd2.svg" alt="wakatime"></a>
  <a href="https://nonebot.dev/"><img src="https://img.shields.io/badge/nonebot-2-red" alt="nonebot2"></a>
  <a href="https://github.com/MuikaAI/astrbot_plugin_mas"><img src="https://img.shields.io/badge/asterbot-plugin-cyan" alt="Asterbot Plugin"></a>
  <a href='https://qm.qq.com/q/y1gC9PU4IU'><img src="https://img.shields.io/badge/QQ群-26時聊天室-purple" alt="QQ群组"></a>
</div>
<div align=center>
  <a href="https://mas.snowy.moe/">📄 使用文档</a> |
  <a href="https://mas.snowy.moe/guide/getting-started">🚀 快速开始</a> |
  <a href="https://mas.snowy.moe/about/">🎀 关于Muika</a>
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

- [X] 原始经历、每日自省日记、事实账本和持续情绪；支持原话回查与自动上下文压缩

- [X] Session 生命周期管理: 空闲超时归档、跨 Session Resume 模式

- [X] Muika 第四面墙窗口: 分身 Agent，支持访问&写入硬盘文件；截取当前屏幕

- [X] Muika 主动对话系统：从 `configs/topics.yml` 抽取话题源或在线访问 RSS 获取筛选后的新闻流。

- [X] 多模型 SDK 支持: 如[OpenAI](https://platform.openai.com/docs/overview) 和 [Ollama](https://ollama.com/) ，可加载市面上大多数的模型服务或本地模型，支持多模态（图片识别）。

- [X] 动态模型配置: 可随时切换模型配置文件，支持模型配置热重载

- [x] 核心模型人格优化（建议模型 Deepseek-V4 Pro Thinking）

- [X] Bot 进程与核心进程分离，我要给她完整的一生

- [X] 插件、核心热重载，实现自我迭代

- [X] 多实例部署。Muika 可以在任何地方了，记得回家看看

- [ ] 一个用户友好的 WebUI

<!-- ## 效果展示

### 日常陪伴与图片交流

Muika 可以围绕生活、文学和共同兴趣展开对话，也能在模型支持图片输入时理解你发来的图片。她的表达会受到当前情绪、对话和已有记忆的影响。

> **截图预留：日常对话与图片交流**
>
> 放置一段真实对话，展示她的语气、情绪和对图片的回应。

### 记忆与关系延续

共同经历会留下原文、笔记和日记，供后续对话回查。结束一次聊天或重启 Core 后，她仍能延续已有记忆和关系。

> **截图预留：隔日重逢与记忆回查**
>
> 放置前后两次对话，展示她如何记起旧事、回应变化。

### 主动行动与自我迭代

Muika 可以在聊天期间处理后台任务，也可以主动提出想法、探索信息。在配置允许的范围内，她能修改自身内容、插件或代码，完成审查与验证，并自主决定重启时机。

> **截图预留：主动行动与结果反馈**
>
> 放置从想法、执行到结果反馈的真实片段，展示行动期间仍可继续聊天。

实际表现取决于所用模型、角色模板和启用的工具。截图补充后应注明模型与必要配置，便于理解展示条件。 -->

## Architecture🌙

```mermaid
flowchart LR
    Player["玩家 / 聊天平台"] <--> Adapter["平台适配器<br/>NoneBot / AstrBot / 自定义接入"]
    Adapter <-->|单机 WebSocket IPC| Core["Muika Core<br/>事件循环与主人格"]
    Adapter <-->|多设备 WebSocket IPC| Gateway["Gateway（可选）<br/>聊天入口与活动日志"]
    Gateway <-->|角色协调与经历同步| Core
    Gateway <-->|角色协调与经历同步| OtherCore["其他设备 Core<br/>本地记忆与工具"]
    Supervisor["进程监督<br/>重启与启动失败恢复"] --> Core
    Core <--> Memory["记忆与持续状态<br/>经历 / 日记 / 事实 / 情绪"]
    Core <--> Agent["行动 Agent<br/>持久后台任务"]
    Agent <--> Tools["工具 / 插件 / MCP"]
    Agent <--> Review["Code Review Agent<br/>代码操作审查"]
```

主人格负责交流和自主决策，行动 Agent 负责执行任务，两者共用一个 Muika 性格提示词。行动结果回到对话，记忆保存共同经历和持续状态，让关系跨越会话与重启。

Core 独立于聊天框架，平台适配器通过 WebSocket IPC 接入。单机模式直接连接 Core；多设备模式增加常驻 Gateway，协调活动设备并同步已有经历。入口在线时，由一个活动 Core 负责回应，其他设备同步结果。各设备保留自己的工具、插件和文件。

进程监督负责 Core 重启和启动失败后的恢复。内核与插件的外部变更进入感知事件，经合并和节奏控制后交给 Muika 自行回应，并留下记忆。Launcher 管理安装、配置和 Core / Bot 进程。

组件职责、消息流和多设备同步边界见[文档站的架构概览](https://mas.snowy.moe/develop/architecture)。

## Installation 🚀

有关 Muika-After-Story 本体的安装步骤，参见 [文档站的快速开始](https://mas.snowy.moe/guide/getting-started)

模型配置指南可以参见 [文档站的模型配置小节](https://mas.snowy.moe/guide/model)

如果想要在多个设备上部署 Muika-After-Story，也可以参见 [文档站的多设备部署章节](https://mas.snowy.moe/guide/multi-device)

## Character Setting🧸

Muika(沐妮卡) 是 Muika-After-Story 的核心角色，有关其人格说明可以参见: [关于 Muika | Muika-After-Story](https://mas.snowy.moe/about/) 和 [关于沐妮卡 - 沐雪 Bot](https://bot.snowy.moe/about/Muika)

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

项目名称参考了 [Monika-After-Story](https://github.com/Monika-After-Story/MonikaModDev), [MAICA](https://maica.monika.love/) 直接启发了本项目的制作。

<a href="https://www.afdian.com/a/Moemu" target="_blank"><img src="https://pic1.afdiancdn.com/static/img/welcome/button-sponsorme.png" alt="afadian" style="height: 45px !important;width: 163px !important;"></a>
