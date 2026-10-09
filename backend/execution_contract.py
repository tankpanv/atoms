"""Executable project facts: resolve defaults, validate commands, detect drift.

Natural-language design belongs in design/verification, never in a shell command
or directory. Workspace runner configuration is authoritative after development.
"""
from __future__ import annotations
import copy
import json
import re
import shlex
from pathlib import Path

POLICY = 'execution-contract-v1'


def validate_command(command, *, empty=False):
    if not isinstance(command, str) or (not command.strip() and not empty):
        raise ValueError('命令必须为实际可执行的非空 shell 字符串，不是操作说明')
    if not command.strip():
        return command
    if len(command) > 12000 or '\x00' in command:
        raise ValueError('命令长度超过 12000 字符或含 NUL')
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError as exc:
        raise ValueError('命令引号不完整：' + str(exc)) from exc
    # This is not an executable whitelist. Any shell/program/path is allowed;
    # reject prose/placeholders without banning Chinese arguments or filenames.
    first = next((v for v in tokens if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', v)), '')
    if (not first or re.match(r'^(按|沿用|使用|执行|运行|若|现有|根据|待确认|请)', first)
            or first.lower() in {'use', 'follow', 'according', 'todo', '<command>', 'tbd'}
            or (first == 'if' and not re.search(r'\bthen\b', command))):
        raise ValueError('命令字段含自然语言/占位说明。提供如 npm test、python -m unittest 的真实命令；说明写在 verification/design，不能发送给 shell')
    return command


def validate_directory(path):
    if (not isinstance(path, str) or not path or Path(path).is_absolute()
            or '..' in Path(path).parts or '\\' in path or '\n' in path
            or re.search(r'[；;。]|^(沿用|现有|新项目|若|待确认)', path)):
        raise ValueError('目录必须是具体项目相对路径（例如 frontend、backend 或 .），不能是条件或说明')
    return path


def workspace_commands(root):
    path = root / '.atoms-workspace.json'
    if not path.exists():
        return {}
    settings = json.loads(path.read_text())
    if not isinstance(settings, dict):
        raise ValueError('工作区配置必须为 JSON 对象')
    nested = settings.get('commands', {})
    if not isinstance(nested, dict):
        raise ValueError('工作区 commands 必须为对象')
    # Existing flat configs remain canonical. Accept the old nested shape
    # consistently for build/test/dev; a repair can override a frozen plan.
    return {**nested, **{k: settings[k] for k in ('dev', 'build', 'test') if k in settings}}


def resolve_default_plan(plan, prompt, history, *, new_project):
    from project_templates import requested_stack
    resolved = copy.deepcopy(plan)
    if not new_project or requested_stack(prompt, history) or plan.get('application_type') != 'web':
        return resolved
    architecture = resolved.setdefault('architecture', {})
    architecture['frontend'] = {'stack': 'Vite React TypeScript + shadcn/ui + Tailwind CSS', 'directory': 'frontend'}
    back = architecture.get('backend') or {}
    if back.get('required'):
        back.update(stack='FastAPI PostgreSQL', directory='backend')
    architecture['backend'] = back
    commands = resolved.setdefault('commands', {})
    commands.update(bootstrap=[], build=['npm run build'], dev='npm run dev')
    # Web acceptance is code/build + real service/core-flow verification.
    commands['test'] = []
    resolved['template_policy'] = POLICY
    # Keep the default plan lightweight and visibly ordered. This is metadata
    # for the planner/executor, not a license to skip backend work.
    resolved.setdefault('delivery_strategy', 'demo_first')
    resolved.setdefault('implementation_order', [
        'api_contract_and_demo_adapter',
        'frontend_preview_and_core_interactions',
        'backend_persistence_and_provider_wiring',
        'real_startup_and_core_workflow_acceptance',
    ])
    return resolved


def conformance_issues(root, plan, files):
    """Small structural checks, not an exhaustive source review or code grader."""
    if plan.get('application_type') != 'web':
        return []
    front = plan['architecture']['frontend']
    directory = Path(front['directory']).as_posix().strip('/')
    prefix = '' if directory in ('', '.') else directory + '/'
    issues = []
    # Plans may describe either the application root or its source directory.
    # In particular directory=src with src/App.tsx must never mean src/src.
    source_prefixes = (prefix,) if Path(directory).name == 'src' else (prefix+'src/', prefix+'app/', prefix+'pages/')
    modules = [n for n in files if n.startswith(source_prefixes) and n.endswith(('.tsx','.ts','.jsx','.js','.vue','.svelte'))
               and '/components/ui/' not in n and not n.endswith(('.test.ts','.test.tsx','.spec.ts','.d.ts'))]
    if files and not modules and any('/src/' in '/'+n for n in files):
        actual = sorted(n for n in files if '/src/' in '/'+n and n.endswith(('.tsx','.jsx','.vue','.svelte')))[:6]
        issues.append(f'计划前端目录 {front["directory"]} 未找到源码，实际入口候选：{", ".join(actual)}。'
                      '先核对 package.json、index.html 和实际启动/构建配置；保持已有业务，避免仅为计划文字搬移目录。')
    # Number of modules is a design preference, not functional acceptance.
    # Refactoring an already working small App here caused needless repair loops.
    manifest = root / '.atoms/template.json'
    if manifest.exists():
        package = root / 'frontend/package.json'
        try:
            meta = json.loads(package.read_text())
            deps = {**meta.get('dependencies', {}), **meta.get('devDependencies', {})}
            missing = [name for name in ('react','react-dom','vite','typescript','tailwindcss') if name not in deps]
            if missing:
                issues.append('已安装平台模板，但基线依赖被移除：'+', '.join(missing)+'。恢复并沿用模板，避免另写入口/脚手架。')
            if not (root/'frontend/src/pages/Index.tsx').exists():
                issues.append('平台模板的页面入口 frontend/src/pages/Index.tsx 缺失；确认路由与组件集成，不另建独立应用。')
        except (OSError, ValueError, TypeError):
            issues.append('平台模板 frontend/package.json 缺失或无效，先修复工程配置。')
    return issues
