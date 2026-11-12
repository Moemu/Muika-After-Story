# 联网搜索插件

Core 启动时自动加载此内置插件。插件提供 `web_search` 工具，支持 Tavily、Perplexity 和 DeepSeek。

在项目根目录的 `.env` 中配置，然后重启 Core：

```dotenv
WEB_SEARCH_PROVIDER=deepseek
WEB_SEARCH_API_KEY=你的 DeepSeek API Key
```

`WEB_SEARCH_PROVIDER` 可选 `tavily`、`perplexity`、`deepseek`。留空或未配置密钥时，工具返回不可用说明。
现有 Tavily、Perplexity 配置可继续使用，无需更名。

配置由插件的 `WebSearchConfig` 独立读取。搜索请求继续使用全局 `PROXY`。

DeepSeek 使用 [`POST /v1/tools/search`](https://api-docs.deepseek.com/zh-cn/api/search/)，每次最多返回 5 条结果。
工具优先提供结果中的 `summary`，缺失时使用 `snippet`。需要全文时，可继续调用 `fetch_web_content`。

`time_range` 支持 `day`、`week`、`month`、`year`。Tavily 使用原生过滤；Perplexity 忽略此参数。
DeepSeek 使用 UTC 日期转换为 `after:` 条件，分别回溯 1、7、30、365 天。
DeepSeek 的搜索词连同日期条件不能超过 128 个 Unicode 字符；超长时工具返回错误，不会截断搜索词。
