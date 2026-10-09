# Python 项目隔离与安装故障修复

2026-10-09，针对项目 `fc942788ce8e478592008195ad745104` 的线上故障完成修复和验证。

## 故障原因

- 平台用 root 创建 `.python-packages`，安装命令随后降权到项目 UID，导致不可写。原有工作区归属修复跳过了该目录内的文件。
- 改装到 Worker 的 `/tmp` 只能绕过目录权限：该 tmpfs 默认 noexec，原生扩展无法映射加载；128 MB 容量也不足以容纳重复安装的临时文件。
- 模型随后把依赖装进 `.runtime-python-deps`，该目录被当成源码，触发 25 MB 版本快照限制。
- 之前的 `PYTHONPATH` 安装目标没有禁用平台 Python 的全局 site-packages，项目可能意外依赖镜像中的平台库。

## 当前隔离方式

调度器一直采用每项目一个 Worker 容器。每个 Worker 只挂载自己的工作区，并使用独立项目网络、只读镜像文件系统和资源配额。共享的是只读基础镜像中的 Python 3.12；各项目的可写依赖不共享。

本次增加项目独立 `.venv`，禁用系统和用户 site-packages。命令、构建和预览使用同一个项目 Python/pip、`.python-packages` 安装目标、`.python-cache` 缓存与临时目录。普通 `pip install` 和 `python -m pip install` 默认只写入当前项目。Shell 不再使用可能重置 PATH 的 login 模式。

安装前修复遗留目录归属；不跟随依赖目录的符号链接。依赖清单变化后在私有暂存目录中全新安装，成功后替换旧依赖，失败保留此前安装并允许重试。同一项目的托管依赖准备串行执行。构建和预览启动都会准备依赖；默认模板缓存独立复制给项目。

`.venv`、`.python-packages` 和 `.python-cache` 排除在源码快照、源码下载、项目克隆和 GitHub 导出之外。手动指定绝对解释器、改写 PATH/PYTHONPATH 或显式安装目标会覆盖默认环境；平台默认路径和自动准备流程始终使用上述私有环境。

主要实现：`backend/project_python.py`、`backend/agent.py`、`backend/runtime.py`、`backend/project_templates.py`。

## 线上项目恢复

已重建该项目的闲置 Worker，使用修复后的镜像；保留持久工作区和业务数据。旧临时安装目录和原配置保存在项目 `.python-cache/recovery-*` 中。

恢复 `.atoms-workspace.json` 缺失的结束括号，去掉后端命令中 `/tmp/qing-backend-pydeps` 的覆盖。修复 `backend/app/db.py` 启动时自行创建 Schema 的代码，改为确认平台已分配的 Schema；数据库角色权限没有扩大，原文件保存在 `.python-cache/database-startup-recovery/db.py`。

历史失败任务记录保留；不把失败任务改写为成功。通过新的实际构建保存恢复后的项目版本。

## 验证

- 23 个相关测试：22 个通过，1 个需要额外 CLI 下载的用例跳过。包含真实 pip 安装、两个不同版本、遗留目录归属、升级清理、失败重试、缓存复制、原生扩展加载、项目运行时、默认模板构建和超时回收。
- 两个真实调度器 Worker 分别安装同名包的 1.0.0 和 2.0.0，确认容器、可写挂载和网络独立。修改 A 项目的依赖后，B 仍使用 2.0.0；B 停止并重新启动后版本仍正确。两项目均无法通过默认 Python 导入平台的 FastAPI。临时项目、容器、数据库角色和工作区已清理。
- 原故障项目正式依赖安装、TypeScript/Vite 构建成功；项目 UID 下成功导入 FastAPI 0.115.12、psycopg 3.2.6、argon2 和 pydantic 原生扩展。使用项目 `.venv/bin/python`，用户 site-packages 关闭。
- 原故障项目后端就绪接口返回 200；恢复后源码快照约 0.7 MB。

复现专项测试：

```sh
docker compose exec -T backend sh -c 'cd /app/tests && PYTHONPATH=/app python -m unittest test_project_python test_project_runtime -v'
```

真实容器探针会创建并清理两个临时项目，不调用模型或 PyPI：

```sh
docker compose exec -T -e PYTHONPATH=/app agent-service python tests/live_python_isolation.py
```

扩展运行的历史测试集仍有过期 mock 接口错误（如缺失 `Gateway.model_for`）；在修复前镜像中抽查复现了同样错误，不声称全套历史测试通过。验证输出保存在本机 `.deploy/python-isolation-tests.log`、`.deploy/python-live-isolation.json` 和 `.deploy/project-python-recovery.json`。
