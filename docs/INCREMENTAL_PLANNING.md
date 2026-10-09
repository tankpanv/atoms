# 已有项目追加功能的增量规划

## 本次失败的根因

项目 `a701b74d87b34c46a61362e188c16fe4` 的任务 `7a397965-1c0d-4193-b0fd-bda0c6a2b0f0`，要求“增加注册登录完整功能”。数据库记录显示规划调用 10 次，`read_file` 44 次，涉及 20 个不同文件，累计 208,936 tokens，其中 121,600 tokens 命中上游前缀缓存。没有提交最终计划，也没有计划结构校验失败的记录。

原有代码只对“继续”复用原任务计划。新需求进入通用完整规划循环，虽然提供了项目记忆，但未将已有架构、命令和新增任务分离。规划最多 10 轮，工具读取也消耗该次数，未为计划输出保留轮次。`phase_window` 只留下旧工具结果前 300 字符，通常仅剩导入和文件头；模型再次读取相同源码，最后读取循环耗尽被错误描述成“规划连续失败”。

真实复测另外发现输出预算耗尽：推理与可见计划共用 `max_tokens`，模型可能只返回推理或截断的 JSON。旧解析器尝试每个 `{`，会把损坏外层计划中的有效内层对象作为计划，误报“缺少目标”。这两类问题需要分别处理，增加总轮数无法解决。

## 参考实现

- [MetaGPT 的增量计划 Action](https://github.com/FoundationAgents/MetaGPT/blob/main/metagpt/actions/write_code_plan_and_change_an.py)：将新增需求、已有 PRD、设计、任务、旧代码形成结构化 Development Plan / Incremental Change，再交给代码实现。其部分增量实现确实加载全部源码；这里采用变更计划和旧功能兼容思路，没有照搬全源码拼接。
- [OpenCode 的会话压缩](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/compaction.ts)：在持久会话中修剪旧工具输出，并按预算保护最近的有效对话尾部。不能把压缩当成删除工作记忆后重新探索。
- [Claude Code 工作方式](https://code.claude.com/docs/en/how-claude-code-works)：由模型根据当前需求选择搜索、读取、修改和验证工具，在结果反馈中调整下一步；项目指令、会话和记忆提供连续性。这里参考官方文档，没有声称读取其闭源内部实现。
- [OpenCode Glob](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/tool/glob.ts)、[Grep](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/tool/grep.ts)、[Read](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/tool/read.ts)：分别找文件名、检索匹配行、分页读源码，而非每次将全项目正文放进模型上下文。
- [OpenRouter 推理预算文档](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)：推理通常计入输出预算，需留下可见输出容量。本次公开模型 API 查询确认 `deepseek/deepseek-v4.1-flash` 默认 high，支持 low，并支持 reasoning 参数。

## 实现约束

1. 有源码且存在通过校验的旧计划时进入增量模式；继续执行原任务的既有恢复路径不变。没有可靠基线时按新项目完整规划。
2. 旧 architecture / commands / application_type 可直接继承。当前 requirements / tasks / design 必须由模型生成并通过完整校验。用户明确修改架构时允许显式替换，绝不强行锁住旧栈。继承的 bootstrap 清空，避免重做已有项目初始化。
3. 旧需求保留为 `preserve_requirements`，不重新变成全部 pending 的开发任务。计划须针对变更解释数据迁移、接口兼容及核心功能回归；注册登录依然要求真实服务端认证和用户数据隔离。
4. `code_locator.py` 将路径、哈希、符号、静态路由、表名和引用保存在项目会话 SQLite。冷索引需在本地读取文件；后续只检查 size/mtime_ns/ctime_ns/inode，复用未变化文件的事实和哈希。不向模型发送全部源码，模块概览最多 4,500 字符。开发版本快照和验收证据仍在本地读取源码，以保持版本和验证一致性；这是本地 I/O，不是模型全文阅读。
5. 默认不注入上次任意读取过的源码。当前 prompt 明确包含文件路径时，才复用对应哈希仍一致的最多四个区间，合计不超过 8,000 字符；否则模型通过定位工具选择范围。变化/删除文件失效，规划观察和改动位置交给实现阶段时再次核验哈希。文本读取保留实际换行，确保 CRLF 文件与快照的版本哈希一致。
6. 增量规划最多 3 轮补充探索，新项目最多 6 轮；另预留 3 轮计划输出/校验修复。出现格式问题立即停止工具探索，专门修复计划。用户再次尝试时保留观察、进入有界输出修复，避免永久卡在耗尽的计数上。
7. 增量规划和最终计划生成在 OpenRouter 使用 low 推理强度；不改变代码实现模型的推理设置。发现 `finish_reason=length` 时拒绝截断计划，并将后续输出预算有界提高到 16,000，不额外增加规划轮次。
8. JSON 解析从外层对象进行，不从损坏对象中挑选内层对象；可以无损解析字符串内的换行，缺失结构仍必须修复。错误包含语法位置或字段校验原因，并持久保存响应结束原因。
9. 压缩旧观察时保留首尾及可分页读取的完整输出 ID。跨新增需求保留项目会话身份、旧摘要和最近操作，同时重置当前任务状态、预算与计费请求 ID，不把旧任务结论冒充新增任务的验证证据。

## 新需求的定位与实现链路

模型在同一个规划循环中解释新 prompt 的能力、限制和可观察验收，提取界面文案、API、实体、英文符号和同义词。没有额外付费的需求分类调用，也没有按“注册”等固定关键词猜测文件名的规则。

- `locate_change(queries, include, limit)` 查询本地事实，返回真实路径、符号行号、路由、表名、imports/imported_by。Python 使用 AST；JS/TS 使用明确标记的词法提取。静态引用不包含所有动态调用或跨 HTTP 接口关系，必须继续搜索接口消费者。
- `glob_files(pattern, offset, limit)` 返回文件名；`**` 可跨零个或多个目录，普通 `*` 不跨目录。
- `search_code(query, include, regex, offset, limit)` 使用 rg argv 执行真实检索，限定项目文件、4 秒期限并分页输出匹配行。无命中必须更换关键词/范围，不能当作功能不存在的证据。
- `read_code(path, symbol/start_line, max_lines)` 读取具体符号或行区间，最多 160 行、12,000 字符；附版本和字符偏移，复用既有哈希读取缓存。源码正文只按需进入模型上下文。

增量计划必须生成 `change_map`，每项包含具体 `path`、`operation`、`reason`、`requirement_ids`。执行器拒绝不存在的 modify/delete、已存在的 create、目录/越界/私密文件路径、遗漏需求和与 tasks.files 不一致的清单，并附当前哈希及静态引用。无效计划在已预留的输出修复预算内纠正。它是可扩展的起始范围，不禁止开发中根据真实证据增加必要文件。

同一改动清单、已观察源码及已有架构交给实现阶段；哈希变化的内容要求重新读。实现优先按符号/行读取和局部修改，追踪真实数据、接口和调用方；验收继续使用实际构建、服务启动、API 和浏览器核心用户流程，不增加全文件代码审查，不用历史成功记录代替新功能验证。

## 验证范围

回归测试覆盖：哈希失效、显式变更架构、继承命令、保留旧需求、继续恢复、读取循环进入输出阶段、格式修复不再探索、截断和推理预算、完整观察归档与外层 JSON 解析。新增检查进入镜像构建测试。

真实模型复测使用原项目源码和会话数据库的隔离副本、独立测试账号和运行中计费任务，经正式模型网关发起请求，每次调用按真实 usage 结算。只测试规划链路，不向原项目追加任务、不修改原项目业务源码，不把计划通过当作注册功能已经实现。结果见 `backend/incremental-planning-verification.json`。真实模型轮数/耗时会变化，测试结果不承诺每次固定次数。

本次定位工具复测记录见 `backend/code-navigation-verification.json`；新增开发的真实模型与真实工具端到端记录见 `backend/code-navigation-development-verification.json`。隔离测试分别覆盖原网站新增需求定位，以及现有 Python CLI 增加参数、修改业务代码、运行新旧行为验收。单元回归额外覆盖冷/暖索引、同长度修改、删除、二进制/CRLF 哈希、包引用歧义、搜索分页、符号范围、计划覆盖及交接失效。
