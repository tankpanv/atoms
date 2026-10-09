# 默认项目模板与业务优先流程

2026-10-04：将临时生成前端、逐次拼装后端和部署配置，替换为可信版本化模板，让模型优先实现完整业务。

## 选择与初始化

- 新 Web 项目未指定栈、仅需浏览器能力：web-v1，React / TypeScript / Vite / shadcn-ui / Tailwind CSS。
- 需要共享数据、服务端持久化、账号、秘密或服务器计算：fullstack-v1，相同前端 + FastAPI / PostgreSQL。
- 指定栈、版本、数据库或小程序等平台：遵循用户要求，不自动套模板或调用最新版生成器替代指定版本。
- 现有项目及继续对话：保持代码、架构、依赖与检查点。CLI / library / service 不强加 Web 模板。

选择规则在 backend/project_templates.py。安装前检查每个目标文件和路径边界，拒绝覆盖已有应用。执行器生成真实初始化证据，.atoms/template.json 保存版本、内容 SHA-256 与清单。

新空工作区不提供无意义的探索工具。架构确定后，一次交付实际源码、命令及 docs/AGENT_GUIDE.md，锁文件不塞进模型上下文，已安装模板不再提供重复 scaffold 工具。

## 直接编写业务

App.tsx 组合 pages/Index.tsx；lib/ 放领域逻辑与 API。后台入口、路由、数据库事务已经存在。共享接口保持精确签名，优先以 1–2 个完整端到端业务单元编写和验收。

前端规范参考用户提供的 FRONTEND_DESIGH_README.md，适配为 frontend/README.md。先编写根 DESIGN.md 四部分（Direction & Layout、Tokens、Shared Patterns & States、Media），已有设计沿用。组件库源码真实预置，编译、测试均支持 @/ 别名；组件库不整包塞入 Agent 上下文。保留预览路由 basename、API 代理和 npm workspace。

编写规范涵盖完整需求、模块组织、视觉与响应式、加载/错误/空状态、输入验证、测试选择器、参数化 SQL、事务回滚、迁移、鉴权边界以及真实 API 和浏览器验收。验收重点为服务成功启动、真实页面预览、核心交互及必要的数据持久化链路；通过后直接交付，不再调用模型逐文件源码复核。发现实际问题时针对相关代码修复。模板占位页不能通过交付检查；基础测试不能替代业务验收。模板不添加未请求的产品功能。

## 依赖缓存

镜像构建时安装锁定基线依赖并执行真实构建。只有根 package.json、frontend/package.json、package-lock.json 全部精确匹配且无自定义 .npmrc 时复用缓存。根 node_modules 和 npm 可能在 frontend/node_modules 嵌套的依赖均独立复制并交给项目用户，不共享可修改依赖。声明改变时安装更新；后续按 manifest 和安装标记复用。

Vitest 已配置 jsdom、Testing Library、user-event、jest-dom 和每例 cleanup，最多 2 个工作线程，避免默认工作进程数量超过项目隔离限制而静默失败。空测试集仍失败，必须编写真正的业务测试。

## 预览与一键部署

.atoms-workspace.json 自动管理前端和 API 的端口、就绪、重启及清理。先启动 API 再注入 API_PORT；Vite 使用 BASE_PATH + api 代理键，避免拦截平台 /api/runtime 前端路径。数据库连接器自动注入 APP_DATABASE_URL/APP_DATABASE_SCHEMA，业务角色只能访问本项目 Schema，不使用平台管理账号或单独数据库容器。

模板含 Dockerfile、compose.yaml、deploy.sh 和 README。运行 sh deploy.sh 真实构建部署，默认 127.0.0.1:23871；APP_PORT / APP_BIND_ADDRESS 可配置。构建网络默认为 host，适配当前服务环境，其他环境可用 BUILD_NETWORK=default。

全栈由非 root FastAPI 用户同源提供 dist 和 API，专属 Docker 卷保存数据库，重建/重启保留数据。COPY 显式设置文件所有权，兼容工作区私有权限，不放宽宿主机隔离。纯前端使用 Nginx。框架镜像和依赖锁定，减少新项目漂移。

平台现有 MinIO 发布只上传静态资源；完整后端上线使用 Docker 部署，不将静态发布声称为后端已上线。

## 验证

镜像发布检查覆盖真实初始化、指定栈与版本、拒绝覆盖、缓存匹配和租户隔离，以及缓存模板在实际项目用户/资源限制下的构建、组件清理测试。PostgreSQL 集成测试显式使用现有数据库，验证真实事务、隔离和重启；镜像构建没有数据库凭据时跳过数据库集成测试。另运行 Agent 效率、工具契约、执行与预览回归。

真实模型调试生成公共留言板，验证数据库 API、必填校验和客户端交互；Playwright MCP 在实际生产 Docker 部署中验证空输入、写入、刷新保留及容器重启后的持久化。调试发现的 Vitest 工作池、组件清理、部署文件所有权与构建网络问题已经修入模板。模板减少固定工作，不承诺所有任务都在固定分钟数内完成。
