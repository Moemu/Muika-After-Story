# Bot 配置

| 配置项 | 用途 |
| --- | --- |
| `MASTER_ID` | 主人账号；未设置时，沿用 NoneBot 的 `SUPERUSERS` 隐式回退，取其迭代得到的首个账号 |
| `CORE_WS_URL` | Core 或 Gateway 的 Bot WebSocket 地址，默认 `ws://127.0.0.1:8765/ws` |
| `CORE_FALLBACK_URLS` | JSON 数组格式的备用连接地址 |
| `CLIENT_NAME` | 当前 Bot 实例的唯一名称 |
| `INPUT_TIMEOUT` | 合并连续消息前的等待时间，单位为秒，默认 `0` |

配置多个超级用户时，用 `MASTER_ID` 明确指定主人账号。NoneBot 将 `SUPERUSERS` 存为集合，回退结果不保证与 `.env` 中的书写顺序一致。
