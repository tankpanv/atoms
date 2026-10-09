# JavaScript 页面启动验收缺口（2026-10-06）

项目 f70ceb36-e7c8-4668-8e29-c6458c9d451a 构建通过、后端额度回读成功，但普通 HTTP 页面在初始化时直接调用 crypto.randomUUID，导致 React Index 组件崩溃。截图中的“未启用浏览器验收”意味着旧路径只请求 HTML 和 API，没有执行 JavaScript；因此“演示启动通过”的范围不足，结论不可信。

现在 Web 最终交付在未启用完整交互工具时，也必须用真实 Chromium 执行页面 JavaScript，等待可观察页面挂载，检查 pageerror、console error、资源/服务失败及空页面，再执行真实 HTTP/API 回读。失败直接反馈给开发循环定位修复，不用构建成功覆盖失败。模型 browser_check 工具仍受用户选择控制，启动检查不自动执行完整业务交互。

本地独立浏览器检查不再使用有安全上下文特殊待遇的 127.0.0.1，改用 Chromium 本地解析的普通 HTTP atoms-preview.test；实际服务仍经生产预览网关。结果包含真实 secure_context/origin/random_uuid 能力信息。启动策略版本进入验收指纹，旧 HTTP-only 成功结果无法作为新验收缓存复用。

模板新增 frontend/src/lib/id.ts，通过 crypto.getRandomValues 生成标准 UUID v4，兼容普通 HTTP，保留安全随机性。规划规范和模板文档说明安全上下文能力边界。该项目的匿名客户端 ID 及上传条目 ID 已改用同一实现，真实构建通过；真实预览网关中 secure_context=false、random_uuid=undefined 的页面也正常挂载，未捕获 JavaScript 错误。

新增真实回归链路：HTML 返回 200 → 运行随机 UUID 脚本失败 → 未启用交互工具的最终启动验收必须失败 → 修复 helper → 真实源码重启 → 普通 HTTP 验收通过。启动检查证明页面可以正常执行，不替代完整业务验收；AI 服务依赖是否可用仍须按系统合同验证。
