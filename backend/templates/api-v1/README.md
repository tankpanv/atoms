# 应用开发与部署
平台模板 fullstack-v1（React + TypeScript + Vite + shadcn/ui + Tailwind CSS + FastAPI + PostgreSQL）。业务要求见 .atoms/PLAN.md；实现规范见 docs/AGENT_GUIDE.md 和 frontend/README.md。首页路由 frontend/src/pages/Index.tsx；先写根 DESIGN.md，再实现业务。

## 命令
`npm ci`（锁定依赖）；`python -m pip install -r backend/requirements.txt`；`npm run dev -- --host 0.0.0.0`；`npm run build`（根 dist）。
当前生成或修改代码可编写并执行相关文件/用例的局部单元测试；最终不统一运行测试套件。先系统检查业务源码、类型和前后端接口，再真实启动前后端并验证核心链路及数据回读；测试随当前改动更新，局部结果不作为最终验收证据。
应用查看器由 .atoms-workspace.json 管理端口和路径，默认已经配好。

## 数据库连接器
项目创建时自动配置独立 PostgreSQL Schema 和应用角色。平台预览及执行命令自动注入 APP_DATABASE_URL、APP_DATABASE_SCHEMA。请保持 backend/app/db.py 的 connection()/initialize()/database_schema()，直接添加业务表与真实迁移。使用 %s 参数、字典行；服务端凭据不得写入源码、README 或前端。无需启动 PostgreSQL Docker。

## 一键部署
在平台连接器页面下载本项目 env.connector 放到项目根目录；在同一平台 Docker 主机安装 Docker Compose 后运行 `sh deploy.sh`，访问 http://127.0.0.1:23871。脚本自动接入现有项目数据库网络，只启动应用容器。不要提交 env.connector（含本项目专属连接凭据）。
用 `APP_PORT=其他端口 sh deploy.sh` 更改端口；`APP_BIND_ADDRESS=0.0.0.0 sh deploy.sh` 对外提供服务。
`docker compose -f compose.yaml logs -f` 查看日志；`docker compose -f compose.yaml down` 停止。
完整前后端使用上述 Docker 部署，同源提供页面与 /api；数据库始终使用现有平台连接器，不创建数据库容器或本地卷。独立部署先配置 APP_DATABASE_URL/APP_DATABASE_SCHEMA 为本项目连接信息，地址必须可从部署机器访问；数据保存在平台项目 Schema，重建应用不会清空。平台发布会归档完整应用镜像，启动独立发布容器并使用独立生产数据库；后续开发构建不会改变已发布前后端。

## 本地全栈开发
后端 `python -m uvicorn backend.app.main:app --port 8000`；另一终端 `API_PORT=8000 npm run dev`。平台预览自动启动两者，无需手动配端口。`python -m unittest discover -s backend/tests -v` 检查数据库持久化与事务；业务仍需新增实际测试。

Docker 构建默认使用 host 网络，适配本服务环境；其他环境可用 `BUILD_NETWORK=default sh deploy.sh`。

## 实现
模板只提供工程起点，没有产品业务。必须替换首页，完整实现用户要求并通过真实交互验收。
