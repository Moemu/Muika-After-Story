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

- [ ] 动态模型配置，可随时切换模型配置文件

- [ ] 使用 Jinja2 动态生成人设提示词，搭配模型配置文件可动态切换模型人设

- [ ] 系统交互层开发

- [ ] Muika 主交互逻辑开发


## SDK Support📌

| Python SDK  | Stream | MultiModal | Thinking | Tool Call | Online Search |
| ----------- | ------ | ---------- | -------- | --------- | ------------- |
| `Azure`     | ✅      | 🎶🖼️/❌       | ⭕        | ✅         | ❌             |
| `Dashscope` | ✅      | 🎶🖼️/❌       | ✅        | ⭕         | ✅             |
| `Gemini`    | ✅      | ✅/🖼️        | ⭕        | ✅         | ✅             |
| `Ollama`    | ✅      | 🖼️/❌        | ✅        | ✅         | ❌             |
| `Openai`    | ✅      | ✅/🎶        | ✅        | ✅         | ❌             |

✅：表示此加载器能很好地支持该功能并且 `Muika-After-Story` 已实现

⭕：表示此加载器虽支持该功能，但使用时可能遇到问题

🚧：表示此加载器虽然支持该功能，但 `Muika-After-Story` 未实现或正在实现中

❓：表示 Maintainer 暂不清楚此加载器是否支持此项功能，可能需要进一步翻阅文档和检查源码

❌：表示此加载器不支持该功能

多模态标记：🎶表示音频；🎞️ 表示视频；🖼️ 表示图像；📄表示文件；✅ 表示完全支持

## About🎗️

大模型输出结果将按**原样**提供，由于提示注入攻击等复杂的原因，模型有可能输出有害内容。无论模型输出结果如何，模型输出结果都无法代表开发者的观点和立场。对于此项目可能间接引发的任何后果（包括但不限于机器人账号封禁），本项目所有开发者均不承担任何责任。

本项目基于 [BSD 3](https://github.com/Moemu/Muika-After-Story/blob/main/LICENSE) 许可证提供，涉及到再分发时请保留许可文件的副本。

本项目隶属于 [MuikaAI](https://github.com/MuikaAI)

<a href="https://www.afdian.com/a/Moemu" target="_blank"><img src="https://pic1.afdiancdn.com/static/img/welcome/button-sponsorme.png" alt="afadian" style="height: 45px !important;width: 163px !important;"></a>