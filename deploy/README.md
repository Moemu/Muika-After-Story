# QQ Bot 部署指南

通过 NapCatQQ + OneBot v11 协议将 Muika 接入 QQ。

## 电脑关机后继续聊天

如果希望电脑关机后仍能与 Muika 聊天，请参考文档站的[多设备部署指南](https://mas.snowy.moe/guide/multi-device)。
你需要一台持续运行的服务器，并将聊天机器人也运行在服务器上。

以下步骤介绍原有的单机连接方式。

## 准备什么

你需要运行 MAS，以及两个负责 QQ 接入的程序：

| 程序 | 用途 |
| --- | --- |
| Muika Core | 保存记忆，生成回复，执行她的行动 |
| NapCat | 登录用于聊天的 QQ 账号 |
| muika-bot | 把 QQ 消息交给 Muika，再把回复发回 QQ |

以下示例在电脑上运行 Muika Core，在同一台机器的 Docker 中运行 QQ 接入程序。
需要 Python 3.10～3.13、Docker 和 Docker Compose v2，以及一个用于 Muika 的 QQ 账号。
电脑关机后，这种部署无法继续回复。需要持续聊天时，请使用上方的多设备部署指南。

## 部署流程

### 搭建 Core 环境

先启动负责保存记忆和生成回复的 Core，再启动聊天机器人。

```bash
# 1. 克隆项目并安装依赖
cd Muika-After-Story
pip install -e '.[standard]'

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
> 需要长期运行时，请设置开机自动启动，并在程序退出后自动重启。

---

### 配置 QQ Bot 环境

```bash
# 编辑项目根目录的 .env.qq，填写实际值：
#    - MASTER_ID     → 你的 QQ 号
#    - SUPERUSERS    → 同上
#    - IPC_SECRET    → 从 Core 的 .env 中复制过来的值
#    - 其他项保持默认即可
```

> Compose 默认读取项目根目录的 `.env.qq`。
> 这个文件是仓库中的配置示例。填写密钥后，请保留在本机，切勿将密钥提交到仓库。

---

### 启动 NapCat 和 muika-bot 适配器

```bash
cd deploy
docker compose up -d
```

这会构建并启动聊天机器人，同时启动 NapCat。

NapCat 管理页面只监听部署机器本地的 `6099` 端口。

**首次使用需要登录 QQ：**

在部署机器的浏览器打开 `http://127.0.0.1:6099/webui`，用手机 QQ 扫码登录。
部署在远程服务器时，可以先通过 SSH 转发管理端口，再在自己的电脑上打开同一地址：

```bash
ssh -L 6099:127.0.0.1:6099 服务器用户名@服务器地址
```

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
| **全新部署** | Core 首次启动时自动生成 `IPC_SECRET` 并写入 `.env`。将 `.env` 中的值复制到 `.env.qq` 的对应字段 |
| **已有 Core** | `.env` 中的 `IPC_SECRET` 已经存在，直接复制即可 |
| **手动指定** | 在启动 Core **之前**，在 `.env` 中填入自定义的 `IPC_SECRET=xxxx`。Core 会使用这个值而不会自动生成。然后在 `.env.qq` 中使用同样的值 |

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
# 打开 WebUI http://127.0.0.1:6099/webui 扫码
```

**Bot 日志显示 "Ignored non-master message"：**
- 确认 `.env.qq` 中 `MASTER_ID` 设置正确
- 确认 `SUPERUSERS` 也包含同样的 QQ 号
