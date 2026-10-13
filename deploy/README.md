# Bot 部署指南

MAS 以完整 Bot 项目提供 NoneBot 接入。Core 管理对话、记忆和行动；Bot 连接聊天平台，并通过 IPC 与 Core 通信。
默认镜像包含 OneBot v11，以及 FastAPI、HTTPX 和 WebSocket 驱动依赖。Telegram 等平台按需安装适配器，无需运行 NapCat。

电脑关闭后仍需继续聊天时，请先阅读[多设备部署指南](https://mas.snowy.moe/guide/multi-device)。
本页命令均在仓库根目录执行，需要 Docker 和 Docker Compose v2。

## 准备 Core 和配置

按[快速开始](https://mas.snowy.moe/guide/getting-started#manual)准备 Core、模型配置和项目根目录的 `.env`，先启动 Core：

```bash
uv run --no-sync python core_main.py
```

Core 首次启动时生成缺失的 `IPC_SECRET`，并写入 `.env`。Bot 必须使用相同密钥。
若 Core 在宿主机上，容器使用以下地址；Core 需监听容器可访问的接口，并允许该连接：

```dotenv
CORE_WS_URL=ws://host.docker.internal:8765/ws
```

若 Core 在其他服务器或通过 Gateway 提供接入，填写对应聊天入口地址。
Compose 将 `.env` 作为环境变量和只读文件传入 Bot。文件挂载供 NoneBot 读取适配器自定义配置；`data/` 保存运行数据和协议确认记录。

## 选择平台

### QQ：默认 OneBot v11

在 `.env` 中填写你的 QQ 号：

```dotenv
MASTER_ID=123456789
SUPERUSERS=["123456789"]
ENABLE_ADAPTERS=["nonebot.adapters.onebot.v11"]
CLIENT_NAME=qq
```

合并 QQ 配置，构建 Bot 并启动 NapCat：

```bash
docker compose --env-file .env -f deploy/docker-compose.yml -f deploy/qq.compose.yml build muika-bot
docker compose --env-file .env -f deploy/docker-compose.yml -f deploy/qq.compose.yml up -d napcat
docker compose --env-file .env -f deploy/docker-compose.yml -f deploy/qq.compose.yml run --rm --no-deps muika-bot python -m muika.agreement confirm
docker compose --env-file .env -f deploy/docker-compose.yml -f deploy/qq.compose.yml up -d muika-bot
```

在 Docker 宿主机打开 `http://127.0.0.1:6099/webui` 并登录 QQ。远程管理时，可通过 SSH 转发该端口。
在 NapCat 中启用 OneBot v11 WebSocket 服务端，监听 `0.0.0.0:3001`。Bot 通过 `ws://napcat:3001` 连接它。
如果设置 OneBot 访问令牌，Bot 的 `ONEBOT_ACCESS_TOKEN` 需与 NapCat 一致。

QQ 覆盖文件只为 NapCat 发布 WebUI 端口，并将账号、配置和插件保存在 `deploy/napcat/` 下。
只有 `MASTER_ID` 的消息会进入 Core；群聊需要 @Muika。当前回复和主动消息均发到主人的私聊。

### Telegram：按需添加适配器

在 `.env` 中填写以下内容，并保留已有的 `IPC_SECRET` 和 `CORE_WS_URL`：

```dotenv
MASTER_ID=你的Telegram数字用户ID
SUPERUSERS=["你的Telegram数字用户ID"]
ENABLE_ADAPTERS=["nonebot.adapters.telegram"]
DRIVER=~fastapi+~httpx
TELEGRAM_BOTS=[{"token":"你的BotToken"}]
CLIENT_NAME=telegram
BOT_EXTRA_DEPENDENCIES=nonebot-adapter-telegram==0.1.0b20
```

构建并启动：

```bash
docker compose --env-file .env -f deploy/docker-compose.yml build muika-bot
docker compose --env-file .env -f deploy/docker-compose.yml run --rm muika-bot python -m muika.agreement confirm
docker compose --env-file .env -f deploy/docker-compose.yml up -d muika-bot
```

此示例使用长轮询，无需对外开放 Bot 端口。Webhook、代理等选项见 [Telegram 适配器手册](https://github.com/nonebot/adapter-telegram/blob/beta/MANUAL.md)。

### 其他 NoneBot 适配器

三个步骤分别配置：

1. 用 `BOT_EXTRA_DEPENDENCIES` 指定要安装的包，多个包以空格分隔，建议固定版本。更改后重新构建镜像。
2. 用 `ENABLE_ADAPTERS` 指定已安装适配器的模块名，例如 `["nonebot.adapters.telegram"]`。
3. 按适配器手册配置 `DRIVER`、平台凭据和连接参数。驱动所需的可选依赖也放入构建依赖。

`ENABLE_ADAPTERS` 必须是非空 JSON 字符串数组；省略时加载 OneBot v11。加载配置不会安装包或切换驱动。
适配器还需受 Alconna 通用消息接口支持；能被 NoneBot 注册不代表所有平台功能都可用。
`BOT_EXTRA_DEPENDENCIES` 只传公开依赖规格，不用于传递凭据；Token 等配置留在运行时 `.env` 中。
镜像默认依赖使用仓库的 `uv.lock`。额外包与这些版本冲突时，构建会报错；需先在实例源码中用 `uv add` 更新并验证锁文件，再构建镜像。

直接构建镜像时，可使用相同构建参数：

```bash
docker build -f deploy/bot.Dockerfile --build-arg 'BOT_EXTRA_DEPENDENCIES=nonebot-adapter-telegram==0.1.0b20' -t muika-bot:telegram .
```

## 源码运行

当前 wheel 只包含 Core 包。运行 NoneBot Bot 需保留仓库中的 `bot.py` 和 `muika_bot/`。
安装默认依赖后，用 `uv add` 保存实例额外依赖：

```bash
uv sync --extra nonebot
uv add --optional nonebot "nonebot-adapter-telegram==0.1.0b20"
uv sync --extra nonebot
uv run --no-sync python -m muika.agreement confirm
uv run --no-sync python bot.py
```

`uv add` 修改当前实例的 `pyproject.toml` 和 `uv.lock`；后续同步环境会保留这些依赖。
仅用 `uv pip install` 临时安装的包可能被下次 `uv sync` 移除。使用 pip 时，将额外依赖记录在实例的依赖文件中。
Core 和 Bot 共用源码环境时，同步时还需保留 Core 所需的 `standard` 等可选依赖。

## 更新与排查

修改源码或依赖后，重新执行对应平台的构建命令，再运行 `up -d muika-bot`。
QQ 命令保留两个 `-f` 参数。查看日志：

```bash
docker compose --env-file .env -f deploy/docker-compose.yml logs -f muika-bot
```

Bot 启动失败时，先按日志检查配置数组、适配器包和驱动依赖。
IPC 连接失败时，检查 `CORE_WS_URL`、Core 的监听地址和双方的 `IPC_SECRET`；Core 健康检查地址为 `/health`。
平台连接后，用 `MASTER_ID` 对应账号发一条私聊，确认回复能通过平台返回。
删除容器时保留 `data/` 和 `deploy/napcat/`，升级不会替换其中的运行数据。

## Core 退出

Core 收到首次 Ctrl+C 或 SIGTERM 后，会停止接入并等待对话摘要和资源清理。
监督进程最多等待 180 秒；再次收到停止信号时立即强制退出，可能使本次摘要无法保存。

`multi-device.compose.yml` 为 Core 设置了 190 秒退出宽限期。
单独运行 Core 容器时，使用 `docker stop --time 190 <容器名>`，为清理保留同样的时间。SIGKILL 无法触发退出清理。
