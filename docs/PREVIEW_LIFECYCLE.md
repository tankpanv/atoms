# 继续构建与预览加载状态

2026-10-09 的穿搭项目在 16:37:42 返回预览 HTML，浏览器到 16:37:51 才收到 `App.tsx`。旧诊断脚本在 HTML 加载后固定等待 4 秒，把尚未完成模块加载的空 `#root` 判成“应用未挂载”。应用随后实际挂载，但诊断协议没有成功或恢复事件，工作页一直保留错误。生成阶段的浏览器验证与用户浏览器是两个独立加载过程，前者成功不能证明后者的模块已经加载完毕。

当前链路分为生成验证、开发服务 HTTP 就绪、当前浏览器加载、当前应用挂载四个状态：

1. 项目与任务状态在同一次补读中提交，单个接口失败仍应用另一个接口的结果。完成任务 ID 触发预览更新，无需同时通过 HTML 变化再刷新一次。
2. 普通启动请求在执行时合并，启动失败最多重试一次，不通过重启进程修复浏览器加载延迟。服务在 HTTP 就绪前返回 `starting`，轮询不会把启动中的服务当成已停止。
3. 预览网关在应用脚本之前注入诊断，替换旧 worker 注入的诊断。实时预览、构建产物、没有 `<head>` 的 HTML 使用相同协议。
4. 浏览器上报 `loading`；`#root`、`#app` 或静态页面实际出现内容后上报 `ready`。工作页在此期间显示“正在加载应用，请稍候…”。应用挂载等待 30 秒仍未完成才报 `mount_timeout`，此后继续观察挂载并允许恢复。没有诊断消息的连接也有 30 秒超时。
5. `ready` 只清除加载超时。脚本异常、未处理的 Promise 拒绝、脚本或样式资源失败为真实运行错误，不因页面局部出现内容而清除。加载超时不会伪造控制台 JavaScript 错误。

还原时，浏览器验证沿用历史 `dev_command`。如果验证擅自重新检测默认命令，可能停止刚恢复的服务，导致还原成功结果中的 token 已失效。回归检查历史自定义命令在验证后仍然生效，返回地址与当前运行服务相同。

消息包含 `frameId`、`documentId`、`documentStartedAt`。父页面先检查消息来自当前 iframe，再拒绝旧帧或旧文档消息。预览 URL 或刷新编号改变时替换 iframe；内部跳转丢失查询参数时，新文档向父页面握手获取当前帧上下文。新文档的加载会清除上一文档的诊断。版本还原的预览绑定完成任务 ID，继续生成后不会复用旧的还原地址。

验证：

```sh
PYTHONPATH=backend:backend/tests python -m unittest test_preview_diagnostics test_preview_fullstack test_project_runtime.RuntimeTests test_project_restoration test_job_recovery
node frontend/node_modules/vite/bin/vite.js frontend --config frontend/tests/vite.config.ts
WORKSPACE_TEST_URL=http://localhost:25179/tests/workspace.html PYTHONPATH=backend:backend/tests python -m unittest test_workspace_sync_browser
cd frontend && npm run build
```

Chromium 回归覆盖模块延迟超过 4 秒、挂载超时后的恢复、真实脚本和资源失败、静态页面、续建响应错序、旧消息过滤、内部页面跳转和还原后的继续生成。服务生命周期测试覆盖 HTTP 就绪之前不能代理预览。

部署需同步更新前端和预览网关；新的网关可以替换旧 worker 的诊断脚本，避免活跃项目仍执行旧的 4 秒误判。后端和 worker 镜像的更新另外使服务 `starting` 状态生效。本地构建与回归不等于正在运行的容器已经更新。
