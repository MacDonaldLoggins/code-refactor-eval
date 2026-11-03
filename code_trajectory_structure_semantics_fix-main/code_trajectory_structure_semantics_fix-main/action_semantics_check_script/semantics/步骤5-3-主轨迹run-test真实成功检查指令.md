# 步骤 5-3：主轨迹 `run test` 真实成功检查指令

<GOLD_ROOT>=/Users/qijian/Desktop/workspace/20260711修复数据/RepoGen_Trajectory_Fix/gold_tasks/TASK-20260519-C18256

将 `<GOLD_ROOT>` 替换为金标数据根目录，然后将以下完整指令粘贴到独立 Codex 会话：

```text
请只执行“每个主轨迹必须真实执行 `run test` 并最终成功”检查。

输入：<GOLD_ROOT>
检查范围：只检查 `<GOLD_ROOT>/TASK-*/sessions/*.json` 主轨迹；不检查 `sessions/<main>/subagents/` 中的子轨迹。忽略 `.meta.json`。
输出：`<GOLD_ROOT>/report/step5_reports/03-main-run-test-success.md`

这是只读审计。不要运行测试/Docker，不要修改金标 JSON。

本项只检查每个 main 是否包含至少一次真实 `run test` 执行，且最后一次 `run test` 明确成功。不要检查多个总结的数量口径一致性。

识别 `run test`：
- assistant 实际调用 Bash/Shell/exec_command 等工具执行 `bash run_tests.sh`、`./run_tests.sh`、`run_tests` 或项目明确的等价全量 run-test 入口。
- 必须有与 tool call ID 配对的 tool result。Read 脚本、assistant 声称“已运行”、粘贴历史日志或 `echo "All tests passed"` 不算真实执行。
- `nl -ba run_tests.sh && bash run_tests.sh` 这类组合命令可以计入，但必须确实执行到脚本且结果可判定。

真实成功必须同时满足：
1. tool result 明确表明命令成功完成，不得有超时、中断或只显示命令尚在运行。
2. 测试总数为非零，通过数与总数一致，failed/error 为 0；或项目标准 run-test 总结明确给出等价的零失败成功证据。
3. 如果 run-test 包含 Docker 流程，Docker/服务必须启动和健康，不得因环境问题跳过。
4. 最后一次 `run test` 必须成功；不得用更早的成功覆盖更晚的失败。

注意 shell pipeline：如命令使用 `bash run_tests.sh | tail ...` 但没有 `pipefail`，不要仅依据 pipeline 返回码判成功；必须从可见 run-test 输出中确认完整零失败总结。

判定：
- PASS：每个 main 都有真实 `run test` 交易，且最后一次明确成功。
- FAIL：任一 main 缺少真实 `run test`，只有口头/文本成功声明，结果不能证明零失败，或最后一次失败。
- ERROR：输入/JSON/tool transaction 损坏导致无法判定。

报告必须简短：
1. 一行总结：`Status: PASS|FAIL|ERROR; Main sessions: N; Findings: N`。
2. PASS 时只写“所有主轨迹均有真实且最终成功的 run test”。
3. FAIL/ERROR 时用表格每个 main 最多一行：`JSON | 节点 | 行号 | 节点内容 | 简要原因`。
4. 有 run-test 时，节点写调用和 result 的 `messages[N]`，行号写关键 result 的物理起始行；完全缺失时节点/行号写 `-`。节点内容最多 120 字符。
5. 不要生成 JSON 报告，不要列出每个通过 main。

完成后只回复总体状态、Findings 数和报告路径。
```
