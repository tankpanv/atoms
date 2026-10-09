# 工具调用错误根因与修复

对应项目：a4cac944-d3e9-435c-8ef3-431e23c7ad6d。结构化证据见 [回放及验证记录](tool-contract-verification.json)。

## 实际根因

数据库中的实际模型响应与截图一致，并非工作区权限或网络失败：

1. 请求 78a7a085-29ac-4fcb-9690-e58ca2cbaa82 输出 12000 tokens，包含 2511 reasoning tokens，write_files 参数长 25638 字符，在 content 字符串中间结束。服务商仍返回 finish_reason=tool_calls。旧网关只对没有工具的 length 响应作处理，没有将输出状态和用量交给执行边界，直接 json.loads 导致 Unterminated string。
2. 请求 e88e1452-0f48-41d8-815f-b6d2c34bc085 同样输出 12000 tokens，write_files 参数只有 {}。响应记录已经缺少 files，属于服务商输出/转换结果，并非本地参数被清空。旧实现直接 args['files']，报 KeyError。
3. 请求 4edffe1c-261a-4907-adf2-48c7c50c8769 是合法 JSON，但 files[2] 只有 content，没有 path。旧批量函数逐项预检虽会在写入前失败，却只给出 'path'，无法指导修正。截图中的 files/path 缺失、JSON 截断均不能靠放宽路径权限解决。
4. 另有 apply_patch 使用独立 @@ 或过时行号。原接口只接受有数字的 unified diff，描述与常见模型补丁习惯不一致；旧行号与当前实际内容不一致也会直接拒绝。

服务商的 tool_calls 标签和 required schema 不能视为执行保证。[OpenRouter 工具协议](https://openrouter.ai/docs/guides/features/tool-calling) 将参数交给应用执行；[结构化输出官方说明](https://github.com/OpenRouterTeam/docs/blob/main/guides/features/structured-outputs.mdx) 也说明不同服务商的严格模式执行保证不同。必须在应用自己的边界校验。

## 修复

- ModelGateway 保留 finish_reason、实际 completion_tokens 与请求 max_tokens 到内部响应元数据；不把这些内部字段发送回服务商历史。
- 规划、实现、复核三个工具入口共用参数合同校验：当前阶段允许的工具、JSON 对象、required 字段、嵌套数组/对象、声明类型、enum、数量与数字范围。校验在任何 handler 或文件副作用之前进行。
- length 响应禁止执行工具；JSON 或 schema 不完整且用满输出预算时识别 OUTPUT_TRUNCATED，其他错误区分 INVALID_JSON/MISSING_ARGUMENT/INVALID_ARGUMENTS/TOOL_NOT_ALLOWED。不拼接残缺字符串，不猜路径，不用默认空字段执行。
- 错误结果明确 executed=false，并指明具体字段如 arguments.files[2].path；UI 步骤标记“工具参数需修正”，日志保留原参数，私密审计仍保存完整响应。
- 批量写入改为 1–3 个小文件，描述与提示词建议总代码正文不超过 12000 字符，给 JSON 转义及 reasoning 留余量。大文件单独写入，已知局部改动使用 replace_in_file。**字符建议不是精确 tokenizer，也不是模型生成永不截断的保证。**
- 缺字段的 batch 在预检时整批拒绝；每项 path/content 的类型及路径、重复路径、文件大小都在写入前校验。运行时 I/O 故障仍可能导致部分文件已写入，已有结果会指明已成功项，恢复前先检查；不宣称批量写入是原子事务。
- 无效调用返回后正常进入下一轮，让模型只修复未执行的调用。连续三轮所有调用都不合合同则保存进度并停止，避免无限自动计费；成功调用打断该连续计数。真正的命令结果及已成功修改不会被当成未执行。
- apply_patch 同时接受有效 unified diff 与独立 @@ 精确上下文格式。旧行号失效时只允许唯一、逐字相同的上下文定位，重复/缺失上下文拒绝，不做模糊匹配。所有 hunks 验证后才写入，后一个失败不留下前一个的修改。

## 验证及部署

43 项针对 Agent、会话、工具合同、流式解析与上下文流程的测试通过。新增测试重现了截断 JSON/错误 finish 标签、完整 length 命令禁止执行、嵌套必填/类型检查、阶段工具限制、批量后项错误无写入、唯一补丁重定位、多 hunk 无部分写入、真实文件执行后的自动恢复以及连续错误停止。

只读回放原项目实际响应，新边界准确识别两次截断与第三项 path 缺失；不重放历史写入和命令。历史合法六文件批次在新三文件策略下也会被拒绝，需拆批，这与坏参数分类不同。

原作业通过 stop_job 正常保存检查点并停止（停止时无未结算请求），更新后端和 worker 镜像后创建恢复作业 47f0bce2-2c89-4b5b-b135-0ae6a4ddb1c4。实际 worker 已切换新镜像，恢复 27 条活动上下文、40 条操作记录及原始 9 个任务（中断的工具边界已修复）；原需求和专家继续保留，源码没有重新初始化。

补充真实联调：Playwright MCP 注册独立账号并创建项目 bfc26492-c2bf-4cfc-8195-c1487cb2cae0，经过真实模型/计费代理的 18 次请求后 COMPLETE，write_files 分别成功写入三文件和单文件批次，4 项真实 unittest 通过，独立代码复核通过，工具错误为 0、未结算请求为 0。原用户项目恢复作业仍在继续实现，当前不能声称该完整网站已验收完成。

后续审计发现三文件硬限制和参数越界拒绝过于生硬。最新策略已改为完整批次最多12文件、超时和页大小安全调整、按工具恢复提示，见 [工具策略一致性修复](TOOL_POLICY_FIX.md)。
