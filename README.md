# RepoPilot

RepoPilot 是一个面向真实 Git 仓库的可恢复 Coding Agent CLI。它把 Issue 或自然语言任务转换为
经过审批、测试验证且可审计的代码补丁。

当前仓库已完成 P0 只读规划、P1 补丁生命周期、P2 验证闭环和 P3 有限修复：DeepSeek 的
Explorer、Planner 与 Editor 会先生成有证据支撑的计划和 unified diff；用户查看 diff 并显式执行
`apply` 后才会写入目标仓库，随后 `verify` 用白名单命令证明补丁有效，独立只读的 `review` 节点
决定任务能否进入 `DONE`。

## 快速开始

```powershell
cd D:\实习\RepoPilot
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[agent]"

$env:DEEPSEEK_API_KEY = "你的 DeepSeek API Key"
$env:DEEPSEEK_MODEL = "deepseek-v4-flash"

repopilot doctor --repo .
repopilot status --repo .
repopilot task "修复登录页空邮箱时崩溃的问题" --repo .
repopilot plan "修复登录页空邮箱时崩溃的问题" --repo .
repopilot propose "修复登录页空邮箱时崩溃的问题" --repo .
repopilot diff <run_id>
repopilot apply <run_id>
repopilot verify <run_id>
repopilot resume <run_id>
repopilot list
repopilot undo <run_id>
```

不安装也可以直接运行：

```powershell
$env:PYTHONPATH = "src"
python -m repopilot doctor --repo .
python -m unittest discover -s tests -v
```

## 产品工作流

```text
DISCOVER -> PLAN -> WAIT_APPROVAL -> PATCH -> VERIFY -> REVIEW -> DONE
                                      ^          |
                                      +-- REPAIR-+
```

- `Explorer`：并行执行只读检索与文件读取，结果按输入顺序归并。
- `Editor`：只接收已批准的结构化补丁请求。
- `Verifier`：只运行策略允许的 test/lint/typecheck；每个动作在独立子进程中执行，可并行但历史顺序固定。
- `Orchestrator`：维护状态、预算、审批、恢复和最终证据。

首版不自动执行 `commit`、`push`、依赖安装、联网命令或任意 shell。

## 当前可用命令

| 命令 | 当前行为 |
|---|---|
| `repopilot doctor` | 检查 Python、Git、仓库和配置状态 |
| `repopilot status` | 显示仓库根、分支和工作区状态 |
| `repopilot task` | 创建一个 JSON 可序列化的任务初始状态 |
| `repopilot plan` | 调用 DeepSeek 执行只读探索并生成结构化计划 |
| `repopilot propose` | 生成、校验并保存 unified diff，但不修改文件 |
| `repopilot diff` | 从外部台账显示待审查补丁 |
| `repopilot apply` | 将显式命令视为审批，冲突检查通过后整体应用补丁 |
| `repopilot verify` | 按白名单执行 pytest/ruff/npm test/npm run lint，失败则不进入 `DONE` |
| `repopilot review` | 只读审查节点，结论必须引用补丁文件，通过后进入 `DONE` |
| `repopilot repair` | 最多一次定向修复，只允许改动已批准文件，随后重新验证 |
| `repopilot resume` | 从 checkpoint 继续；已验证通过的动作不会重复执行 |
| `repopilot report` | 输出 JSON/Markdown 运行报告，包含 diff 摘要、验证证据与停止原因 |
| `repopilot list` | 列出运行台账及其阶段 |
| `repopilot providers` | 列出模型 Provider、默认模型、凭据变量与就绪状态 |
| `repopilot tui` | 打开交互式终端面板，在同一会话里完成全流程 |
| `repopilot status [run_id]` | 无参数时显示仓库状态，带 `run_id` 时显示该运行的 checkpoint |
| `repopilot undo` | 文件未被二次修改时，按台账逐字节恢复 |
| `repopilot commit` | 在单独审批且 `git_write` 已启用时，只提交本次运行涉及的文件 |
| `repopilot ci plan/propose/verify` | 无交互 CI 模式，写出产物并使用稳定退出码 |
| `repopilot sandbox serve` | 启动独立沙箱服务，按动作名执行白名单验证 |
| `repopilot rsi eval/compare/propose` | 对 RepoPilot 自身做基准评测，生成候选补丁但不自动应用 |
| `repopilot rsi run/history` | 连续生成多轮候选并保留 scorecard 历史 |

修复循环默认只重试一次，可通过 `[execution] max_repair_attempts` 调整，且不会修改原始提案之外
的文件。离线基准可用 `python -m repopilot.benchmark` 运行，全程不需要 API Key。实现顺序和
`commit` 默认被策略拒绝。启用需要显式设置 `[permissions] git_write = "ask"`（或 `allow`），并且
只提交本次运行 `apply` 过的文件：命令使用 pathspec 提交，不会带上你已暂存的其他改动；任务必须
已通过验证与审查（阶段 `DONE`），且文件内容仍需与应用回执一致。提交后 `undo` 会被拒绝，以免
留下悬空的未提交改动。RepoPilot 永不自动提交，也永不执行 `push`。

验收标准见
[`docs/roadmap.md`](docs/roadmap.md)，模块关系见 [`docs/architecture.md`](docs/architecture.md)。

运行台账默认位于 `%LOCALAPPDATA%\RepoPilot\runs`，不会写进目标仓库。可通过全局参数
`--runs-dir` 或环境变量 `REPOPILOT_RUNS_DIR` 覆盖。P2 起默认使用 SQLite
（`repopilot.sqlite3`）保存计划、审批、补丁回执和验证历史；加 `--store json` 可退回 P1 的
单文件 JSON 台账。
为了保护用户工作，补丁只要触及生成提案时
已修改或未跟踪的文件，就会被拒绝；请先提交、用 `git stash` 将改动移出工作区，或让提案避开
这些文件。仅执行 `git add` 仍会被视为已有改动。
P1 仅处理 UTF-8 文本文件的新增、修改和删除，不支持二进制补丁与重命名。详见
[`docs/p1-patch-lifecycle.md`](docs/p1-patch-lifecycle.md)。

## 自我改进（RSI）

`repopilot rsi eval` 用内置基准给当前代码打分，`rsi compare` 评测一个候选补丁，`rsi propose`
让 RepoPilot 针对失败的基准场景生成补丁并评测，`rsi run` 连续生成多轮候选并写入只增历史
（`rsi history` 查看）。评分由候选之外的固定评分器计算，候选只能修改 `src/repopilot/**`，
**补丁永不自动应用**——仍然需要你审阅 diff 后显式 `apply`。设计与信任边界见
[`docs/rsi.md`](docs/rsi.md)。

## CI 与远程沙箱

`repopilot ci plan|propose|verify` 是面向流水线的无交互入口：不读 stdin、不应用补丁，把计划、
diff 和报告写成产物，退出码固定为 `0` 成功 / `1` 任务失败 / `2` 环境错误。完整说明与可直接
复制的 GitHub Actions 工作流见 [`docs/ci.md`](docs/ci.md)。

验证默认在本机执行。需要隔离环境时有两种做法：配置一个固定 argv 前缀（容器/远程包装器均可），
或连接独立沙箱服务——后者只传输动作名，客户端无法让服务执行任意命令：

```toml
[sandbox]
mode = "command"
command = ["docker", "run", "--rm", "--network", "none", "-v", "{workspace}:/work", "-w", "/work", "python:3.12-slim"]
```

```powershell
repopilot sandbox serve --repo "D:\srv\checkout" --host 127.0.0.1 --port 8765
```

服务支持多租户（`--tenant name=path`，每租户独立 token 与并发上限）和 TLS 1.2+
（`--cert/--key`，客户端用 `tls = true` 与 `ca_file` 校验），细节见
[`docs/ci.md`](docs/ci.md)。请求会默认写入 SQLite 审计日志，可用
`repopilot sandbox audit` 查看；`--quota N --quota-window 3600` 可按租户限制请求速率，
配额跨重启生效。

需要一次性容器时用 `mode = "docker"`：每个验证动作一个 `docker run --rm`，工作区挂载到
`/work`，默认 `--network none`，超时会 `docker kill` 对应容器：

```toml
[sandbox]
mode = "docker"
image = "python:3.12-slim"
network = "none"
memory = "512m"
```

并发验证较多时可开启容器池（`pool_size = 4`）并配合 `pull` 预热镜像；串行场景下池化反而更慢，
默认保持关闭。实测数据见 [`docs/ci.md`](docs/ci.md)。

## 交互式 TUI

`repopilot tui` 打开一个只用标准库实现的交互面板，不新增依赖，Windows 与 Linux 行为一致：

```text
repopilot> propose 修复登录页空邮箱时崩溃的问题
repopilot> diff 3f2a…
repopilot> apply 3f2a…
应用补丁到 app.py? [y/N] y
repopilot> verify 3f2a…
repopilot> review 3f2a…
repopilot> report 3f2a… json
```

可用命令：`help`、`open`、`doctor`、`providers`、`list`、`show`、`diff`、`plan`、`propose`、
`apply`、`undo`、`verify`、`repair`、`review`、`report`、`clear`、`quit`。`apply` 与 `undo`
必须输入 `y` 才会执行，TUI 不会绕过策略层；非 TTY 输出自动关闭 ANSI 颜色，便于脚本和测试。

## 模型与 Provider

RepoPilot 的模型层通过 Provider 插件注册，内置 `deepseek`（默认）、`openai`（任意 OpenAI
兼容端点）和 `anthropic`。用 `repopilot providers` 查看每个 Provider 的默认模型、凭据环境变量
和当前是否就绪：

```powershell
repopilot providers
repopilot --provider anthropic doctor --repo .
```

选择 Provider 有三种方式：配置文件 `[model] provider`、全局参数 `--provider`，或第三方插件。
每个 Provider 的模型和地址可用 `<PROVIDER>_MODEL`、`<PROVIDER>_BASE_URL` 覆盖（例如
`DEEPSEEK_MODEL`、`OPENAI_MODEL`）；凭据默认读取 `<PROVIDER>_API_KEY`，可用
`[model] api_key_env` 指定其他变量名。`openai` 必须显式指定模型，`anthropic` 需要额外安装
`anthropic` SDK。

启动时会通过 `python-dotenv` 加载项目根目录的 `.env`，显式进程环境变量优先。Key 不会写入
配置对象、运行状态或日志。DeepSeek 的细节见 [`docs/deepseek.md`](docs/deepseek.md)。

**第三方插件**：在包中声明 entry point 即可接入，无需修改 RepoPilot 源码。

```toml
[project.entry-points."repopilot.model_providers"]
my-llm = "my_llm.provider:MyProvider"
```

目标可以是 Provider 实例、Provider 类，或无参工厂函数。插件必须实现 `name`、
`default_model`、`default_base_url`、`credential_env`，以及 `normalize`、`requirement`、
`create` 三个方法。无法加载或与内置同名的插件会被记录并在 `repopilot providers` 中提示，
不会劫持内置 Provider。

## 目录

```text
RepoPilot/
├── src/repopilot/
│   ├── cli.py           # CLI 入口
│   ├── tui.py           # 交互式终端面板
│   ├── config.py        # TOML/环境配置
│   ├── contracts.py     # JSON 可序列化领域契约
│   ├── workspace.py     # Git 根与路径安全边界
│   ├── policy.py        # allow/ask/deny 权限策略
│   ├── git.py           # Git 状态与补丁校验/应用
│   ├── llm.py           # 模型抽象与 OpenAI 兼容 / Anthropic 适配器
│   ├── providers.py     # Provider 注册表与 entry point 插件发现
│   ├── planning.py      # P0 Explorer -> Planner 只读流程
│   ├── editing.py       # P1 Editor 与审批生命周期
│   ├── patching.py      # unified diff、安全策略与精确撤销
│   ├── verification.py  # P2 验证白名单、argv 执行与 VERIFY/REPAIR 闭环
│   ├── review.py        # P3 只读审查节点
│   ├── repair.py        # P3 限定范围的单次修复
│   ├── repo_map.py      # P3 符号地图与变更影响面
│   ├── report.py        # P3 JSON/Markdown 运行报告
│   ├── benchmark.py     # P3 离线基准场景与评分
│   ├── persistence.py   # 仓库外 JSON / SQLite 台账
│   ├── runtime.py       # 依赖组装
│   ├── workflow.py      # 状态机骨架
│   └── tools/           # 工具协议与注册表
├── tests/               # P0/P1 离线测试和 fixture
└── docs/                # 架构与阶段路线图
```
