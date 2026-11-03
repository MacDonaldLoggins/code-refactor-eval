# 步骤 5：Codex 金标轨迹分项终检指令

<GOLD_ROOT>=/Users/qijian/Desktop/workspace/20260711修复数据/RepoGen_Trajectory_Fix/gold_tasks/TASK-20260519-C18256

步骤 5 拆分为 5 个可独立粘贴到 Codex 中运行的只读检查指令。每份指令只判定一项规则，并生成一份简短 Markdown 报告，便于人工逐项复核和修复。

运行每份指令前，将 `<GOLD_ROOT>` 替换为包含 `TASK-*/sessions/` 的金标数据根目录，例如 `gold_tasks`。

| 顺序 | 指令 | 只检查 | 输出报告 |
| ---: | --- | --- | --- |
| 1 | [步骤5-1-执行失败修复闭环检查指令.md](步骤5-1-执行失败修复闭环检查指令.md) | 所有执行失败是否诊断、修复、重跑并成功 | `01-execution-failure-closure.md` |
| 2 | [步骤5-2-run-test-Docker启动失败日志检查指令.md](步骤5-2-run-test-Docker启动失败日志检查指令.md) | `run test` 流程是否出现 Docker 无法启动日志 | `02-run-test-docker-startup.md` |
| 3 | [步骤5-3-主轨迹run-test真实成功检查指令.md](步骤5-3-主轨迹run-test真实成功检查指令.md) | 每个 main 是否真实执行 `run test` 并最终成功 | `03-main-run-test-success.md` |
| 4 | [步骤5-4-run-test总结与执行证据一致性检查指令.md](步骤5-4-run-test总结与执行证据一致性检查指令.md) | 每个总结的成败和数量是否与 tool result 一致 | `04-run-test-summary-evidence.md` |
| 5 | [步骤5-5-run-test多总结口径一致性检查指令.md](步骤5-5-run-test多总结口径一致性检查指令.md) | 同一 main 内多个总结的总数、通过数和状态是否统一 | `05-run-test-summary-consistency.md` |

## 统一执行规则

- 5 份指令应分别在独立 Codex 会话中运行，避免上一项的结论影响下一项。
- 指令只读审计 `TASK-*/sessions/**/*.json`，不执行轨迹中的命令，不修改金标 JSON。
- 报告统一写到 `<GOLD_ROOT>/report/step5_reports/`。
- 报告只记录汇总和不通过项，不复制大段轨迹，不另外生成 JSON 报告。
- 每条不通过项必须包含 JSON 路径、`messages[N]` 节点索引、文件物理行号、精简节点内容和简要原因。

## 建议顺序

按 1 到 5 顺序执行。五份报告全部 PASS，才表示步骤 5 通过。任何一份报告为 FAIL 或 ERROR，都应人工修复金标轨迹后重新运行全部 5 项。
