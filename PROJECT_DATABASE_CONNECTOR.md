# 托管项目数据库连接器

默认全栈模板为 React / FastAPI / PostgreSQL。复用平台现有 PostgreSQL，每个项目创建 `project_<UUID hex>` Schema 和 `atoms_app_<UUID hex>` 应用角色，不创建项目 PostgreSQL 容器。用户明确指定其他技术栈或数据库时仍遵循用户要求；已有业务代码不会被模板覆盖。

## 生命周期

- 创建项目及 GitHub 导入通过 `grant_project_role()` 自动配置业务 Schema 和角色。平台启动为既有项目补齐连接器元数据，幂等配置不会删除业务数据。
- `public.project_databases` 记录项目、Schema 和角色关系。业务账号不能访问平台项目表或其他项目 Schema，不能在 public 创建表或创建其他 Schema。
- Worker 的任务控制账号与业务数据库账号分离。业务命令和预览服务只接收 `APP_DATABASE_URL` / `APP_DATABASE_SCHEMA`，不会获得平台管理账号。
- 应用数据库连接使用本项目 search_path、字典行、参数化 SQL 和独立事务；成功提交、异常回滚。启动迁移有事务锁，业务建表应放在初始化迁移中，避免多个请求并发执行 DDL。
- 删除项目时停止运行资源，终止该业务账号连接，删除本项目 Schema、角色和元数据；不会删除平台数据库或其他项目的数据。

## 使用

连接器页面的 PostgreSQL 卡片显示本用户项目及 Schema，点击「检查连接」执行真实数据库探测。默认模板的 `backend/app/db.py` 提供 `connection()` / `initialize()` / `database_schema()`；直接编写业务表、路由和真实测试。Agent 的规划、实现提示词和模板文档已统一使用托管连接器。

在连接器页面点击本项目「下载部署配置」，将 `env.connector` 放到项目根目录。在平台同一 Docker 主机执行 `sh deploy.sh`，脚本读取该文件并加入已有项目数据库网络，只部署应用容器。默认访问 `http://127.0.0.1:23871`。异地主机部署需要使用可访问平台 PostgreSQL 的地址，并移除该主机不存在的 `APP_DATABASE_NETWORK` 设置。

`env.connector` 含本项目专属凭据，只允许项目所有者下载，不缓存；已排除在 Git、Docker 构建上下文、Agent 文件读取/上下文和 GitHub 同步之外。

## 验证

`backend/tests/test_project_database.py` 的真实 PostgreSQL 测试需显式设置 `ATOMS_TEST_DATABASE_URL`；使用现有平台数据库，只创建并清理随机测试项目。无凭据的镜像构建跳过该集成测试；部署后已单独运行并通过。

Playwright MCP 已实测：连接器探测、所有者下载配置、其他所有者访问被拒绝、应用查看器新增/读取/删除记录、刷新与前后端重启后数据保留、两个项目同名表隔离、一键 Docker 部署读写同一项目数据、应用容器重启后数据保留，以及删除项目后 Schema/角色/元数据/工作区/容器/网络清理。测试项目和账号已清理。

权限设计参考 [PostgreSQL Schema 文档](https://www.postgresql.org/docs/18/ddl-schemas.html)；事务行为参考 [Psycopg 文档](https://www.psycopg.org/psycopg3/docs/basic/transactions.html)。
