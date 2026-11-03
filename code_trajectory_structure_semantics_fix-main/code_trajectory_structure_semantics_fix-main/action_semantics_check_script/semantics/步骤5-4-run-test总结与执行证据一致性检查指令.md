# 步骤 5-4：`run test` 总结与执行证据一致性检查指令
<GOLD_ROOT>=/Users/qijian/Desktop/workspace/20260711修复数据/RepoGen_Trajectory_Fix/gold_tasks/TASK-20260519-C18256

将 `<GOLD_ROOT>` 替换为金标数据根目录，然后将以下完整指令粘贴到独立 Codex 会话：

```text
请只执行“每个 `run test` 总结节点与它所总结的真实执行证据是否一致”检查。

输入：<GOLD_ROOT>
检查范围：递归检查 `<GOLD_ROOT>/TASK-*/sessions/**/*.json`，包括 main 和 subagent，忽略 `.meta.json` 和非 session JSON。
输出：`<GOLD_ROOT>/report/step5_reports/04-run-test-summary-evidence.md`

这是只读审计。不要运行测试/Docker，不要修改金标 JSON。

本项只做“单个总结 vs 对应执行证据”比对。不要在本项判定同一 main 的多个总结之间是否口径统一，该项由步骤 5-5 检查。

识别对象：
1. `run test` 执行：Bash/Shell/exec_command 等工具实际执行 `bash run_tests.sh`、`./run_tests.sh`、`run_tests` 或项目明确的等价全量 run-test 入口，并有配对 tool result。
2. `run test` 总结节点：任何 assistant 节点对某次 run-test 结果作出成功/失败、total/pass/fail/error/skipped 数量、Docker/服务状态等总结，包括立即总结和 final assistant。

对每个总结节点，定位它明确指向的执行；如无明确指向，使用同一 agent scope 中最近一次在它之前完成的 run-test 交易。不得用其他 session/subagent 的结果自动支撑当前总结。

必须逐项一致：
- summary 声称成功，tool result 必须真实成功；声称失败也必须与输出一致。
- total/passed/failed/errors/skipped/suites/packages/services 等任何数字都必须与对应 tool result 一致，不得自行累加、舍入、省略失败或引用其他运行数字。
- summary 声称 Docker/服务启动健康，result 必须有相应成功证据；如 result 显示 Docker 未启动或测试未运行，不得总结为通过。
- tool result 没有数量时，summary 不得凭空增加精确数量。
- Read 脚本内容、echo 文本或 assistant 之前的口头说明不是证据。

判定：
- PASS：所有 run-test 总结节点均能定位到真实执行，且状态、数字和 Docker/服务声明全部一致。
- FAIL：任一总结无执行证据、成败反转、数量不符、凭空添加数字或 Docker/服务声明不符。
- ERROR：输入/JSON/tool transaction 损坏导致无法比对。没有总结节点时不在本项判 FAIL，但总结中写 `Summary nodes: 0`。

报告必须简短：
1. 一行总结：`Status: PASS|FAIL|ERROR; Files: N; Summary nodes: N; Findings: N`。
2. PASS 时只写“所有 run test 总结均与对应执行证据一致”。
3. FAIL/ERROR 时用表格每个不一致总结一行：`JSON | 节点 | 行号 | 节点内容 | 简要原因`。
4. 节点并列总结 `messages[N]` 和证据 `messages[M]`；行号写总结节点物理起始行，原因中简短写明“总结值 vs 证据值”。节点内容最多 120 字符。
5. 不要生成 JSON 报告，不要列出通过的每个总结。

完成后只回复总体状态、Findings 数和报告路径。
```
