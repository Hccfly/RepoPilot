# DeepSeek API 配置

RepoPilot P0 通过 DeepSeek 的 OpenAI 兼容 Chat Completions API 完成只读探索与规划。

## 安装模型依赖

```powershell
cd D:\实习\RepoPilot
.venv\Scripts\python.exe -m pip install -e ".[agent]"
```

## 配置 API Key

推荐在项目根目录创建 `D:\实习\RepoPilot\.env`：

```dotenv
DEEPSEEK_API_KEY=你的 DeepSeek API Key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

RepoPilot 启动时会通过 `python-dotenv` 自动加载这个文件，并采用 `override=False`：如果当前进程已经
显式设置了同名环境变量，以进程环境变量为准。`.env` 已被 `.gitignore` 排除。

也可以只在当前 PowerShell 会话中设置：

```powershell
$env:DEEPSEEK_API_KEY = "你的 DeepSeek API Key"
$env:DEEPSEEK_BASE_URL = "https://api.deepseek.com"
$env:DEEPSEEK_MODEL = "deepseek-v4-flash"
```

RepoPilot 不从 TOML 读取 API Key，也不会把 Key 放进 `AppConfig`、`RunState`、工具参数或终端输出。
如果使用 PowerShell 临时环境变量，关闭当前终端后变量会失效。

## 检查与调用

```powershell
repopilot doctor --repo D:\path\to\target-repo
repopilot plan "修复解析器空输入异常" --repo D:\path\to\target-repo
repopilot plan "修复解析器空输入异常" --repo D:\path\to\target-repo --json
```

`doctor` 只显示 Key 是否存在，不显示 Key 内容。`plan` 会调用两次模型：

1. Explorer 根据仓库清单输出检索词和候选文件。
2. Planner 根据检索命中与实际读取内容输出修改计划。

两次调用都启用 JSON Output，并在提示词中声明 JSON 结构。空内容、非法 JSON、引用未读取文件等
情况都会使任务进入 `FAILED`，不会继续执行。

## 默认参数

| 参数 | 默认值 |
|---|---|
| Base URL | `https://api.deepseek.com` |
| Model | `deepseek-v4-flash` |
| Thinking | 关闭 |
| Max tokens | `4096` |
| Timeout | `60` 秒 |

模型、Thinking、Token 和超时也可以在 `.repopilot/config.toml` 或 `repopilot.toml` 中配置；
参考项目根目录的 `repopilot.toml.example`。

官方接口参考：

- [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)
- [JSON Output](https://api-docs.deepseek.com/zh-cn/guides/json_mode/)
- [模型与价格](https://api-docs.deepseek.com/quick_start/pricing)
