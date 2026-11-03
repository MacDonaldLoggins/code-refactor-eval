# 步骤 5-5：`run test` 多总结口径一致性检查指令
<GOLD_ROOT>=/Users/qijian/Desktop/workspace/20260711修复数据/RepoGen_Trajectory_Fix/gold_tasks/TASK-20260519-C18256

将 `<GOLD_ROOT>` 替换为金标数据根目录，然后将以下完整指令粘贴到独立 Codex 会话：

```text
请只执行“同一主轨迹内多个 `run test` 总结节点的口径是否一致”检查。

输入：<GOLD_ROOT>
检查范围：只检查 `<GOLD_ROOT>/TASK-*/sessions/*.json` 主轨迹；不检查 subagent。忽略 `.meta.json`。
输出：`<GOLD_ROOT>/report/step5_reports/05-run-test-summary-consistency.md`

这是只读审计。不要运行测试/Docker，不要修改金标 JSON。

本项只比较“同一 main 内的多个 run-test 总结节点之间”。不要在本项重新判定总结是否与 tool result 一致，该项由步骤 5-4 检查。

识别 run-test 总结节点：任何 assistant 节点对 `run_tests.sh`/run-test 流程作出总体成功/失败判断，或声明 total/passed/failed/errors/skipped/suites/packages/services 数量，包括立即总结、后续回顾和 final assistant。不把 tool result 本身当“总结节点”。

严格口径规则：
1. 同一 main 内所有 run-test 总结节点的最终状态必须一致，不允许一处说成功、另一处说失败/未验证。
2. 同一类计数必须完全一致：例如一处说 12 项 test 全部通过，另一处说 14 项全部通过，必须判 FAIL，即使两处都声称成功。
3. total、passed、failed、errors、skipped、test suites、packages、Docker services 等指标分别对齐；同一指标不得在不同总结中变化。
4. 即使轨迹真实执行了多次且测试集变化，本项仍按金标“总结口径唯一”要求严格判定；不同数量必须 FAIL，交由人工修复轨迹表述。
5. 某总结没有提及一项指标，不把“未提及”自动当数值冲突；但它若使用“全部”、“无失败”等状态语义，该语义必须与其他总结一致。

判定：
- PASS：每个 main 内的所有 run-test 总结在状态和所有重叠指标上完全一致。仅有 0 或 1 个总结节点的 main 在本项没有跨节点冲突，可 PASS，但报告汇总要记录数量。
- FAIL：任一 main 内的总结状态冲突，或任一重叠数量指标不同。
- ERROR：输入/JSON 损坏导致无法提取总结。

报告必须简短：
1. 一行总结：`Status: PASS|FAIL|ERROR; Main sessions: N; Summary nodes: N; Findings: N`。
2. PASS 时只写“未发现同一主轨迹内的 run test 总结口径冲突”。
3. FAIL/ERROR 时用表格每组冲突一行：`JSON | 节点 | 行号 | 节点内容 | 简要原因`。
4. 节点并列冲突的 `messages[N]` 和 `messages[M]`；行号并列它们的物理起始行；节点内容用简短形式写冲突值，例如 `messages[40]: 12/12 passed; messages[58]: 14/14 passed`；最多 120 字符。
5. 不要生成 JSON 报告，不要列出无冲突的 main。

完成后只回复总体状态、Findings 数和报告路径。
```
