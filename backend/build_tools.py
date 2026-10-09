"""User-selected optional build tools. Selection is snapshotted per job."""
from typing import Literal

BuildTool = Literal['browser_check']


def normalize_tools(value):
    if value is None:
        return []
    if not isinstance(value, list) or any(v != 'browser_check' for v in value):
        raise ValueError('不支持的构建工具')
    return list(dict.fromkeys(value))


def browser_enabled(plan):
    # Legacy direct harness callers retain their previous behavior. Every
    # production job explicitly stamps a selection (default []), including
    # continuations of older projects/plans.
    return 'browser_check' in plan.get('enabled_tools', ['browser_check'])


def stamp_tools(plan, tools):
    return plan if tools is None else {**plan, 'enabled_tools': normalize_tools(tools)}


def api_acceptance_required(plan):
    """Explicit API requirements keep their acceptance contract in every tier."""
    return any(item.get('verification') == 'api' and item.get('origin', 'explicit') == 'explicit'
               for item in plan.get('requirements', []))


def tool_instructions(tools, build_tier=None):
    if 'browser_check' in normalize_tools(tools):
        return '\n用户已启用 browser_check，可用真实浏览器验证核心交互。'
    normal_demo = build_tier == 'normal'
    return ('\n用户未勾选浏览器验收：browser_check 禁用。不得调用或要求浏览器验收，'
            '不得通过 run_shell 自行启动 Playwright/Chromium 绕过用户选择。'
            '按 requirements.verification 和实际开放工具取得验收证据；可选浏览器工具未开启本身不是功能延期原因。'
            '已经通过当前验收合同的任务正常完成，交付说明另行列出未做完整浏览器交互检查，不能冒充 DOM 操作已验证。'
            + ('普通档优先真实构建、服务启动和页面 JavaScript 首屏；除非用户明确要求 API，不逐接口探测，未配置依赖记 TODO。'
               if normal_demo else
               '仍须完整实现需求、执行适用构建/业务测试、检查真实服务启动与 HTTP/API 可用性。'
               '页面交互需求使用针对性业务/组件测试及真实 API 结果验收，verification 可用 command/api。') +
            '平台最终交付仍强制用真实浏览器检查 JavaScript 启动、页面挂载和运行错误，再检查 HTTP/API；'
            '这是平台启动检查，不开放智能体 browser_check 工具、不生成完整交互场景，也不声称已验证浏览器交互。')
