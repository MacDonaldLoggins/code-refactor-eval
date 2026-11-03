# Behavior Contract

## Observers

- CHANGE_ME：直接调用者或公共 API 消费者。
- CHANGE_ME：CLI、UI、下游服务、数据消费者或运维观察者。

## Must preserve

- CHANGE_ME：返回值、排序、格式和序列化。
- CHANGE_ME：异常类型、错误消息和失败时机。
- CHANGE_ME：副作用、持久状态、幂等性和外部调用。
- CHANGE_ME：属于契约的性能、资源或时序承诺。

## May change

- 私有 helper 名称和内部模块组织。
- 不可观察的中间数据结构。
- CHANGE_ME：本任务明确允许改变的内容。

## Out of scope

- CHANGE_ME：明确排除的平台、模式或非目标行为。

## Unknowns

- CHANGE_ME：当前无法独立证明、存在环境波动或只被间接观察的内容。

## Evidence map

| Claim | Primary evidence | Secondary evidence | Failure action |
|---|---|---|---|
| CHANGE_ME | `tests/oracles/...` | focused regression test | hard gate fail |

