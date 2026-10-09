# 应用开发与部署
平台模板 web-v1（React + TypeScript + Vite + shadcn/ui + Tailwind CSS）。业务要求见 .atoms/PLAN.md；实现规范见 docs/AGENT_GUIDE.md 和 frontend/README.md。首页路由 frontend/src/pages/Index.tsx；先写根 DESIGN.md，再实现业务。

## 命令
`npm ci`（锁定依赖）；`npm run dev -- --host 0.0.0.0`；`npm run build`（根 dist）。
生成或修改代码期间可编写并运行相关文件/用例的局部单元测试；最终不统一运行测试套件。先系统检查实际代码、类型、接口和数据流，再构建并启动真实服务验证完整核心链路；测试可随当前改动更新，其结果仅用于当次验证，不作为自动交付门槛。
应用查看器由 .atoms-workspace.json 管理端口和路径，默认已经配好。

## 一键部署
安装 Docker Compose 后运行 `sh deploy.sh`，访问 http://127.0.0.1:23871。
用 `APP_PORT=其他端口 sh deploy.sh` 更改端口；`APP_BIND_ADDRESS=0.0.0.0 sh deploy.sh` 对外提供服务。
`docker compose -f compose.yaml logs -f` 查看日志；`docker compose -f compose.yaml down` 停止。
纯前端的 dist 可以使用平台发布上传 MinIO。

Docker 构建默认使用 host 网络，适配本服务环境；其他环境可用 `BUILD_NETWORK=default sh deploy.sh`。

## 实现
模板只提供工程起点，没有产品业务。必须替换首页，完整实现用户要求并通过真实交互验收。
