# 预置组件清单

来源：shadcn/ui 官方 new-york registry（https://ui.shadcn.com/r/styles/new-york/），使用官方 shadcn@2.3.0 CLI 安装并在模板内保存源码。此 CLI 是官方 Tailwind v3 安装文档指定版本：https://v3.shadcn.com/docs/installation/vite 。Tailwind 3.4 保留 tailwind.config.ts，与本项目参考规范一致。

兼容性适配：calendar 的新版样式写法转换为 Tailwind v3 的 has-[:focus] 与后代选择器；resizable 使用其现有 PanelGroup API 对应的 2.1.9，避免安装新版 API 引起运行错误。

源码与依赖由模板版本和 package-lock.json 固定；新项目无需下载组件或运行 shadcn init。此清单明确列出本版本支持的组件，不承诺自动包含今后官方新增组件。组合示例（如 combobox/data-table/date-picker）可用下面基础组件按业务实现。

从 @/components/ui/<name> 直接导入；cn 从 @/lib/utils 导入。例如 Button 从 button、Dialog/DialogContent/DialogTitle/DialogDescription 从 dialog；表单必须保持可访问名称、真实校验和错误反馈。组件库本身不包含产品业务，必须按 DESIGN.md 修改语义颜色/样式，并完成用户功能。

可用模块：

`accordion`, `alert-dialog`, `alert`, `aspect-ratio`, `avatar`, `badge`, `breadcrumb`, `button`, `calendar`, `card`, `carousel`, `chart`, `checkbox`, `collapsible`, `command`, `context-menu`, `dialog`, `drawer`, `dropdown-menu`, `form`, `hover-card`, `input-otp`, `input`, `label`, `menubar`, `navigation-menu`, `pagination`, `popover`, `progress`, `radio-group`, `resizable`, `scroll-area`, `select`, `separator`, `sheet`, `sidebar`, `skeleton`, `slider`, `sonner`, `switch`, `table`, `tabs`, `textarea`, `toast`, `toaster`, `toggle-group`, `toggle`, `tooltip`

依赖已锁定且预装到 worker 缓存，生产构建、类型检查和 Vitest 均使用同一 @/ 别名。只阅读确实要改动的组件；不要通读整个库。源码清单与 SHA-256 见 UI_COMPONENTS.json。
