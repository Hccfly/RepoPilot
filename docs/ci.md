# CI 集成

RepoPilot 的 CI 模式有三个子命令，全部**无交互**、不读取 stdin、不修改工作区：

| 命令 | 作用 | 产物 |
| --- | --- | --- |
| `repopilot ci plan "<目标>"` | 只读探索并生成计划 | `plan.json` |
| `repopilot ci propose "<目标>"` | 生成计划与待审查补丁 | `patch.diff`、`report.json`、`report.md` |
| `repopilot ci verify <run_id>` | 在干净环境中重跑白名单验证 | `report.json`、`report.md` |

退出码固定为三种：`0` 成功；`1` 任务级失败（规划失败、验证未通过）；`2` 环境或参数错误
（缺少密钥、仓库不可用、台账损坏等）。

`ci propose` **不会应用任何改动**，只把 diff 和报告写成产物，等待人工审查。这是刻意的：CI
不拥有审批权。若要在流水线中完成验证，先在有审批权的环境里 `apply`，再在 CI 里跑
`ci verify`。

## GitHub Actions 示例

把下面内容保存为 `.github/workflows/repopilot.yml`。触发方式刻意选了手动，避免每次 push
都消耗模型额度：

```yaml
name: RepoPilot 提案

on:
  workflow_dispatch:
    inputs:
      goal:
        description: 任务目标
        required: true

jobs:
  propose:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e ".[agent]"
      - name: 生成提案
        run: repopilot ci propose "${{ inputs.goal }}" --repo . --out-dir artifacts
        env:
          DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: repopilot-proposal
          path: artifacts/
```

任务失败时 `if: always()` 仍会上传 `report.md` 和 `plan.json`，便于排查。

## 远程沙箱

默认在本地执行验证。若要把验证放进隔离环境，配置一个固定的 argv 前缀：

```toml
[sandbox]
mode = "command"
command = [
  "docker", "run", "--rm", "--network", "none",
  "-v", "{workspace}:/work", "-w", "/work",
  "python:3.12-slim",
]
```

规则：

- `command` 是 **argv 数组，不是 shell 字符串**；`{workspace}` 会作为一个完整参数替换成本地
  仓库路径。
- 动作本身的 argv 只能**追加**在前缀之后，不能被前缀改写。
- 前缀模式下列表里的 `python` 动作使用 `python` 而不是本机解释器路径，因此镜像里必须提供
  `python`、`pytest`（以及可选的 `ruff`）。
- `--network none` 这类隔离由前缀负责，RepoPilot 不会额外放行网络。

`sandbox.mode` 只接受 `local`、`command` 和 `remote`；写错会在启动验证时直接报错，而不是静默回退。

## Docker 容器后端

把验证放进一次性容器，每个动作一个 `docker run --rm`，并发动作互不干扰：

```toml
[sandbox]
mode = "docker"
image = "python:3.12-slim"
network = "none"
memory = "512m"
cpus = "1.0"
user = ""            # 留空使用镜像默认用户
```

规则：

- 工作区挂载到 `/work`，容器工作目录也是 `/work`；动作 argv 只能追加在镜像名之后。
- `image`、`network`、`memory`、`cpus`、`user` 都以独立参数传给 docker，且不能以 `-` 开头，
  避免配置值被当成额外 flag（例如把 `image` 写成 `--privileged` 会被直接拒绝）。
- 默认 `--network none`。需要联网时显式修改，但要意识到那等于放开测试代码的出网能力。
- 超时会对对应容器执行 `docker kill`，配合 `--rm` 清理，不会留下孤儿容器。
- 镜像需要自带 `python` 与 `pytest`（可选 `ruff`）。RepoPilot **不会自动 pull**，请提前
  `docker pull`。
- 容器内以镜像默认用户运行；需要固定 uid 时设置 `user`。

### 容器池与镜像预热

`pool_size = 0`（默认）时每个动作一个 `docker run --rm`。设置 `pool_size > 0` 会启用容器池：
后台线程提前 `docker create` + `start` 一批空闲容器，动作通过 `docker exec` 执行，结束后容器在
后台被销毁并补充新的。

**每个动作仍然独占一个容器**，所以 `/work` 之外的写入不会串到下一个动作。

```toml
[sandbox]
mode = "docker"
image = "python:3.12-slim"
pool_size = 4
pull = "missing"   # never / missing / always
```

本机实测（`python:3.12-slim`）：

| 场景 | 每动作一个容器 | 容器池 |
| --- | --- | --- |
| 4 个并行动作（每个 sleep 1s） | 2.14s | 1.44s |
| 6 个串行动作 | 5.34s（0.89s/动作） | 8.82s（1.47s/动作） |

结论：**只有并发执行验证动作时才有明显收益**。串行时容器池反而更慢，因为销毁加创建的绝对开销
和 `docker run --rm` 相当，只是搬到了后台线程。因此默认保持 `pool_size = 0`；只有在
`max_parallel_verifications > 1` 且动作较多时才建议开启。

`pull` 控制镜像预热：`never` 完全不检查、`missing`（默认）缺失时执行一次 `docker pull`、
`always` 每次拉取。拉取失败会直接报错，不会静默继续。

## 审计与配额

服务默认把每次请求写入 SQLite 审计日志（默认位于 RepoPilot 数据目录的 `sandbox.db`，可用
`--audit-db` 或环境变量 `REPOPILOT_SANDBOX_DB` 覆盖）。每条记录包含时间、租户、动作名、成功
与否、错误信息、退出码、耗时和客户端地址；**不记录 token，也不记录命令输出**。

```powershell
repopilot sandbox audit --limit 20
repopilot sandbox audit --db D:\srv\audit.db --limit 5
```

配额是固定窗口计数，按租户持久化在同一个 SQLite 文件里：

```powershell
repopilot sandbox serve --repo . --quota 100 --quota-window 3600
repopilot sandbox quota --window 3600
```

- 超限时返回 `ok: false` 与 `quota_exceeded: 已用 N/M，<时间> 重置`。
- 计数按时间窗口对齐，服务重启后继续生效。
- 被拒绝的请求（未授权、未知租户、超配额）**不消耗**配额，但都会写入审计。
- `--no-audit` 关闭审计；由于配额计数依赖同一个 SQLite 文件，关闭审计同时关闭配额。
- 审计写入失败只打印到 stderr，不改变已经执行的结果；配额写入失败则会拒绝请求（fail-closed）。

## 独立沙箱服务

`command` 模式仍然把执行交给本机的一个包装命令。若要把验证放到另一台机器或另一层隔离里，
可以用独立服务：服务端在自己的仓库副本上执行白名单动作，客户端只通过网络请求动作名。

启动服务端：

```powershell
$env:REPOPILOT_SANDBOX_TOKEN = "一个足够长的随机串"
repopilot sandbox serve --repo D:\srv\checkout --host 127.0.0.1 --port 8765
```

客户端配置：

```toml
[sandbox]
mode = "remote"
host = "127.0.0.1"
port = 8765
token_env = "REPOPILOT_SANDBOX_TOKEN"
tenant = "default"
tls = false
ca_file = ""
```

### 多租户

一个服务进程可以同时服务多个仓库。每个租户有自己的工作区、白名单、token 和并发上限：

```powershell
repopilot sandbox serve `
  --tenant team-a=D:\srv\a `
  --tenant team-b=D:\srv\b `
  --host 0.0.0.0 --port 8765 --max-concurrency 8
```

- 每个租户的并发默认是 1，超出后请求在队列里等待，等待超时返回 `busy`；
  `--max-concurrency` 限制所有租户的总并发。
- 租户 token 优先读 `REPOPILOT_SANDBOX_TOKEN_<NAME>`（名称大写），`default` 租户回退到
  `REPOPILOT_SANDBOX_TOKEN`。
- 客户端用 `tenant = "team-a"` 选择租户；未知租户直接拒绝。
- 非回环地址上，每个租户都必须配置 token，否则服务拒绝启动。

### TLS

服务端用 `--cert` / `--key` 提供证书链与私钥，最低 TLS 1.2：

```powershell
repopilot sandbox serve --repo D:\srv\checkout --host 0.0.0.0 --port 8765 `
  --cert D:\certs\server.pem --key D:\certs\server-key.pem
```

客户端打开 `tls = true`，并用 `ca_file` 指定签发该证书的 CA（留空则使用系统信任库）：

```toml
[sandbox]
mode = "remote"
host = "sandbox.internal"
port = 8765
tenant = "default"
tls = true
ca_file = "D:/certs/ca.pem"
```

TLS 启动后，明文客户端会在握手阶段被拒绝；客户端如果无法验证服务端证书，会直接报
`SandboxError` 而不是降级。

### 协议

一条连接一个请求，换行分隔的 UTF-8 JSON：

```json
{"action": "pytest", "token": "...", "timeout_seconds": 120}
```

响应：

```json
{"ok": true, "exit_code": 0, "stdout": "", "stderr": "", "timed_out": false,
 "truncated": false, "duration_seconds": 0.42, "action": "pytest", "command": ["..."]}
```

失败时返回 `{"ok": false, "error": "..."}`。

### 安全模型

- **协议只传动作名**。服务端用自己的白名单把名字解析成 argv；客户端附带的 `argv` 字段会被
  完全忽略，因此无法通过协议执行任意命令。
- 服务端限制单条请求 64 KiB、单条响应 1 MiB，并把客户端给的超时钳制到服务端上限。
- `token` 用 `hmac.compare_digest` 比较；监听非回环地址而没有配置 token 时，服务直接拒绝启动。
- **默认是明文 TCP**。跨网络部署请启用 TLS（见下）或走 SSH 隧道，不要直接暴露到公网。
- 服务端以运行它的用户身份、在 `--repo` 目录下执行动作；真正的文件系统/网络/资源隔离仍由部署
  方式决定（容器、虚拟机或专用主机），协议本身不提供这些。

## 注意事项

- 密钥只通过环境变量传入，CI 里请用 secrets，不要写进配置或产物。
- 台账默认在仓库之外（`%LOCALAPPDATA%` / `$XDG` 或 `REPOPILOT_RUNS_DIR`）。CI 中若需要跨步骤
  复用，请显式设置 `--runs-dir` 指向缓存目录，并把它作为 artifact 传递。
- `ci verify` 依赖台账中存在对应的 run；纯 CI 环境需要先导入台账或重新 `propose`。
- RepoPilot 永不执行 `git push`，`commit` 也默认被策略拒绝。
