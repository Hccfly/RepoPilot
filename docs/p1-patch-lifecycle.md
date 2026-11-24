# P1 补丁生命周期

P1 的目标是把“模型建议修改”和“真实写入文件”拆成两个明确步骤。生成提案不会写入目标仓库；
只有用户执行 `repopilot apply <run_id>` 时，RepoPilot 才把该命令视为明确审批并应用补丁。

## 命令流程

```powershell
repopilot propose "修复某个问题" --repo D:\path\to\repo
repopilot diff <run_id>
repopilot apply <run_id>
repopilot undo <run_id>
```

`propose` 会执行 P0 的只读探索与规划，再让 Editor 输出 unified diff。RepoPilot 随后检查路径、
文件类型、工作区状态和每个 hunk，但仍不修改文件。`diff` 只读取台账。`apply` 再次检查文件 hash
和完整补丁，任何一个 hunk 冲突都会让整个操作失败。`undo` 只有在应用后的文件没有再次变化时
才会恢复，避免覆盖用户后续编辑。

## 台账与敏感信息

默认台账目录是 `%LOCALAPPDATA%\RepoPilot\runs`，每次运行对应一个原子写入的 JSON 文件。
台账保存任务状态、正向 diff、反向 diff、涉及文件、SHA-256 和撤销所需的原始字节，不写入目标
仓库。`DEEPSEEK_API_KEY` 只从进程环境或 RepoPilot 自身的 `.env` 加载，不进入配置对象、模型
提示词、运行状态、日志或台账。

如需隔离测试数据，可以覆盖目录：

```powershell
repopilot --runs-dir D:\temp\repopilot-runs propose "任务" --repo D:\path\to\repo
# 或
$env:REPOPILOT_RUNS_DIR = "D:\temp\repopilot-runs"
```

`--runs-dir` 也可以写在 P1 子命令之后。

## 安全规则

- 只允许仓库内的相对路径，拒绝绝对路径与 `..` 路径穿越。
- 拒绝 `.env`、私钥、凭据文件和 `.git` 等受保护路径。
- 只支持 UTF-8 文本文件的新增、修改和删除。
- 拒绝二进制补丁、重命名以及旧路径和新路径不一致的跨路径补丁。
- 生成提案时，涉及文件只要已修改或未跟踪，就整体拒绝。
- 生成提案后文件 hash 发生变化时，`apply` 拒绝覆盖。
- 应用后文件 hash 发生变化时，`undo` 拒绝覆盖。

当前 RepoPilot 项目本身如果还没有创建首个提交，所有源码都会被视为未跟踪文件，因此严格策略
会拒绝修改它们。这是设计行为；在干净或已提交的目标仓库中使用 P1 即可。

## P1 边界

P1 应用补丁后进入 `VERIFY` 阶段，但不会自动执行测试、lint、任意 Shell、Git commit 或 push。
验证命令白名单、SQLite checkpoint 与中断恢复属于 P2。
