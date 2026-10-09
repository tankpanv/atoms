# 构建全流程的成本与正确性优化

## 研究结论和边界

本次检查实际官方开源源码，不把公开仓库等同于所有内部实现。Anthropic 的 `claude-code` 仓库主要提供发布、问题与扩展内容，未将其完整执行器当作可审计开源代码，也不采用所谓泄露源码。借鉴的是 OpenCode 与 xAI Grok Build 实际公开的工具和上下文处理，以及服务商官方缓存协议。

- [OpenCode compaction](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/compaction.ts) 在较大可回收量时整理旧工具结果，保护最近上下文及 skill。其源码对话轮数保护适合交互终端，我们的单条需求长时间运行不能机械照搬“至少两个用户轮次”规则，否则整次构建从不整理。
- [OpenCode core compaction](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/compaction.ts) 将最近上下文按 token 保留，将任务、已解决问题、约束与下一步作为滚动摘要；按模型窗口扣除输出余量后检查完整请求。
- [OpenCode read](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/tool/read.ts) 为读取提供范围、文件大小与截断提示。我们的实现继续使用已有字符分页协议，并为工具输出增加全文检索。
- [Grok Build 摘要模板](https://github.com/xai-org/grok-build/blob/main/crates/common/xai-grok-compaction/src/code_compaction/templates/full_replace_summary_prompt.txt) 保留用户意图、路径、失败和下一步，并继承前一份摘要。它的“保存所有代码片段”等建议不是照搬目标：源码已经是本项目的事实来源，重复把所有源码搬进摘要反而增加成本。
- [OpenRouter 缓存协议](https://openrouter.ai/docs/guides/best-practices/prompt-caching) 按稳定前缀及同一路由增加命中。缓存是优化，不能代替持久状态。只有真实 usage 字段用于账本与验证。

## 本项目发现的问题

以前规划强制 UNDERSTAND → 多轮 EXPLORE → PLAN，即使没有源码也发送多次请求。自动检索的源码又在规划工具中读取，执行阶段重复检索。任务状态每次更新返回整份需求、架构、任务和近期证据，每四轮再发送完整当前任务。日志直接截取前部，或在更底层丢掉前部；模型不见根因又重新执行命令。旧工具的代码参数及返回内容长期占据窗口，一到阈值就付费总结。复核重复发送需求、全计划、diff、完整源码、任务清单与大量验收日志。

上轮真实小型 CLI 测试合计 78 次计费模型调用，1,190,133 输入 tokens、56,830 输出 tokens、6 次摘要。其中包含首次部署错误与主动停止/恢复，不能把它视为公平的单次 A/B 基线。

## 已实现的流程

### 规划

理解、按需源码探索与计划生成合并为一个只读工具循环。无源码时可直接返回计划，有源码时工具范围保持 list/read/search/symbol/output。仍执行完整的计划结构、需求覆盖、依赖与验收类型校验，不接受分析段落代替计划。失败只反馈具体缺口。

规划对话、已读取观察和完成计划独立持久保存。相同阶段输入可以恢复，不再次固定执行三次模型调用。内容哈希验证后的源码片段交给实现阶段；变化文件显式提示重新读取。历史完整需求和验收保留在项目记忆中，避免再次塞入重复的助手总结。任务按可验收功能组织，不拆成每个文件、文档或每次提交，不添加用户未要求功能。

### 实现与工具

每个任务只有在切换时追加一次执行提示。get_tasks 提供紧凑状态与验证 ID，update_task 只返回更新的任务。工具定义在一次执行中保持稳定；CLI/library 不发送 browser/API/runtime/scaffold 定义和 Web 运行细节，Web 项目保留完整能力。

读取缓存覆盖当前上下文中的已验证交接片段，以及与实际文件一致的自身 write_file 调用。哈希变化、删除或原文已离开上下文时返回真实内容，不拿短引用替代不存在的源码。相同内容的 write_file 不修改文件时间或触发无用写入。修改已有文件建议局部 patch；增加 write_files 工具，预检查全批路径、大小及重复目标后写入 1–3 个相关文件，失败报告已完成路径；不是文件系统事务。相关独立工具也可在同一回复中成批调用，执行仍按顺序提交结果，不并行竞争写入。

长工具输出在私密会话数据库保存全部实际捕获文本，模型看到头部和尾部及 output_id；read_tool_output 支持分页查看中间内容。保留退出码、验证 ID 和错误尾部。命令、HTTP 请求、浏览器操作不做本地结果缓存，也不自动重放副作用。这里的重复节省来自提示词及全文检索，不伪造新的运行结果。

### 分层上下文整理

只有超过活动上下文目标时才考虑轻量整理，保护最近至少两个完整工具组和约 8000 tokens 的近期上下文。旧的长工具结果与已执行写入正文缩为引用，并保留可读取结果。只有可回收量达到至少 2000 tokens 或目标的 10% 才改变历史前缀，避免每一轮破坏缓存。

轻量整理足够时无需模型摘要调用。仍超目标时才生成滚动摘要；保留原始需求、专家 system 指令、架构、完整设计及验收约束、任务状态和近期工具组。摘要继承前一份摘要的未完成要求，并保留失败、路径和下一步。只在摘要阶段请求较低推理强度，执行和独立复核保持所选模型默认行为。上下文长度拒绝时使用强制整理路径，预算仍独立约束累计用量。

变更历史前缀时清除不再可迁移的签名/加密 reasoning 并切换 cache epoch。完整审计历史保持持久化，工具请求和结果仍完整配对，中断不会自动再次执行未知状态的命令。

### 验收与复核

未完成任务时不因为模型提前总结而反复执行全套构建。最终所有任务仍必须通过真实验证 ID、当前源码的需求验收、配置构建/测试与独立代码复核。最终命令仍执行，服务端及浏览器副作用测试不因哈希相同跳过。

复核提供一次架构/设计/验收合同、紧凑任务证据、已有文件 diff、有限完整当前源码和真实最终验证输出；新文件不同时发送 diff 和源码两份副本。未提供源码要求通过只读工具补查，不能因为没看到就批准或猜测。复核工具对话也独立持久保存。复核使用 2 倍执行上下文目标（仍受实际模型窗口限制），避免刚整理就重新取回所需观察；模型只补查直接影响正确性的文件，不重复读取已提供的计划元数据。只读轮数耗尽时强制给出 JSON 结论，证据不足必须拒绝批准，不能把正常只读请求误报为 JSON 格式错误。分页读取直接返回既有结果，不再把它另存成一个新引用，避免引用递归。平台不保证任意需求“完美实现”，实际完成必须由这些证据和最终复核共同支持。

### 服务商缓存与观察

规划、执行、摘要与复核使用独立稳定路由身份。Anthropic 在 system、初始用户消息和滚动末尾设置最多三个 ephemeral cache breakpoints，其他支持隐式缓存的模型由服务商处理。缓存写入、缓存命中、实际服务商成本和平台积分结算分别展示，不声称命中即免费。

session 接口新增 prunes、pruned_tokens（本地估算）及 output_chars_saved。UI 展示轻量整理次数与估计上下文减少量。计费仍只用真实服务商 usage；本地估算绝不替代收费证据。

## 验证方法

自动化测试验证：规划一个工具循环和持久恢复、交接内容哈希变化、读取自身写入不重读、相同写入不改变时间、输出跨进程分页恢复全文、轻量整理减少体积同时保留工具组与历史、任务状态只返回必要信息。已有测试继续覆盖真实文件执行、停止恢复、文档变更不能冒充代码实现、上下文错误类型、模型缓存协议和真实 PostgreSQL 计费事务。

真实联调使用独立测试账号与经过计费代理的真实模型，覆盖新项目构建及后续需求。单次不同模型运行存在随机性，不能把前后样本结果当作保证节省比例。固定相同工具轨迹的请求体积测试和真实计费结果分别记录。


## 本次真实联调记录

结果见 [结构化验证报告](agent-efficiency-verification.json)。所有模型请求经过真实 PostgreSQL 计费代理，最终没有未结算请求。

| 场景 | 请求数 | 输入 tokens | 输出 tokens | 结果 |
| --- | ---: | ---: | ---: | --- |
| 新建 Python CLI | 46 | 627740 | 21221 | COMPLETE |
| CLI 增加 repeat 参数并保留现有功能 | 44 | 800632 | 29782 | COMPLETE；独立执行 65 项 unittest 全通过 |
| 计数器网站首次构建 | 42 | 628199 | 35059 | 构建及浏览器验收通过，但旧复核循环耗尽；记录失败，不算成功 |
| 修正复核后继续该网站 | 11 | 188376 | 5322 | COMPLETE；没有 PLAN 请求，20 项 Vitest 及真实浏览器验证通过 |

另外使用 Playwright MCP 在平台预览 iframe 独立点击：重置 0 → 增加两次 2 → 刷新仍为 2 → 减少 1 → 重置 0。CLI 和网站实际记录了规划观察交接、项目记忆恢复及轻量整理；两个项目累计轻量整理估计回收 9848 / 31894 tokens。这是上下文体积估算，不是可直接相加的收费节省。此次实测重复读取命中计数为 0，不能宣称该样本体现了文件缓存命中收益；读取缓存机制另由哈希失效与恢复测试验证。

发现 CLI 安装 pytest 时产生项目内 .local 依赖目录，导致约 500 个第三方文件混入源码清单。现将 .local/.cache 与已有 node_modules、虚拟环境、构建产物一起排除，避免第三方依赖污染规划清单、源码快照和变化比较；不删除安装内容，实际命令仍能运行。

51 项相关自动化测试覆盖 Agent、会话、上下文/输出分页、复核最终判定、依赖目录排除、缓存协议、账号和真实数据库计费事务；前端生产构建通过。旧验证的 78 请求 / 1190133 输入 tokens 包含失败、停止和恢复，不构成公平同轨迹 A/B，因此不提供承诺性节省百分比。

工具调用截断、嵌套合同校验及有界恢复的后续修复见 [工具合同修复](TOOL_CONTRACT_FIX.md)。

## 2026-10 acceptance-loop optimization

A recovery diagnosis is an implementation directive, not an acceptance checkpoint. The harness must not run source inspection, build, completion-gap analysis, and delivery verification after a read-only diagnostic turn. A cached browser result is also terminal evidence for that source/action fingerprint and must not set the checkpoint flag again. Only a source mutation, a task transition to `done`/`deferred`, or entry into bounded delivery stabilization may schedule the next acceptance pass. This keeps the OpenCode-style loop action-oriented: observe one concrete failure, make one change or discriminating probe, then validate the affected path.
