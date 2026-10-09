# Atoms Demo

Atoms 风格的 AI 项目构建平台，包含项目管理、对话式编码、预览、文件编辑和发布。

项目实现思路、完成程度和后续优先级见 [项目简要说明](docs/PROJECT_BRIEF.md)。

Agent 构建的详细流程、架构、工具、循环退出条件及上下文缓存复用见 [Agent 构建全过程](docs/AGENT_BUILD_PIPELINE.md)。

## 部署

需要 Linux、Docker Engine 和 Docker Compose v2；Ubuntu/Debian 缺少 Python、curl 或 Docker 时，部署脚本会安装依赖。

### 首次启动

```bash
git clone https://github.com/tankpanv/atoms.git atoms-demo
cd atoms-demo
./start.sh start
```

`start.sh` 在缺少 `.env` 时复制 `.env.example`、生成密钥，构建并启动 PostgreSQL、MinIO、后端、Agent、预览网关和前端；完成健康检查后才报告成功。首次启动需下载镜像并构建，等待时间较长。

### 配置 AI

编辑 `.env`：

```env
AI_API_KEY=你的模型服务密钥
AI_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=openai/gpt-6-luna
```

保存后运行 `./start.sh restart --no-build`。`AI_API_KEY` 为空时平台可以启动和登录，但不能生成项目。

### 访问与端口

默认访问地址：

- 控制台：`http://服务器IP:25173/zh/dashboard`
- 项目预览：`http://服务器IP:28082`
- 后端健康检查：`http://localhost:28080/api/health`

防火墙放行 TCP `25173` 和 `28082`。后端、PostgreSQL 和 MinIO 无需对公网开放。端口被占用时修改 `.env` 中对应的 `*_PORT`，并同步更新防火墙和外部地址。

`.env.example` 使用本地默认值。公网或隧道访问时，在 `.env` 配置浏览器实际使用的地址：

```env
APP_PUBLIC_URL=http://你的域名或IP:25173
VITE_PREVIEW_ORIGIN=http://你的域名或IP:28082
AUTH_SECURE_COOKIE=false
```

控制台和预览必须使用不同源。HTTPS 部署时将两个 URL 改为各自的 HTTPS 域名，并设 `AUTH_SECURE_COOKIE=true`；修改预览地址后运行 `./start.sh restart` 重建前端。

如果用 frpc 并通过 `http://服务器IP/` 访问，可将本机 `25173` 映射到远程 `25173`、本机 `28082` 映射到远程 `28082`，再由远程 Nginx 把 80 端口反代到 `127.0.0.1:25173`。示例见 [nginx-atoms.conf.example](nginx-atoms.conf.example)。设置 `APP_PUBLIC_URL=http://服务器IP`、`VITE_PREVIEW_ORIGIN=http://服务器IP:28082`。

### 服务管理

```bash
./start.sh start                 # 一键构建并启动全链路服务
./start.sh restart               # 重建镜像并重启全链路
./start.sh restart --no-build    # 复用已有镜像重启（如只改 AI 密钥）
./start.sh status                # 查看服务状态
./start.sh check                 # 检查运行中的服务
./start.sh stop                  # 停止服务和项目 Worker

docker compose logs -f --tail=100 backend agent-service preview frontend
```

不带参数的 `./start.sh` 等同于 `./start.sh start`；它也可转发 `deploy.sh` 的 `--host`、`--env-file` 和 `--timeout` 参数。停止和重启会保留数据；不要执行 `docker compose down -v` 或删除 Docker volumes。

### 数据与备份

PostgreSQL 保存账号、项目和对话；MinIO 保存发布文件；`agent_workspaces` 保存项目源码；`platform_state` 保存平台密钥。迁移服务器时需同时备份这些卷和 `.env`。数据库备份示例：

```bash
mkdir -p .backups
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > .backups/database.sql
```

### 故障检查

```bash
./start.sh check
./start.sh status
docker compose logs --tail=100 backend agent-service preview frontend
```

页面无法访问时检查端口监听和防火墙；预览失败时检查 `VITE_PREVIEW_ORIGIN`；登录状态不保留时，HTTP 部署应使用 `AUTH_SECURE_COOKIE=false`；AI 失败时检查密钥、模型 ID、额度和服务端日志。

## 功能

完整的树状功能列表见 [已实现能力清单](docs/IMPLEMENTED_CAPABILITIES.md)，包含详细子功能、使用条件和代码索引。

- 邮箱注册登录、项目和对话持久化、收藏、版本管理。
- Agent 按需求规划和构建项目，支持继续任务、进度恢复和真实运行验收。
- 浏览器预览、代码编辑、终端、文件上传下载和项目发布。
- 支持图片与文档附件；本地图片识别为可选功能。
- 支持项目隔离、资源限制、专家技能和 专门针对项目使用的自建云数据库(docker启动 scheme 隔离)连接。
- 支持生成和预览 PPT、PDF、Office 文档、表格及其他文件成果。

详细架构和实现说明见 [docs](docs/)；部署参数见 [`.env.example`](.env.example)。
