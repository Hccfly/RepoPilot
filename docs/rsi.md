# 自我改进（RSI）

## 边界

RSI 只做三件事：**生成候选 → 在隔离副本里评测 → 人工审阅 diff**。它不会自动应用任何补丁，
也不会改动测试或评分标准。

## 内置场景

| 场景 | 覆盖的行为 |
| --- | --- |
| `bug_fix` | 标准修复 → 验证通过 → 审查通过 → DONE |
| `review_request_changes` | 审查提出 blocking finding → REPAIR |
| `review_reject` | 审查直接拒绝 → REPAIR |
| `repair_recovery` | 验证失败 → 定向修复 → 再次验证通过 |
| `repair_budget_exhausted` | 修复后仍失败 → FAILED（预算耗尽） |
| `verification_timeout` | 验证失败且不重试 → REPAIR |
| `repair_out_of_scope` | 修复补丁越出已批准范围 → 整体拒绝 |
| `editor_unread_file` | 编辑节点修改未读取文件 → 拒绝 |
| `dirty_worktree_conflict` | 目标文件有用户改动 → 拒绝生成补丁 |

这 9 个场景都离线可复现（脚本化模型 + 桩执行器），跑一轮约十几秒。`rsi eval` 会给出每个场景
的分数；只有存在失败项时，`rsi run` 才有事可做。

## 行为检查

沙箱、审计、配额不在 propose → apply → verify → review 这条链路上，因此用独立的"检查"表达。
检查与场景在同一个候选子进程里执行，同样只上报状态，由受信 grader 判分：

| 检查 | 覆盖的行为 |
| --- | --- |
| `sandbox_timeout_kills` | 验证超时会终止子进程并报告 `timed_out` |
| `sandbox_secret_isolation` | 验证子进程环境不含 `DEEPSEEK_API_KEY` |
| `docker_network_isolation` | Docker 后端 `--network none` 生效（无 docker/镜像时跳过） |
| `audit_hides_token` | 审计落盘但不含明文 token，错误 token 被拒 |
| `quota_window_rollover` | 配额在同一窗口内拦截、滚动后恢复 |

`skip` 表示当前环境不适用（例如没有 Docker），不计入均分。评分器对 `fail` 直接给 0 分，
所以把密钥剥离改成空操作这类回退会被立刻抓到。

## 跑与判分离

- **跑**：`benchmark.run_scenario_facts` 在候选进程里执行场景，只返回原始事实（涉及文件、阶段、
  停止原因、错误），不返回任何分数。
- **判**：`grader.score_facts` 是唯一的打分点，只接受事实；它不导入候选、不做 I/O。
- 评分规则是 `grader.py` 里的常量（无关修改 0.3、缺文件 0.5、阶段 0.5、停止原因 0.25、通过线
  0.75），不放在配置文件里，所以不存在"改配置放宽标准"这条路径。
- 候选补丁不能触碰 `src/repopilot/grader.py` 或 `src/repopilot/benchmark.py`，越界补丁在评测前
  就被拒绝。
- 基线会计算 `grader.fingerprint()`（`grader.py` + `benchmark.py` + 版本号的 sha256）：
  - 候选指纹与基线不一致时，整个候选记为 0 分并标记"候选修改了评测栈"；
  - `--expect-grader <sha>` 可以固定这个指纹，不匹配直接拒绝评测，scorecard 也会记录本次使用的
    指纹，便于跨轮对比。
- 范围守卫只允许候选修改 `src/repopilot/**`，并显式禁止评分实现、测试、文档与项目配置。
- 需要清楚的残余风险：评测**确实会运行候选代码**。一个刻意伪造运行结果的候选无法仅凭评测本身
  识别——最终的兜底仍然是你审阅 diff。

## 命令

```powershell
repopilot rsi eval --repo . [--scenario NAME] [--out artifacts/rsi]
repopilot rsi compare --repo . --patch candidate.diff [--out artifacts/rsi]
repopilot rsi propose --repo . --scenario bug_fix [--out artifacts/rsi]
```

- `eval`：对基线和当前工作区（无补丁）各跑一遍基准。
- `compare`：基线 vs 应用候选补丁后的副本。
- `propose`：让 RepoPilot 针对某个基准场景生成补丁，做范围检查，评测候选，写出
  `candidate.diff` 与 `scorecard.{json,md}`，然后停下等待你 `apply`。

`--scenario` 可重复，用于挑选场景；省略则跑全部内置场景。

## 多轮候选

```powershell
repopilot rsi run --repo . --scenario bug_fix --rounds 3
repopilot rsi history --repo .
```

每轮流程：

1. 生成候选；上一轮的失败原因会拼进目标，并要求"换一个思路"。
2. 范围守卫；越界直接记为 `error` 并停止。
3. 候选补丁的 sha256 与历史比对，重复则记为 `duplicate` 并跳过评测。
4. 基线只评测一次并缓存，候选是在副本上加补丁后评测。
5. 与当前最佳分比较：严格更高记 `improved`，否则 `rejected`。
6. 追加到 JSON Lines 历史（默认 `RepoPilot 数据目录/rsi-<repo>.jsonl`），每轮 diff 写到
   `artifacts/rsi/round-<n>.diff`。

轮次状态：

| 状态 | 含义 |
| --- | --- |
| `improved` | 分数严格高于当前最佳 |
| `rejected` | 未超过当前最佳 |
| `duplicate` | 补丁哈希与历史相同，未评测 |
| `error` | 生成或范围检查失败，循环停止 |

`best_score` 达到 1.0 时提前结束。**整个过程不应用任何补丁**——最后仍然要
`repopilot diff <run_id>` 再 `apply`。

## 输出

```text
# RepoPilot 自我评测

- 基线均分: 1.0
- 候选均分: 0.7
- 结论: 回退

| 场景 | 基线 | 候选 | Δ | 通过 | 原因 |
| --- | --- | --- | --- | --- | --- |
| bug_fix | 1.0 | 0.7 | -0.3 | 否 | 无关修改: extra.py |
```

`Δ` 是候选分减基线分；`候选均分 < 基线均分` 会被判为回退。

## 建议的日常流程

```text
基准失败 -> rsi propose -> 审阅 candidate.diff -> apply -> 跑完整测试 -> 再 rsi eval
```

应用之后请跑一次完整测试套件再提交；RSI 的 scorecard 只覆盖基准场景，不替代测试。
