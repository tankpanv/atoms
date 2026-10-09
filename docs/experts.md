# 专家与构建 skill

专家入口：`/zh/experts`。首页和构建页的对话框都有专家选择入口，加号菜单也可进入同一个搜索选择器。可同时选择最多三位专家；点击已选择的专家标签查看详情，右侧叉号移除。详情包含能力、示例需求、交互式案例和收藏功能；示例需求会带着对应专家填入对话框。

## 真实任务链路

1. `backend/expert_skills/catalog.json` 定义专家卡片、能力与案例；每个专家有独立的 `SKILL.md`。
2. 创建项目提交 `expert_ids`，保存到项目及初始用户消息；每次对话也记录自己的专家选择。
3. 任务入队时读取受信任的注册目录，将专家 ID、版本、完整 skill 内容与 SHA256 存入 `agent_jobs.expert_snapshots`。客户端不能传入任意 skill 路径或内容。
4. worker 从任务快照加载指令，分别附加到需求规划、代码实现和复核的 system message。上下文压缩保留 system 前缀。
5. 工作流程记录「已加载专家」步骤。任务开始之后调整选择只影响后续任务，不能修改已经保存的快照。
6. 所选专家发生变化时重新规划，避免沿用不同专家的未完成计划。
7. 项目专家选择通过拥有者校验的 PATCH 接口保存，刷新或重新进入恢复同一选择。收藏记录按用户隔离。

## 已提供专家

- 网站设计与全栈开发专家：视觉方案、前后端架构选择、功能拆分与实现、构建和浏览器验收。
- UI 设计师：设计系统、组件边界、响应式布局、可访问交互与状态。
- 后端架构师：数据模型、真实 API、输入与权限校验、事务、幂等及持久化验收。

案例是独立、可交互的设计/架构参考，页面明确标注其用途。预览通过沙盒 iframe 展示，不伪装成已经为用户接入的第三方服务或数据库。

## 增加或修改专家

在 `backend/expert_skills/` 创建对应目录和 `SKILL.md`（包含 `name`、`description` frontmatter），并在 `catalog.json` 添加元数据。名称和 ID 必须唯一，`skill` 指向受维护的目录。案例放在 `examples/`，由注册表引用。

更新 skill 时提升注册表中的 `version`，重新构建 backend 镜像供新项目 worker 使用。已有任务保留旧版本快照，新任务取得更新的内容。不得让用户输入决定文件路径。

## 接口

- `GET /api/experts`：当前用户可用专家、个人收藏及任务使用次数。
- `GET /api/experts/{id}`：专家详情。
- `PUT /api/experts/{id}/saved`：幂等更新个人收藏。
- `GET /api/experts/{id}/examples/{example}`：注册案例 HTML，附带限制性的 CSP。
- `PATCH /api/projects/{id}/experts`：保存项目的默认专家选择。
- 创建项目和发送消息接口接收 `expert_ids`。创建时默认为空；消息省略该字段时继承项目选择，显式空数组表示不用专家。

## 验证

`docker compose exec -T backend python -m unittest tests.test_experts tests.test_agent_execution tests.test_agent_harness`

另需通过 Playwright 实际执行专家列表、详情、案例预览、收藏刷新、首页选择、进入构建页、后续对话与真实 agent 构建流程。验证记录见 `experts-validation.md`。
