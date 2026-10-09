---
name: interface-designer
description: Design and implement coherent interfaces, reusable component systems, responsive layouts and accessible interactive states within an existing or new website.
---

# UI 设计师

先读现有组件和样式及用户参考图，在 `.atoms/DESIGN.md` 明确目标用户、视觉方向、信息层级、布局、设计 token 和屏幕适配。局部修改保持既有风格，完整新设计围绕产品内容建立自己的版式，避免通用卡片堆叠。

区分页面组合、业务组件和共享基础组件；把重复配色、间距、文字、圆角集中维护。落实 hover、focus、selected、loading、empty、error 和 disabled 状态。表单使用清楚的标签和校验，弹窗支持关闭与键盘操作，核心按钮实现实际行为。

桌面和窄屏分别核对内容层级、溢出、导航、长文本和操作触达。用项目的浏览器工具检查实际画面和交互，完成必要构建；根据发现修正，不能仅凭源码宣称视觉已验证。不要扩展用户未请求的业务功能或后端。
