# 模型请求恢复与计费对账修复

## 故障记录

项目 `a701b74d87b34c46a61362e188c16fe4` 在 2026-10-04 15:00:28 开始的一次 IMPLEMENT 请求中，上游 InferenceNet 返回 SSE 502 `provider_unavailable`。此前的 COMPACT 请求也收到 Sail Research 的 SSE 502。失败响应没有完整 usage；平台把请求保留为 unknown 等待对账。Worker 重试沿用原计费请求编号，网关拒绝重复生成并返回 409，最终中断构建。

查询上游 generation API 实际得到两次调用的真实 token 用量，finish_reason / native_finish_reason 均为空、cancelled 为 false。原对账代码只接受 finish_reason 或 cancelled 存在的结果，因此即使已有真实用量也无法结算。

## 修复

- 明确 SSE / JSON 上游错误使用类型化错误，丢弃所有不完整工具调用；可重试的上游失败最多尝试三次。
- 数据库独立记录 attempt_finished / retry_safe，区分已明确终止和结果尚不确定的调用。
- 网关通过结构化 MODEL_ATTEMPT_FAILED 回报已终止的可重试失败；Worker 只有收到可信确认才续建新请求编号，并持久保存编号。旧失败请求继续独立按真实用量对账。
- 网络断开不能凭客户端超时续建新编号；MODEL_REQUEST_PENDING 使用原编号等待 15 / 30 秒后查询。上游 generation 明确完成后才允许重新尝试。等待有界，无法确认时保留项目和上下文。
- 完整模型响应先写数据库，再结算。用量暂缺不会丢弃回答；重复请求复用已保存响应，不再次调用上游。
- 对已明确终止的请求，真实 token 计数有效时即使 finish_reason 为空也可结算。未知状态仍不能估算费用或盲目释放冻结额度。
- 对账入口支持指定 request_ids，集成测试和定向修复只处理明确指定的请求。

## 验证与账单修正

35 项针对性回归覆盖流式错误、请求编号恢复、部分工具调用丢弃、网络断开保护、真实 PostgreSQL 冻结/结算、响应复用和恢复后只扣费一次。供应商边界使用错误注入；资金记录、事务和幂等校验使用真实 PostgreSQL。

首次回归测试遗漏了对账范围限制，影响四条历史请求账单。已逐条查询上游 generation API 的真实用量，通过追加 billing_correction 审计流水修正余额，并更新对应请求用量；没有删除原审计记录。后续测试已限制到自建测试请求。目标项目 55 条请求均已结算；项目源码、会话和已有 dist 仍保留。没有自动重新提交客户的构建任务。

参考：[OpenRouter 流式错误说明](https://openrouter.ai/docs/api_reference/errors-and-debugging)、[generation 用量查询](https://openrouter.ai/docs/api/api-reference/generations/get-request-&-usage-metadata-for-a-generation)。
