# 专家功能真实验证记录

日期：2026-10-03。通过 Playwright MCP 操作运行中的本地网站，使用独立测试账号；没有替换构建服务或模型响应。

## UI 与 API 验证

| 检查 | 实际结果 |
| --- | --- |
| 专家列表 | 三位专家正确显示，卡片可打开详情 |
| 名称/描述/能力搜索 | 数据库搜索命中后端专家；未知关键词显示空结果 |
| 分类与排序 | 产品设计只显示 UI 设计师；全部恢复列表；最常用排序与实际两次使用一致 |
| 专家详情 | 能力列表、三个示例需求、注册案例可预览 |
| 案例交互 | ATELIER 案例切换至烘焙后咖啡隐藏，可颂可见；后端案例展示真实架构说明 |
| 收藏 | 收藏、取消收藏、我的专家过滤及刷新后的数据库恢复通过 |
| 示例需求 | 点击示例后进入首页，需求文字与对应专家同时填入 |
| 首页加号与下方选择器 | 搜索、添加、移除、空结果及外部关闭通过 |
| 首页到构建页 | 创建请求包含 `expert_ids=["website-architect"]`，构建页继承同一专家 |
| 构建页选择 | 加号内选择、三位专家同时添加、逐一移除通过 |
| 项目持久化 | 移除后刷新为空，重新选择后刷新恢复网站专家 |
| 消息历史 | 两次用户消息都显示所使用的专家 |
| 权限与输入 | 其他账号修改项目返回 404；未知专家及超过数量上限返回 422 |
| 个人收藏隔离 | 独立账号的已收藏专家数量为 0，不读取首个账号的收藏 |
| 手机适配 | 390px 专家列表、详情、案例、首页选择器无横向溢出；选择器边界 x=36，right=356 |
| 键盘 | 外部点击及 Escape 关闭详情/案例；案例开启时底层详情 inert，焦点由关闭按钮进入案例 iframe |

## 真实模型构建

项目：`6989b8ce-bebb-49f5-b7ea-9ff59f52f053`（独立测试账号）。

任务 1：`3c26f743-2bd3-4137-a0a7-d560dc18ef02`。

- 在首页选择网站专家并提交「栖木」咖啡馆网站需求。
- worker 的任务步骤记录「已加载专家：网站设计与全栈开发专家」。
- skill 快照：`website-architect`，版本 `1.0.0`，SHA256 `19d012291cedae84650e7ddfdd1c414b41c6814ba015e0dafaa38f6e5bd5a720`。
- 实际创建 `.atoms/DESIGN.md` 与 `.atoms/ARCHITECTURE.md`，使用 Vite/React/TypeScript，按首屏、菜单、营业时间、门店与联系区拆分模块。
- 需求限定前端，因此架构明确记录不需要数据库和后端，未添加无需求的服务。
- 任务最终为 `done / COMPLETE`，生成版本 1。项目的 3 项测试、生产构建和 worker Chromium 验收通过。
- 使用 Playwright MCP 再次检查实际生成页面：烘焙分类显示海盐可颂、柠檬磅蛋糕、时令水果塔和肉桂卷；咖啡分类显示栖木手冲、榛果拿铁、林间气泡和黑巧摩卡。切换实际改变条目。
- 实际页面在 390px 无横向溢出，独立浏览器检查的脚本错误和失败网络请求均为空。

任务 2：`cea4e3a7-c3ed-47eb-9ad5-8494302d7fa6`。

- 在构建页继续发送“把联系区域标题改为在栖木，慢慢相见”的请求。
- 请求保留相同 `expert_ids`；第二次任务单独保存同版本 skill 快照并记录加载步骤。
- 任务最终为 `done / COMPLETE`，生成版本 2。
- MCP 在实际预览中确认新标题，刷新后仍保留，烘焙菜单筛选仍有效。
- 数据库核对：项目默认专家、两条用户消息专家及两次任务专家一致；两个任务快照独立保存。

## 自动检查

- `tests.test_experts`、`tests.test_agent_execution`、`tests.test_agent_harness`：共 14 项通过。
- 专家测试覆盖不可变快照、未知 ID/路径拒绝、选择上限、去重及所有注册 skill/案例存在。
- 三位专家的 `SKILL.md` 均通过 skill-creator 校验。
- TypeScript 检查与最终前端生产构建通过。
- 后端镜像更新，新 worker 使用更新后的 skill 加载代码。
- 构建文件 `frontend/dist/index.html` 归属 `ubuntu:ubuntu`，权限 `644`。

## 调试中修复

- 新项目编辑器原先假定 `src/App.tsx` 已存在，造成初始 404；改为从实际文件列表选择存在的文件。
- 案例预览在详情滚动后保持完整可见，并隔离底层键盘焦点。
- 专家变化时不沿用不同 skill 的未完成计划。

## 截图

- [专家列表](../.playwright-mcp/experts-library-desktop.png)
- [专家卡片](../.playwright-mcp/experts-cards-desktop.png)
- [专家详情](../.playwright-mcp/experts-detail-desktop.png)
- [首页选择](../.playwright-mcp/experts-home-selected.png)
- [手机选择器](../.playwright-mcp/experts-selector-mobile.png)
- [手机案例预览](../.playwright-mcp/experts-preview-mobile.png)
- [实际生成网站](../.playwright-mcp/experts-generated-site-desktop.png)
- [实际生成网站手机布局](../.playwright-mcp/experts-generated-site-mobile.png)
- [后续修改完成](../.playwright-mcp/experts-followup-complete.png)
