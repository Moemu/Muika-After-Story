# QQ Bot 部署指南

通过 NapCatQQ + OneBot v11 协议将 Muika 接入 QQ。

## 架构

```
宿主机 (Windows)
├── Muika Core                             ── AI 引擎（LLM / 记忆 / Butler Agent）
│   python -m muika.ipc.bootstrap
│   ws://0.0.0.0:8765/ws
│
└── Docker  (WSL2 / Other Linux Server)
    ├── muika-napcat                       ── QQ 协议实现（NapCatQQ）
    │   镜像: mlikiowa/napcat-docker:latest
    │   端口: 3000(HTTP) / 3001(WS) / 6099(WebUI)
    │
    └── muika-bot                 ── NoneBot
        镜像: deploy/bot.Dockerfile (构建)
        nonebot-adapter-onebot  ←→  napcat:3001
        IPC Client  ←→  host.docker.internal:8765
```

**消息流：**

```
用户 → [QQ] → NapCat → OneBot WS → muika-bot → IPC → Core
Core → IPC → muika-bot → OneBot API → NapCat → [QQ] → 用户
Core → (主动消息, 孤独感/话题驱动) → IPC → muika-bot → NapCat → [QQ]
```

## 前置条件

- Linux 服务器（带 Docker + Docker Compose v2）
- Python 3.10+
- 一个可用的 QQ 号（用于 NapCat 登录）

---

## 部署流程

### 搭建 Core 环境

Core 是 Muika 的 AI 引擎，**必须先于 Bot 启动**。

```bash
# 1. 克隆项目并安装依赖
cd Muika-After-Story
pip install -e .[standard]

# 2. 配置 Core 的运行环境
#    编辑或创建 .env 文件，至少填入 LLM 模型配置

# 3. 启动 Core
python -m muika.ipc.bootstrap
```

Core 首次启动时会**自动生成 `IPC_SECRET`**，写入项目根目录的 `.env` 文件。记下这个值，下一步会用到：

```bash
grep IPC_SECRET .env
# 输出示例: IPC_SECRET=abc123...
```

> Core 已运行时不要关闭终端，另开一个终端执行后续操作。
> 生产环境建议用 systemd / pm2 管理 Core 进程。

---

### 配置 QQ Bot 环境

```bash
# 1. 复制环境变量模板
cp .env.qq .env.qq.local

# 2. 编辑 .env.qq.local，填写实际值：
#    - MASTER_ID     → 你的 QQ 号
#    - SUPERUSERS    → 同上
#    - IPC_SECRET    → 从 Core 的 .env 中复制过来的值
#    - 其他项保持默认即可
```

> `.env.qq.local` 是本地配置，不会被 git 追踪（已在 `.gitignore` 中）。
> 也可直接用 `.env.qq` 编辑，但注意不要提交密钥到仓库。

---

### 启动 NapCat 和 muika-bot 适配器

```bash
cd deploy
docker compose up -d
```

这会构建 bot 镜像并启动 Napcat。

NapCat 容器启动后将暴露 `6099` 作为 NapCat WebUI 管理后台

**首次使用需要登录 QQ：**

在浏览器打开 `http://<服务器IP>:6099/webui`，用手机 QQ 扫码登录。

> 首次登录后 NapCat 可能自动退出，再次执行 `docker compose restart` 即可。
> 登录成功后的会话会持久化到 `deploy/napcat/QQ/`，下次启动无需重复扫码。

Bot 在容器内通过 `host.docker.internal:8765` 连接宿主机 Core。

---

## 配置参考

### `.env.qq` 关键配置项

| 变量 | 说明 | Docker 默认值 | 宿主机默认值 |
|------|------|---------------|-------------|
| `MASTER_ID` | 主人的 QQ 号 | **必填** | **必填** |
| `IPC_SECRET` | 与 Core 的通信密钥 | 从 Core 的 `.env` 中复制 | 同左 |
| `CORE_WS_URL` | Core 的 WebSocket 地址 | `ws://host.docker.internal:8765/ws` | `ws://127.0.0.1:8765/ws` |
| `ONEBOT_WS_URLS` | NapCat WebSocket 地址 | `["ws://napcat:3001"]` | `["ws://localhost:3001"]` |

### NapCat 持久化数据

```
deploy/napcat/
├── QQ/          # QQ 账号数据（登录态、缓存）—— 重启不丢
├── config/      # NapCat 配置文件
└── plugins/     # NapCat 插件
```

---

## IPC_SECRET 说明

| 场景 | 做法 |
|------|------|
| **全新部署** | Core 首次启动时自动生成 `IPC_SECRET` 并写入 `.env`。将 `.env` 中的值复制到 `.env.qq.local` 的对应字段 |
| **已有 Core** | `.env` 中的 `IPC_SECRET` 已经存在，直接复制即可 |
| **手动指定** | 在启动 Core **之前**，在 `.env` 中填入自定义的 `IPC_SECRET=xxxx`。Core 会使用这个值而不会自动生成。然后在 `.env.qq.local` 中使用同样的值 |

Core 和 Bot 的 `IPC_SECRET` **必须一致**，否则 Bot 无法连接 Core。

---

## 群聊说明

- 只有 `MASTER_ID` 的 QQ 号在群里 @Muika 会得到响应
- 群内其他人 @Muika 会被忽略
- 回复会发到群内，而不是私聊
- Muika 的主动消息（孤独感/话题驱动）会发到**上一次对话所在的位置**（群聊或私聊）
  - 如果从未对话过，主动消息发到私聊
  - 如果想切换对话位置，在新的位置发一条消息即可

## 注意事项

1. **QQ 号风控**：使用账号协议（非官方 Bot API）的机器人有封号风险。建议使用小号
2. **扫码登录时效**：NapCat 的 QQ 登录态可能过期，需要定期重新扫码
3. **Core 先启动**：确保 Core 在 muika-bot 之前启动，否则 Bot 会等待重连
4. **日志查看**：`docker compose logs -f muika-bot` 查看 Bot 日志，`docker compose logs -f napcat` 查看 NapCat 日志
5. **IPC 连接失败**：确认 `CORE_WS_URL` 和 `IPC_SECRET` 都配置正确

## 故障排查

**Bot 无法连接 Core：**
```bash
# 确认 Core 正在运行
curl http://localhost:8765/health

# 确认 docker 可访问宿主机端口
docker compose exec muika-bot curl http://host.docker.internal:8765/health
```

**NapCat 无法登录：**
```bash
# 查看 NapCat 日志
docker compose logs napcat
# 打开 WebUI http://<ip>:6099/webui 扫码
```

**Bot 日志显示 "Ignored non-master message"：**
- 确认 `.env.qq` 中 `MASTER_ID` 设置正确
- 确认 `SUPERUSERS` 也包含同样的 QQ 号
