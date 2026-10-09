# Atoms Demo

这是一个独立运行的 Atoms 风格产品原型。前端使用 React + TypeScript，后端使用 FastAPI，项目和对话保存到 PostgreSQL。界面以 2026 年 10 月 1 日的 Atoms 中文控制台为参考，实现首页、资源、我的项目、提示词输入、项目工作区和预览。

## 从零一键部署（先看这里）

这套脚本用于 **单台 Linux 服务器**：自动准备 Docker、部署 PostgreSQL / MinIO / API 后端 / Agent / 预览网关 / 前端，完成真实健康检查后才显示成功。重复执行会先构建新镜像，再停止本部署的旧服务和项目 Worker、重新启动；数据库、发布文件和项目源码保留。**默认不需要 GPU，不下载本地视觉模型。**

推荐新手使用 Ubuntu 22.04 / 24.04 或 Debian 12 / 13，64 位 x86 服务器，至少 4 核、8 GB 内存、40 GB 可用磁盘；并发构建或本地视觉模型需要更多资源。服务器需要能访问 Docker 镜像仓库、系统软件源、PyPI、npm 和你使用的模型服务。首次构建包含 Chromium、LibreOffice、Node/Python 和项目模板依赖，可能耗时较长；后续复用 Docker 缓存，不会每次重新下载安装全部依赖。宿主机不用手动安装 Node.js、pip 或 PostgreSQL。

脚本使用本机 rootful Docker Engine 和 Compose v2（建议 2.26+），不支持远程 Docker daemon / rootless Docker / Windows 原生运行。已有可用 Docker 会复用；缺少 Docker / Compose 时按 [Ubuntu](https://docs.docker.com/engine/install/ubuntu/) 或 [Debian](https://docs.docker.com/engine/install/debian/) 官方签名 apt 仓库安装。安装系统软件需要 root 或 sudo 权限，普通账号可能需要输入自己的 sudo 密码。其他 Linux 发行版先自行安装 Docker Engine、Compose v2、Python 3 和 curl，再运行同一脚本。

### 1. 将代码上传到 GitHub

提交整个项目源码，必须包括 `backend/`（含 `templates/`）、`frontend/`、`scripts/`、`model_list`、Dockerfile、Compose 文件、`.env.example` 和脚本。可以在本地运行 `git status --short` 确认待提交内容。

**不要提交 `.env`、`.atoms_account`、私钥或真实生产配置。** 仓库的 `.gitignore` 已排除这些文件，以及 `.claude-images/`、本地构建依赖、部署状态和备份。`.env.example` 只包含无秘密的默认项和占位符。如果秘密曾经提交过，新增 `.gitignore` 无法清理历史，需要撤销对应密钥并清理 Git 历史。

### 2. 在新服务器拉取并执行

先 SSH 登录服务器。如果没有 Git，Ubuntu/Debian 执行：

```bash
sudo apt-get update
sudo apt-get install -y git
```

下面把 `你的GitHub用户名/你的仓库` 换成真实仓库路径，把 `你的服务器公网IP` 换成浏览器实际访问的 IP（例如 `203.0.113.10`；这是示例，不能照填）：

```bash
git clone https://github.com/你的GitHub用户名/你的仓库.git atoms-demo
cd atoms-demo
bash deploy.sh --host 你的服务器公网IP
```

私有仓库需要你自己的 GitHub SSH key 或 Personal Access Token。不要将 token 写到脚本或提交到仓库。`bash` 执行方式无需手工给脚本加可执行权限。

部署时会：

1. 准备 Python 3 / curl / Docker / Compose，复用已经安装的组件。
2. 缺少 `.env` 时复制 `.env.example`，生成随机 PostgreSQL 密码、MinIO 密码、Agent 密钥和 GitHub token 加密密钥，权限设为 `600`。已存在的 `.env` 保留，不会重置密码或覆盖自定义配置；`--host` 只明确更新两个浏览器访问地址。
3. 校验配置、固定端口和 Worker 镜像/卷名称；先完成镜像构建（前端含 TypeScript 检查及生产构建）。构建失败不会停止原来的运行服务。
4. 停止本部署的旧服务及项目 Worker，保留数据；按依赖顺序启动核心服务，等待健康。重启会中断正在执行的项目任务，建议在没有构建任务时操作。
5. 实际连接 PostgreSQL 并检查自动建表；创建/写入/读回/删除 MinIO 探测对象；检查前端 API 代理、登录公钥、Agent 和预览网关；用 Chromium 加载并检查前端渲染和运行错误；检查 Docker socket、Worker 镜像/工作区卷/网络，创建临时探测项目，实际启动隔离 Worker 和 HTTP 服务，经预览网关回读页面后清理探测项目，最后检查宿主机访问端口。

只有全部通过才显示 `[5/5] 服务启动与本机真实链路检查通过`，失败返回非零退出码。首次自动建表和存储桶初始化，无需手动执行 SQL 或创建 MinIO bucket。

### 3. 放行端口并打开页面

在云服务器的安全组/防火墙允许 **TCP 25173 和 TCP 28082**。使用系统防火墙时也需要允许这两个端口；脚本不会修改你的防火墙规则。随后在自己的电脑浏览器打开：

```text
http://你的服务器公网IP:25173/zh/dashboard
```

注册一个自己的账号即可登录。后台没有预设管理员邮箱/密码。`28082` 用于项目预览，浏览器必须也能访问，否则控制台可打开但项目演示会失败。

如果只是本机体验，可以直接 `bash deploy.sh`，访问 `http://localhost:25173/zh/dashboard`。远程服务器不要把 `localhost` 当成公网访问地址。内网部署把 `--host` 换成服务器内网 IP。

脚本的成功表示**本机服务和探测链路通过**，不会替你验证外部云安全组、DNS、HTTPS 证书或付费模型额度；这些需要按下面说明配置并从自己的浏览器确认。

### 4. 配置 AI（实际生成项目必需）

首次不填写 AI key 也能启动平台、注册登录和管理数据；**AI 生成不能在没有有效模型服务的情况下工作**。在项目根目录编辑自动生成的 `.env`：

```bash
nano .env
```

找到并填写：

```dotenv
AI_API_KEY=填入你自己的模型服务密钥
AI_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=填入该服务实际支持的模型ID
```

默认示例模型是 `openai/gpt-6-luna`，需要你的服务确实支持它。也可以填写兼容 OpenAI Chat Completions 的服务地址/模型；部分服务要求完整的 `/chat/completions` URL。模型账号必须有额度，服务器必须能连通该地址。密钥仅传给服务端，不会打包进前端。

nano 保存：`Ctrl+O`、回车，再 `Ctrl+X` 退出。之后执行：

```bash
bash deploy.sh --no-build
```

模型配置会随容器重建生效。脚本不会自动发起付费模型请求；登录后提交一个简单需求，检查实际生成及预览，是配置 AI 后的最后一步。

### 配置项说明

完整可复制项在 [`.env.example`](.env.example)。通常只需要改访问地址和 AI 三项，其余保持自动生成值即可。

| 配置 | 用途 / 注意事项 |
| --- | --- |
| `APP_PUBLIC_URL` | 控制台外部完整地址，如 `http://IP:25173`；用于公开链接/回调。 |
| `VITE_PREVIEW_ORIGIN` | 预览外部地址，如 `http://IP:28082`；必须与控制台不同源。留空使用当前浏览器主机和预览端口。 |
| `AUTH_SECURE_COOKIE` | HTTP 为 `false`；控制台配置好 HTTPS 后改为 `true`。HTTP 下误设 true 会导致登录无法保持。 |
| `POSTGRES_DB` / `POSTGRES_USER` | 首次建库名称和账号，默认为 `atoms_demo` / `atoms`。只用小写字母、数字、下划线。 |
| `POSTGRES_PASSWORD` | 首次自动生成。只用 URL 安全字符：字母、数字、点、下划线、`~`、`-`。不要在已有数据库上仅修改此项，需先在 PostgreSQL 中正确改密码。 |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | MinIO 登录及存储访问凭据，首次自动生成密码，至少 8 位。 |
| `MINIO_BUCKET` / `MINIO_REGION` | 发布对象存储桶和区域，默认 `atoms-published` / `us-east-1`。 |
| `AGENT_SECRET` | 后端/Agent 之间的密钥，也参与项目隔离凭据计算。部署后保持稳定，不可随意轮换。 |
| `GITHUB_TOKEN_ENCRYPTION_KEY` | 首次自动生成 Fernet key；已有 GitHub 授权 token 后不能随意更换。OAuth 未用可不配置其他 GitHub 项。 |
| `COMPOSE_PROJECT_NAME` / `WORKSPACE_VOLUME` | 决定实例/数据卷标识，首次确定后不要随意改。工作区默认 `<项目名>_agent_workspaces`。目录名可以不同，默认仍使用 `atoms-demo` 实例。 |
| `AGENT_IMAGE` / `FRONTEND_IMAGE` | 本机镜像名称。多实例部署分别设不同名称，避免覆盖其他实例镜像。 |
| `PROJECT_MEMORY_MB` / `PROJECT_CPUS` | 单个生成项目的 Worker 上限，不是平台总资源。并发数量按服务器容量调整。 |
| `ENABLE_LOCAL_VISION` / `VISION_GPU` | 默认均 false。需要本地图片识别时才开启，见下文。 |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` / `GITHUB_CALLBACK_URL` | 可选 GitHub OAuth App，回调必须与真实 HTTPS 控制台地址和 GitHub 设置一致。 |
| `ACCOUNT_CONNECTOR_AUTH_URLS` | 可选其他连接器授权入口，JSON 必须单引号包裹，如 `'{"GitHub":"https://example.com/oauth/github"}'`。 |

`.env` 由 Docker Compose 解析，**不要执行 `source .env`**。如果你自己的值含 `$`，按 Compose 规则使用单引号包裹避免插值，例如 `AI_API_KEY='你的值'`；JSON 也使用单引号。自动生成的密码均为安全字符，不需要手工处理转义。

手工 `cp .env.example .env` 时，脚本不会把已有配置当作首次自动配置：请先用 `openssl rand -hex 24` 生成并替换两个 `replace-with-random-hex`，用 `openssl rand -hex 32` 填入 `AGENT_SECRET`。更简单的做法是在全新安装时让脚本自动创建 `.env`，避免遗漏。**不要为重新生成配置删除正在使用的 `.env`，否则新随机密码与已有数据库不匹配。**

旧版本 `.env` 缺少新增项时使用兼容默认值，不会悄悄更换原数据库凭据；可以对照 example 手动补齐。旧的 `backend/.auth_private.pem` 会在首次新脚本启动时迁移到独立 `platform_state` 数据卷，后续启动复用，不依赖上传私钥到 GitHub。

| 服务 | 默认宿主机监听 | 是否需要对外开放 |
| --- | --- | --- |
| 前端 Nginx（生产构建静态资源 + API/SSE/WebSocket 代理） | `0.0.0.0:25173` | 是，或用 HTTPS 反代 |
| 项目预览网关 | `0.0.0.0:28082` | 是，或用独立 HTTPS 域名反代 |
| API 后端 | `127.0.0.1:28080` | 否，前端 `/api` 已代理 |
| PostgreSQL | `127.0.0.1:25432` | 否 |
| MinIO API / Console | `127.0.0.1:29117` / `127.0.0.1:29118` | 否 |
| Agent / 可选 Ollama | 仅 Docker 内网 | 否；Agent 挂载 Docker socket，不能暴露公网 |

如端口被其他程序占用，脚本明确报错，**不会偷偷换端口**。修改 `.env` 对应 `*_PORT`，同步安全组和外部地址，再运行 `bash deploy.sh`。更换预览地址/端口需要重建前端（Vite 构建时写入配置）；`--no-build` 会检查镜像标签并阻止使用旧的预览配置。

### 重启、更新、停止与日志

始终在项目根目录执行：

```bash
# 拉取新代码并构建、重启、检查
git pull
bash deploy.sh

# 仅重启，复用已构建镜像；适合只修改 AI key 等服务端配置
bash deploy.sh --no-build

# 查看所有服务，包括可选视觉服务
bash start.sh status

# 停止本实例及其项目 Worker，保留全部数据
bash start.sh stop

# 检查当前运行服务，不重启，不发起模型请求（不另建临时 Worker）
bash start.sh check

# 日志（Ctrl+C 退出日志查看，不会停止服务）
docker compose logs -f --tail=100 backend agent-service preview frontend

# 脚本帮助
bash deploy.sh --help
```

`bash start.sh start` / `restart` 都等价于默认部署重启流程。启动健康检查默认最多 600 秒，可用 `bash deploy.sh --timeout 900` 增加；这个参数不限制镜像构建/首次模型下载时间。已有服务设置 `restart: unless-stopped`，Docker 开机运行时会恢复之前未手动停止的服务；手动执行 stop 后需再次执行部署脚本。

需要独立环境文件时：`bash deploy.sh --env-file .env.staging --host IP`。所有后续 status/check/stop 也必须传相同 `--env-file`；原生 Docker 命令使用 `docker compose --env-file .env.staging ...`。同机多实例需不同 `COMPOSE_PROJECT_NAME`、镜像名、工作区卷和六个端口。

### HTTPS / 域名 / 内网穿透

公网长期使用建议为控制台与预览分别配置 HTTPS 域名，例如 `app.example.com` 和 `preview.example.com`。在 Nginx/Caddy 或云负载均衡器上配置 TLS：控制台反代本机 `25173`，预览反代本机 `28082`，支持 WebSocket，关闭 SSE 缓冲，长连接超时建议 3600 秒。修改：

```dotenv
APP_PUBLIC_URL=https://app.example.com
VITE_PREVIEW_ORIGIN=https://preview.example.com
AUTH_SECURE_COOKIE=true
```

然后执行 `bash deploy.sh` 重建前端并重启。证书、DNS 和反代由你配置，脚本不会自动申请证书。预览不能与主站同源，否则会破坏生成代码的来源隔离。不要让 HTTPS 控制台加载 HTTP 预览，浏览器会拦截混合内容。

内网穿透时，分别映射前端和预览端口，把上述两个 URL 填成隧道实际对外地址。frps 端口已经被 Nginx 占用时不能让 TCP 隧道抢占；可参考 [nginx-atoms.conf.example](nginx-atoms.conf.example) 并替换其中示例地址。数据库、MinIO、Agent 不需要穿透。

### 可选本地图片识别

默认平台启动不依赖 Ollama。使用支持图片输入的远程模型时可直接处理图片；选择纯文本模型并上传图片时，需要开启本地视觉服务或配置可用的 `VISION_URL`。

CPU 模式在 `.env` 设置：

```dotenv
ENABLE_LOCAL_VISION=true
VISION_GPU=false
VISION_URL=http://vision:11434
VISION_MODEL=qwen2.5vl:3b
```

执行 `bash deploy.sh`。首次下载模型需要网络和额外磁盘，视觉容器最多使用 10 GB 内存；建议服务器至少 16 GB 内存，CPU 推理可能较慢。之后模型文件复用 `vision_models` 卷。

GPU 模式还需要宿主机正确安装 NVIDIA 驱动及 [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)，`nvidia-smi` 可用，再设 `VISION_GPU=true`。脚本会加载 `docker-compose.gpu.yml`。没有 GPU 的服务器不要设 true。可选视觉服务初始化失败也会明确报部署未通过，不会宣称其已经可用。

### 数据保存与备份

默认持久卷：`atoms-demo_postgres_data`（账号/项目/消息）、`atoms-demo_minio_data`（发布成果）、`atoms-demo_agent_workspaces`（项目源码与数据）、`atoms-demo_platform_state`（平台登录私钥）；开启视觉时另有 `atoms-demo_vision_models`。项目名改过则前缀相应变化。**更新代码与重启不会删除这些卷。不要执行 `docker compose down -v` 或盲目 `docker volume prune`。**

部署只上传源码，**不会自动迁移旧服务器的用户、项目、发布成果和凭据**。迁移这些内容需要同时备份数据库、工作区、MinIO、平台密钥以及旧 `.env`。只复制源码属于全新平台，新机器的随机密码应与自己的新数据库匹配。

数据库逻辑备份示例（项目名/环境文件按实际调整，备份不要进 Git）：

```bash
mkdir -p .backups
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > .backups/database.sql
```

完整冷备份时先 `bash start.sh stop`，在受保护位置保存 `.env`，并备份上述四个持久卷（可通过存储平台快照或 Docker volume 备份）。恢复时使用同一套 `.env` 和卷名，再运行部署。MinIO/工作区/数据库需要同一时间点，避免数据不一致。平台登录私钥与 GitHub token 加密密钥也是备份的一部分。

### 常见问题

- **Docker 无法安装/拉取**：检查服务器到 Docker 官方源和镜像仓库的网络、DNS、磁盘和 apt 锁。已有 containerd/docker.io 冲突时按终端错误和官方安装文档处理；脚本不会为安装 Docker 自动卸载已有生产服务。重试同一命令会复用已下载的缓存。
- **Docker 权限不足**：使用有 sudo 权限的账号；也可在项目目录用 `sudo bash deploy.sh`。脚本不会自动把账号加入高权限 docker 组。不要在文件权限不可信的源码目录以 root 执行。
- **`端口已被其他程序占用`**：用 `sudo ss -lntp` 查占用；改 `.env`，同步 URL / 安全组后重新执行。
- **数据库 authentication failed**：通常是保留旧数据卷，却更换了 `.env` 密码/账号；恢复原配置，或先正确修改数据库账号密码，不要删除数据卷“修复”。
- **启动失败但页面仍能打开**：不能据此当成部署成功；看 `bash start.sh status` 和对应服务日志，修复后重试。可选视觉开启后的原生日志命令加 `--profile vision`。
- **浏览器访问超时**：先在服务器 `curl -I http://127.0.0.1:25173`，本机可访问再检查公网 IP、安全组、系统防火墙、Nginx/隧道映射。云环境还可能有额外 ACL。
- **页面正常但项目预览失败**：检查浏览器能否访问 `http://IP:28082/health`、`VITE_PREVIEW_ORIGIN` 是否正确，以及 HTTPS 混合内容；改后重新构建前端。
- **AI 生成失败**：检查 key、模型 ID、额度和 `AI_BASE_URL`，查看 backend/agent-service 日志；服务启动检查不等于外部模型服务已经验证成功。
- **登录后刷新又退出**：HTTP 环境不要开启 `AUTH_SECURE_COOKIE`；同时检查是否混用了不同 IP/域名，以及代理是否正确传递 cookie。

前端部署默认使用 Nginx 提供生产构建，API/SSE/WebSocket 同源代理到后端。源码开发热更新可在 `frontend/` 内执行 `npm ci && npm run dev`，原开发镜像保存在 `frontend/Dockerfile.dev`。平台内生成项目的实时预览/热更新仍由 Worker 与预览网关处理。

## 账号与历史数据

控制台提供邮箱注册、登录和退出。浏览器用 RSA-OAEP 加密密码后提交，服务端用 Argon2id 保存密码哈希；密码明文不会出现在请求体或数据库中。局域网 IP 的 HTTP 页面也可以完成浏览器端加密。访问令牌只保存在页面内存，有效期 7 天；刷新令牌放在 HttpOnly Cookie 中，有效期 7 天。页面打开期间每 12 小时自动刷新访问令牌，多个标签页共用稳定的刷新 Cookie，重新打开页面时也会通过刷新 Cookie 恢复会话。

项目和对话按用户隔离，并持久保存于 PostgreSQL。旧版本创建的匿名项目没有所属用户，不会自动归到新账号。局域网 HTTP 可以用于调试；正式公网部署应使用 HTTPS，并在 `.env` 设置 `AUTH_SECURE_COOKIE=true`。

## AI 编程工作区

项目根目录的 `.env` 用于配置自己的模型服务，首次部署自动从 `.env.example` 创建。填写自己的密钥：

```env
AI_API_KEY=your_key_here
AI_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=openai/gpt-6-luna
```

修改 `.env` 后重新运行 `./start.sh`。密钥只传入服务端。默认模型为 `openai/gpt-6-luna`。模型列表来自根目录 `model_list`，首页和构建页共用带搜索、厂商分组的自定义下拉组件。

提交需求会进入 `/zh/chat/<项目 ID>`，对应独立持久化工作区。Coding Agent 按 UNDERSTAND → EXPLORE → PLAN → IMPLEMENT → TEST → REVIEW → COMPLETE 执行：结合对话、附件和实际源码，明确判断前端/后端/全栈架构，形成结构化需求、任务依赖和验收步骤；通过真实 CLI 初始化新项目，再用文件、补丁、搜索和终端工具实现模块化代码。`.atoms/task-state.json` 保存任务进度及真实执行证据；上下文压缩保留这些状态和完整工具调用组。普通档优先真实构建、服务启动和前端演示首屏，未配置的后端外部依赖记录 TODO，不反复运行单元测试或逐接口检查；深度/高级档再按计划执行更完整的 HTTP/浏览器链路。所有档位都必须通过当前源码的真实构建和启动检查，不能用模拟结果冒充真实服务成功。任务阶段、工具输入输出、耗时、模型用量、消息和版本仍保存在 PostgreSQL，停止或超出预算后发送“继续”可恢复原计划与已完成进度，最终验收会重新执行；新增要求和附件会重新规划。详见 [Harness 分析与实现](docs/AGENT_HARNESS.md) 和 [持久会话、增量缓存与自动压缩](docs/AGENT_SESSIONS.md)、[构建全流程成本优化](docs/AGENT_EFFICIENCY.md)。

各阶段默认使用聊天中选择的模型，也可在 `.env` 设置 `AI_UNDERSTAND_MODEL`、`AI_PLAN_MODEL`、`AI_IMPLEMENT_MODEL`、`AI_REVIEW_MODEL` 分别路由到 OpenRouter 支持的模型。可通过 `AGENT_MAX_ITERATIONS`（默认 200）、`AGENT_MAX_TOKENS`（默认 15000000，取决于模型返回的 usage）和 `AGENT_TIMEOUT_SECONDS`（默认 3600）限制单次任务。代码检索目前采用有界关键词排名和 Agent 的按需工具搜索，不依赖向量数据库；大型代码库仍需要更强的增量索引。项目可在 `.atoms-workspace.json` 中配置 `build` 和 `test` 命令；没有配置时根据 `package.json` 的脚本及 Python 测试文件检测。 `test` 支持字符串或命令数组；模型和网络临时失败会有界重试。

构建工作区采用全屏双栏结构：左侧展示需求、智能体工作流程与后续对话；右侧可切换应用查看器、文件编辑器和工作区终端。预览控制台在网页下方展开，按全部、错误、信息筛选浏览器日志，并支持搜索与清空。顶部提供版本历史、分享和发布入口。编辑器支持文件名与内容搜索、新建、重命名、删除、上传二进制资源、保存与手动构建；历史抽屉可还原包含上传资源的版本，ZIP 下载包含完整源文件。终端在当前项目目录中执行非交互命令并保存输出。各项目使用不同 Unix 用户和私有目录，不能读取其他项目工作区。

首页与项目续改对话均可上传最多 4 张 PNG、JPEG、WebP、GIF 图片和 8 份文档，附件总计不超过 40 MB；单张图片最大 5 MB，文本文档最大 2 MB，PDF/DOCX 最大 5 MB。支持 TXT、Markdown、JSON、CSV、HTML、CSS、JS、TS、PDF 和 DOCX；扫描版 PDF 若无法提取文字会明确报错。视频上传与识别暂不支持。附件原文件保存在对应项目工作区的 `.atoms-attachments` 目录，提取的文档文字和消息 ID 存在数据库中，Agent 可按文档 ID 分段读取全文；规划与编码阶段收到带文件名的文档摘要和可查询清单，不会将文档静默截断后当作完整输入。图片在构建前按文件名一起提交给视觉模型：所选模型支持图片时由该模型直接读取，文本模型则由本地 Ollama `qwen2.5vl:3b` 识图并转成文字上下文，再交给所选模型规划和编码。仅在 `.env` 设置 `ENABLE_LOCAL_VISION=true` 时，部署脚本调用 `vision-init` 拉取本地模型，模型文件保存在 `vision_models` 卷中。若图片识别失败，任务显示错误，不会默默丢弃图片。

Web 项目进入工作区时会在项目目录中启动真实开发服务，应用查看器通过项目专属代理打开服务，支持 Vite 热更新；可在预览栏重启、停止或配置启动命令。预览使用独立端口和只暴露预览路由的网关；页面保持不透明沙箱来源，`localStorage` 由按项目令牌隔离的持久存储接口提供，避免项目之间共享浏览器存储或访问主站会话。该兼容层将存储上限设为每项目 2 MB，`sessionStorage` 仅在当前页面内有效。公网 HTTPS 或自定义主机名部署时，设置 `VITE_PREVIEW_ORIGIN` 为与主站不同的 HTTPS 域名；不要把预览端口反代到主站同源。默认检测 `package.json` 中的 `dev`/`start` 脚本。其他类型的 HTTP 服务可在项目根目录创建 `.atoms-workspace.json`：`{"dev":"python -m http.server $PORT --bind 127.0.0.1","build":"python -m compileall -q src"}`。服务必须监听环境变量 `$PORT` 指定的端口；`$BASE_PATH` 是代理路径。非 Web 项目没有浏览器预览，但仍可使用文件、终端、智能体、测试和版本功能。Python 依赖可装到 `.python-packages`，运行时已将其加入 `PYTHONPATH`。

项目构建优先执行 `.atoms-workspace.json` 的 `build` 命令，否则执行 `package.json` 的 `build` 脚本；没有构建命令时不强行要求 TypeScript/Vite。构建产物预览仍可服务完整 `dist`，包括拆分模块和静态资源；预览采用临时令牌和沙箱隔离，运行错误会在构建页提示。发布时完整上传 `dist` 到 MinIO 的 `atoms-published` bucket，每个项目和每次发布使用独立对象前缀。PostgreSQL 的 `published_objects` 记录每个资源对应的 bucket、对象键、路径、MIME 类型、大小和 ETag；公开访问路由根据这份清单从 MinIO 读取文件，因此静态资源链接不会依赖 Agent 工作区里的本地副本。新发布完成后数据库切换到新对象清单，旧对象会清理；撤发布时数据库访问记录会删除。后续开发不会悄悄修改已发布版本。浏览器 WebGL 被禁用时，3D 项目需要自行提供降级界面；截图中的 Three.js 项目已加入 2D 降级显示。

新项目只创建待决策的 `.atoms/ARCHITECTURE.md`，不提前写入固定 App.tsx。规划后 Agent 使用官方 CLI 初始化完整前端，并按页面、组件、业务、hooks、API client、types 分模块；需要服务端共享数据、权限、秘密凭据或服务端持久化时实现真实后端。`.atoms-workspace.json` 的 `services` 管理额外后端进程，分配独立端口、等待就绪后启动前端，并提供日志和一起停止的生命周期。`APP_DATA_DIR` 指向项目独立持久数据目录 `.atoms-data`，不包含在源码版本和 ZIP 中。默认前端输出根目录 `dist`；当前 MinIO 发布仍仅发布静态资源，生成的全栈项目需另行部署后端，Agent 应提供实际部署说明。所有架构和验收文档可在编辑器查看；ZIP 排除 `.atoms`。

代码编辑器使用本地打包的 Monaco，根据文件名、扩展名和脚本 shebang 自动识别语言，提供语法高亮、行号、代码折叠、括号匹配、查找替换和撤销重做。未知类型按纯文本显示。编辑器按项目和文件隔离文本模型，保留文件标签、未保存草稿及 Agent 写入后的实时刷新；构建过程中只读。编辑器与语言 Worker 按需从本站加载。

## 故障恢复与成果交付

模型请求使用持久化身份、完整请求检查点和真实用量结算。建立连接之前的 ConnectError/ConnectTimeout/PoolTimeout 会释放预留积分并安全重试；已发送请求后的读取超时、响应截断不能假定未计费，会复用原请求等待结果或供应商对账。恢复任务时不会因为新增检查点提示而重复发起未知状态的生成。

短期模型故障经过请求内重试后，同一个任务最多再自动恢复 3 次，间隔 30、60、120 秒；保存原需求、计划、源码、对话、工具结果和验证进度，不重新初始化项目。自动恢复沿用本任务 token、模型调用、执行轮次、时间和收尾预算；点击停止仍立即停止，删除中的项目不会恢复。永久配置/认证错误和已用完的恢复额度不会无休止重试。

模型暂不可用时先运行本地真实构建、服务启动或 CLI 演示验证；有可运行成果时保留预览，正常恢复继续完成原需求。恢复额度耗尽后只有真实检查通过才交付演示版本，启动检查与完整功能验收分别记录，无法运行的成果不会被标成成功。最终验收优先使用工作区显式命令和有效规划命令，不能仅凭 tests 目录存在就替换为 pytest。

文件生成类任务的应用查看器提供 dist 中 PPT、PDF、Office 文档等真实成果的下载按钮，项目下载包也包含这些成果；依赖、缓存、隐藏文件和越界符号链接不作为成果提供下载。

## Agent 服务架构

前端请求先到 `backend/main.py`：API 网关验证用户、保存项目与任务，并根据 `project_agents` 中持久化的项目归属地址转发给独立的 `backend/agent_service.py`。每个项目对应一个 `backend/project_worker.py` 容器，独占工作目录挂载、Docker 网络、PostgreSQL 行级权限账号和资源配额。规划角色 Mike 与编码角色 Alex 在对应项目容器中运行；任务、命令、构建与开发服务不在 API 进程执行。项目路径存入 `projects.workspace_path`，工作区及依赖缓存在持久化 volume 中；老项目保留原目录，首次进入记录准确路径，不复制或丢失数据。容器根文件系统只读，并限制内存、CPU、进程数及 Linux capabilities。

打开项目会复用运行中的容器，或启动已停止的容器；后者保留容器文件系统状态，通常恢复更快。空闲项目在租约到期后停止，热项目保留停止容器；冷项目在保留期后删除容器与网络，重进时根据持久化工作区和 `project_runtime_state.snapshot` 中记录的路径、版本与启动配置创建新容器。Snapshot 不是进程内存快照，开发服务会重新启动。运行容器和保留容器数量分别由 `MAX_RUNNING_PROJECTS`、`MAX_RETAINED_PROJECTS` 限制；调度器只回收无有效页面租约、无排队或运行任务的项目，资源不足且无法安全回收时返回 503。可用 `PROJECT_IDLE_SECONDS`、`PROJECT_COLD_SECONDS`、`PROJECT_HOT_SECONDS` 调整冷热策略。

PostgreSQL 通知通过 `/api/projects/<ID>/events` SSE 推送状态，前端断线自动重连并保留低频兜底刷新。预览 HTTP 与 WebSocket 均由网关转发到项目容器内对应的开发服务。默认 Compose 是单机部署；多集群时设置相同的高强度 `AGENT_SECRET`，通过私有网络把 `AGENT_SERVICES` 配成逗号分隔的 Agent 服务地址。新项目按 ID 分配，归属写入 `project_agents`；各集群必须访问同一 PostgreSQL 和**共享读写工作区存储**，并配置对应的 `WORKSPACE_VOLUME` 与 Docker 环境。当前不做跨集群自动故障转移：迁移前必须停止旧项目容器并更新 `project_agents.endpoint`，避免并发写入。Docker Socket 属高权限接口，Agent 服务只能部署在可信内部网络。网关仍直接处理文件编辑、静态产物和发布管理；这些管理操作尚未完全迁移为 Agent RPC。

## 已实现的流程

- 首页提示词输入、文本附件、主题与构建模式选项、语音输入（浏览器支持时）。
- 资源作品浏览、分类筛选、保存和基于作品开始新项目。
- 创建项目、继续对话、查看构建活动、停止构建、预览网页、编辑并下载源代码、重命名、收藏、删除项目。
- 项目任务与版本持久化；历史版本可还原。编辑源文件后可手动运行构建。
- 预览支持元素选择与 Ctrl/Cmd 多选，引用标签加入对话；支持双击或“修改文本”进行就地编辑，丢弃恢复预览，保存会携带元素 HTML、DOM 路径、父级上下文和原文本提交真实 Agent 修改源码并构建。元素引用随消息持久化，重新进入项目后仍可用于后续开发。
- 发布项目并获得公开链接，例如 `/api/public/<project-id>`；右上角按钮在发布时显示“发布中”，完成后显示“更新”；点击可打开发布管理窗口并更新线上版本。
- 浅色和深色模式，项目与对话持久化。

## 范围

这是可运行的独立 Demo，不连接 Atoms 的私有后端、计费、真实连接器或云部署。账户资料、偏好和积分兑换保存于 PostgreSQL；连接器授权和支付入口需配置自己的服务，未配置时明确提示，不会伪造连接或扣款；代码生成由 OpenRouter 实际处理。Coding Agent 已具备阶段状态、工具循环、真实构建测试和复核，但代码检索仍是关键词检索，项目记忆使用历史版本摘要，也没有多 Agent 并行协作或所有语言的预装工具链；不能等同于 Codex、Claude Code 或 Cursor 的全部能力。当前容器预装 Node.js 和 Python；其他语言需在后端镜像中安装相应工具链。公开作品卡片是 2026 年 10 月 1 日抓取的参考内容，缩略图来自公开页面，后续内容可能变化。没有把提供的登录 Cookie 写入项目。

## 主要入口

- `http://localhost:25173/zh/dashboard`：控制台首页
- `http://localhost:25173/zh/discover`：资源
- `http://localhost:25173/zh/my-projects`：我的项目
- `http://localhost:25173/zh/chat/<项目 ID>`：项目对话与代码工作区
- `http://localhost:28080/api/health`：后端状态（仅服务器本机可访问；端口由 `.env` 配置）
- `http://localhost:28080/api/models`：模型、上下文、参考价格与可用状态

界面参考：[Atoms 官方控制台介绍](https://help.atoms.dev/en/articles/12129546-dashboard-overview)、[输入区说明](https://help.atoms.dev/zh/articles/12129552-input-chat)、[项目对话](https://help.atoms.dev/zh/articles/12129550-project-chat)、[预览](https://help.atoms.dev/zh/articles/12129554-app-viewer)、[代码和文件](https://help.atoms.dev/zh/articles/12129559-code-and-files)。
## 文件引用和语音输入

项目对话输入 `@` 可按路径搜索并引用最多 10 个文本文件，支持方向键、Enter/Tab 选择和 Escape 关闭。引用随消息持久化；执行时 Agent 从当前项目工作区读取内容供规划与开发使用，历史消息保留引用路径。单文件上限 120 KB，每份引用提供前 6000 字符，剩余内容由文件工具读取。二进制文件请使用图片/附件上传。

语音入口目前因识别模型不在 model_list 中，会在调用供应商前提示未配置计费价格。原录音流程：首页和项目对话输入框都可以点击麦克风开始录音，再次点击停止，将 OpenRouter `openai/whisper-large-v3-turbo` 识别的文字追加到输入框，不自动发送。录音最多 2 分钟、10 MB。麦克风要求 HTTPS 或 localhost；局域网 HTTP 地址不能获取麦克风权限。后端使用 `TRANSCRIPTION_API_KEY`，未设置时使用 `AI_API_KEY`；`TRANSCRIPTION_BASE_URL` 默认 `https://openrouter.ai/api/v1`，通过 `/audio/transcriptions` 请求。原始录音不落盘，调用用量记录在 `speech_transcriptions`。

## 个人菜单与账户设置

左下角个人菜单提供设置、套餐、个人主页、兑换和亮色/暗色/跟随系统主题。点击菜单外部或按 Escape 关闭。账户设置通过 `?settings=profile`、`globalControl`、`workspaceGeneral`、`plan`、`cloudAiBalance`、`connectors`、`diskSpace` 路由访问；个人主页为 `/zh/profile`。头像、名称、工作区资料、默认模型和偏好保存到账号，头像限制为 2MB 的 PNG/JPEG/WebP。存储页面统计本账号真实项目工作区中的文件。

免费积分每天首次访问账户发放 15 积分，每个自然月最多发放 25 积分；实际到账均有流水。管理员也可签发一次性兑换码：

```bash
docker compose exec -T backend python create_credit_code.py --credits 10 --days 30
```

返回的兑换码仅显示一次，数据库只存 SHA-256 摘要。兑换时原子校验有效期和使用状态，成功后写入余额和流水。不存在内置通用兑换码。

支付采用本地模拟支付：充值和套餐先创建数据库订单，确认模拟成功后原子写入到账流水；模拟失败、取消或过期不加积分。重复支付、并发支付不会重复到账。订单历史支持继续支付、下载模拟收据，流水和模型调用明细支持分页查看。团队协作入口已移除。

计费配置以 `model_list` 为唯一模型及价格来源，默认 `openai/gpt-6-luna`。价格单位为美元 / 百万 token，固定 `$1 = 5 积分`，扣费公式为 `(输入 token × 输入价格 + 输出 token × 输出价格) / 1,000,000 × 5`，积分保留八位小数。输出包括供应商计入的推理 token；供应商实际成本另存用于审计，本服务按调用时锁定的 model_list 价格收费。

每次付费调用先冻结额度，额度覆盖该模型完整上下文输入及本次最大输出；余额不足返回 402，不发送模型请求。项目 worker 不持有供应商 API key，只能通过受认证的中央计费代理调用。代理保存真实 usage、供应商调用 ID、价格快照和扣费流水，同一个请求只结算一次。已消耗 token 的失败调用仍按真实用量计费。未返回真实用量时保留冻结，并每 15 秒使用 generation ID 向供应商查询对账；缺失供应商 ID 的未知请求保持待对账，不能凭估算扣费或退款。工具执行、本地构建、配置的本地视觉模型不收模型费用。未配置价格的付费模型会在发送请求前拒绝，包括不在 model_list 中的语音识别模型。

充值金额由服务器换算积分，客户端不能指定到账余额。月付套餐一次到账一个月积分；年付 Pro 优惠 18%、Max 优惠 21%，预付 12 个月后逐月真实到账，后台定时补发到期积分。套餐到期恢复 Free；取消套餐保留已有积分、停止之后的套餐发放。支付结果由本地确认页模拟，不连接真实支付网关，也不产生真实现金扣款。

GitHub 连接器支持真实 OAuth 授权、仓库列表、私有/公开仓库导入为新项目，以及把项目源文件提交到 GitHub 默认分支。首次部署需创建 GitHub OAuth App，并在 `.env` 配置 `GITHUB_CLIENT_ID`、`GITHUB_CLIENT_SECRET`、精确匹配的 HTTPS `GITHUB_CALLBACK_URL`、公开站点 `APP_PUBLIC_URL` 和使用 `openssl rand -base64 32` 生成的 `GITHUB_TOKEN_ENCRYPTION_KEY`。GitHub OAuth App 的授权回调地址必须与 OAuth 请求中的地址完全匹配。随后运行 `./start.sh`。授权使用一次性 `state` 与 PKCE S256；访问令牌使用 AES-GCM 在数据库中加密保存。断开连接会调用 GitHub 撤销应用授权，已导入的项目文件会保留。同步会创建常规 Git 提交，忽略 `.env`、私钥、依赖目录及构建产物；单次同步限制 200 个文件、25 MB，不会删除远端仓库中的其他文件。OAuth 请求 `repo` 作用域以支持私有仓库及写回提交，请仅连接可信的 Atoms 部署。

OAuth 服务入口由 `ACCOUNT_CONNECTOR_AUTH_URLS` JSON 配置。邮件投递及多语言文案尚未接入；相应偏好可以保存。不会对参考网站的账户执行付款或资料修改。

成功导入仓库会创建一个新的 Ready 项目，不会触发模型构建或积分扣费。已关联项目可在连接器页执行“提交到 GitHub”，将源文件作为一个新 commit 写入其默认分支；此操作不会重写历史。断开连接后令牌立即从本地数据库删除，重新连接后可继续使用已导入项目。

## 专家 skill

专家页位于 `/zh/experts`，支持详情与案例预览、收藏、对话选择，以及版本化 skill 的完整构建链路。维护方式和接口见 [专家说明](docs/experts.md)。

### 按用户目标交付与预览成果

规划区分 `task_type`（软件、演示文稿、报告、文档、表格、分析或混合任务）和执行类型 `application_type`。纯文件任务使用 `artifact`，不强制创建 Web 服务、数据库、软件 README 或单元测试。生成脚本是生产工具；用户成果通过 `deliverables` 指定实际 `dist/` 文件、格式、需求关联、最低页数/行数及正文检查。混合任务同时验证软件流程和文件成果，沿用原来的普通/深度/高级策略与预算。

最终文件验收读取实际正文、核对内容要求、检查可打开性并执行预览渲染；文件缺失、损坏、格式不符或正文缺少要求都不能通过。预览缓存按原文件 SHA-256 隔离，文件改变后重新转换。文件任务无需启动应用服务，验收不再显示终端日志作为用户成果。

- PPT/PPTX、Word/DOCX、PDF：LibreOffice（Office 文件）生成真实页面，PDFium 渲染为图片，支持翻页、跳页及原文件下载。中文字体随镜像安装；特殊字体/动画等 Office 特性以原文件为准。
- Excel/XLSX、CSV：按工作表、行/列分批查看真实数据。Excel 显示保存的公式计算结果；没有结果缓存时明确显示原公式。XLSX 另支持 Calc 计算后的版式预览，包含图表。
- Markdown/文本、HTML 报告、图片、ZIP：正文阅读、沙箱 HTML 预览、图像预览或文件包目录，并下载原文件。

预览、分页和下载 API 全部验证项目所属用户。文件预览最大 100MB，Office 解压最大 200MB，转换最多 90 秒，最多两个转换并行。转换使用独立临时配置且禁止宏执行；PDFium 渲染串行，每页最多 800 万像素。预览错误提供重试和原文件下载，不伪造成功或空白预览。
