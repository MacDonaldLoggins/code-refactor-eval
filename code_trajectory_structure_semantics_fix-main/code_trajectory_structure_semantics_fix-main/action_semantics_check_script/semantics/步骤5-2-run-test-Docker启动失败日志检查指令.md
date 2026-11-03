# 步骤 5-2：`run test` Docker 启动失败日志检查指令

将 `<GOLD_ROOT>` 替换为金标数据根目录，然后将以下完整指令粘贴到独立 Codex 会话：

```text
请只执行“`run test` 流程中 Docker 无法启动日志”检查。

输入：<GOLD_ROOT>
检查范围：递归检查 `<GOLD_ROOT>/TASK-*/sessions/**/*.json`，包括 main 和 subagent，忽略 `.meta.json` 和非 session JSON。
输出：`<GOLD_ROOT>/report/step5_reports/02-run-test-docker-startup.md`

这是只读审计。不要运行 Docker/测试，不要修改金标 JSON。

本项只检查：任何 `run test` 实际执行交易的 tool result 中，是否出现 Docker 无法启动、无法连接、无法健康运行的日志。不要检查普通测试失败、总结数量一致性或失败修复闭环。

`run test` 执行交易是指 assistant 实际调用 Bash/Shell/exec_command 等工具执行项目全量测试入口，例如 `bash run_tests.sh`、`./run_tests.sh`、`run_tests`，或项目明确的等价 run-test 入口。仅 Read 脚本、assistant 复述或 echo 伪造成功不算执行。

以下任一信号出现在 `run test` 的 tool result 中即判为一条不通过：
- cannot connect to the Docker daemon / Docker daemon is not running / Is the docker daemon running
- failed to start/create container，container exited/restarting/dead
- service unhealthy，healthcheck failed/timed out，dependency failed to start
- connection refused/no route to host 且上下文是 Docker/Compose 服务未启动
- docker compose up/build/pull 失败导致测试流程未启动
- 因 Docker 环境不可用而 skip/abort/cannot run tests

这项是严格“日志零出现”规则：即使轨迹后面修复 Docker 并成功，只要任意 `run test` tool result 中出现上述无法启动日志，本项仍判 FAIL。

不要因为轨迹在讨论、读取或修改 Dockerfile/Compose 时出现这些文字就报错；必须来自真实 `run test` 执行的配对 tool result。

判定：
- PASS：所有受检 `run test` tool result 均未出现 Docker 启动/健康失败日志。
- FAIL：任一受检 result 出现上述日志。
- ERROR：输入/JSON 损坏或 tool call/result 无法配对。没有找到 `run test` 不在本项判 FAIL，但要在总结中写 `Run-test transactions: 0`，由步骤 5-3 判定缺失。

报告必须简短：
1. 一行总结：`Status: PASS|FAIL|ERROR; Files: N; Run-test transactions: N; Findings: N`。
2. PASS 时只写“未发现 run test Docker 启动失败日志”。
3. FAIL/ERROR 时用表格每项一行：`JSON | 节点 | 行号 | 节点内容 | 简要原因`。
4. 节点写对应 `messages[N]` tool result（可附调用节点）；行号是物理起始行；节点内容只摘录 Docker 失败关键日志，最多 120 字符。
5. 不要生成 JSON 报告，不要列出每个通过 session。

完成后只回复总体状态、Findings 数和报告路径。
```
