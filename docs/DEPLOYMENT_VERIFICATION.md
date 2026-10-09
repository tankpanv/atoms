# 一键部署验证记录

2026-10-09，在独立 Compose 项目 `atoms-deploy-validation` 中验证，使用独立端口、镜像和数据卷；未重启现有 `atoms-demo` 服务，未改其 `.env`，未调用付费模型。

已通过的真实检查：

- 无 `.env` 初始化：自动复制 example、生成独立随机密钥，文件权限 600；Compose 引用的所有环境变量均在 example 中有对应项。
- 构建 backend / frontend 镜像；前端执行 TypeScript 与 Vite 生产构建，由 Nginx 提供静态页面和 API 代理。
- PostgreSQL 自动建表、MinIO 建桶与对象写入/读回/删除、前端 API 代理、登录公钥、Agent HTTP、预览网关及宿主机端口。
- Chromium 真实加载 `/zh/dashboard`、渲染页面，无未捕获 JavaScript 错误。
- 创建临时项目，经真实 Agent 调度隔离 Worker，启动 Python HTTP 服务，经预览网关读取当前项目页面；清理临时 Worker、网络、项目、账号、数据库角色/Schema 和工作区。
- 已运行时再次执行 `--no-build`：六个核心容器全部重建；`.env` 内容未变；重启前真实注册的账号/刷新会话、登录公钥、MinIO 对象和工作区哨兵在重启后仍有效。
- `start.sh check` 检查已运行服务；配置中的 shell 表达式不被执行。
- 预览配置与旧前端镜像不匹配、其他部署占用端口时，脚本返回非零，原容器保持运行。
- 普通 CPU 与可选 GPU Compose 配置校验，以及 ShellCheck / Bash 语法 / Python 编译检查。

验证服务器已有 Docker，故没有在本机重装 Docker 或实测空白虚拟机的 apt 安装；自动安装流程使用 Docker 官方签名 apt 仓库。GPU/模型下载、外部 AI key 与额度、公网安全组、域名及 TLS 需要在目标环境按 README 配置；这些不属于上述本机成功结论。默认 CPU 核心部署已实际启动通过。
