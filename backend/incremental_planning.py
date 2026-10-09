"""Validated project baselines and bounded exploration for follow-up requests.

This is harness policy, not model memory: hashes decide which observations
remain valid, and a reserved synthesis phase cannot spend its turns reading.
"""
import copy
import ast
import json
import re

from agent_harness import validate_plan
from agent_session import encoded, inventory


def plan_object(content):
    # Never mistake a nested architecture/task object for a truncated outer
    # plan. Literal newlines inside strings are losslessly accepted, but missing
    # braces/values and other syntax errors still require a model correction.
    start = content.find('{')
    if start < 0:
        raise ValueError('模型未返回计划 JSON；请直接提交完整对象，保留输出预算')
    try:
        value, _ = json.JSONDecoder(strict=False).raw_decode(content[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f'计划 JSON 语法错误：{exc.msg}（位置 {exc.pos}）；请修复外层对象，不返回局部字段') from exc
    if not isinstance(value, dict):
        raise ValueError('计划必须为 JSON 对象')
    return value


def baseline_plan(root, session, sources):
    if not any(not name.startswith('.atoms/') for name in sources):
        return None
    candidates = [session.state.get('plan')]
    for name in ('task-state.json', 'requirements.json'):
        try:
            data = json.loads((root / '.atoms' / name).read_text())
            candidates.append(data.get('plan', data))
        except (OSError, ValueError, AttributeError):
            pass
    for candidate in candidates:
        try:
            return validate_plan(copy.deepcopy(candidate))
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
    return None


def change_contract(baseline):
    if not baseline:
        return ''
    stable = {key: baseline.get(key) for key in
              ('goal', 'application_type', 'architecture', 'commands', 'interfaces', 'task_type', 'deliverables')}
    return ('\n当前为已有项目增量开发。下面是历史计划，不是新增任务，源码优先于旧文档：\n' + encoded(stable) +
            '\n只为当前新增/修改要求生成 requirements/tasks/design，不重做已实现业务。'
            '未改变的 application_type/architecture/commands/task_type/deliverables 可省略，由执行器继承；'
            '架构或启动命令确需改变时显式输出该字段的完整对象。不要重新初始化已有目录。'
            '必须说明受影响的模块、API、数据迁移、兼容性及旧核心功能回归。'
            '新增注册登录须实现真实后端认证、密码安全、用户数据隔离及现有数据迁移策略，不能用假登录。'
            '读取只用于解决具体设计疑问；不要逐文件审阅、重复扫描或为了规划读完整测试集。')


def assemble_plan(value, baseline):
    if not isinstance(value, dict):
        raise ValueError('计划必须为 JSON 对象')
    plan = copy.deepcopy(value)
    # Empty optional command lists do not require another model call. Never
    # invent a build/start command, service, or successful verification.
    if isinstance(plan.get('commands'), dict):
        plan['commands'].setdefault('bootstrap', [])
        plan['commands'].setdefault('test', [])
    if baseline:
        for key in ('application_type', 'architecture', 'commands', 'task_type', 'deliverables'):
            if key not in plan:
                plan[key] = copy.deepcopy(baseline.get(key, [] if key == 'deliverables' else 'software' if key == 'task_type' else {}))
        # Reinitialization commands are never inherited into an existing app.
        if 'commands' not in value:
            plan['commands']['bootstrap'] = []
        plan['development_mode'] = 'incremental'
        plan['baseline_goal'] = baseline['goal']
        plan['preserve_requirements'] = copy.deepcopy(baseline.get('preserve_requirements', []))
        for requirement in baseline['requirements']:
            if requirement not in plan['preserve_requirements']:
                plan['preserve_requirements'].append(copy.deepcopy(requirement))
    return validate_plan(plan)


def verified_observations(session, sources, max_chars=8000, preferred_paths=None):
    """Reuse recent cached ranges only when their full source hash still matches."""
    current = inventory(sources)
    selected, used = {}, 0
    with session.connect() as conn:
        rows = conn.execute('SELECT path,sha,result FROM reads ORDER BY used_at DESC').fetchall()
    preferred = set(preferred_paths or [])
    rows.sort(key=lambda row: (row[0] not in preferred,
                              any(part in row[0].split('/') for part in ('tests', '__tests__', 'ui'))))
    for path, sha, result in rows:
        if preferred_paths is not None and path not in preferred:
            continue
        if any(part in path.split('/') for part in ('tests', '__tests__', 'ui')) and path not in preferred:
            continue
        if path in selected or current.get(path) != sha:
            continue
        rendered = f'[文件版本 {sha[:16]}]\n{result}'
        if used + len(rendered) > max_chars:
            continue
        selected[path] = {'path': path, 'sha': sha, 'text': rendered}
        used += len(rendered)
        if len(selected) >= 4:
            break
    return selected


def exploration_limit(incremental):
    return 3 if incremental else 6


def project_outline(sources, max_chars=10000):
    """Local syntax facts, no extra model calls or source bodies/secrets."""
    hashes = inventory(sources)
    rows, used = [], 0
    for path, text in sorted(sources.items()):
        if not isinstance(text, str) or not path.endswith(('.py', '.ts', '.tsx', '.js', '.jsx')):
            continue
        if any(part in path.split('/') for part in ('tests', '__tests__', 'ui')) or '.test.' in path or '.spec.' in path:
            continue
        names, routes = [], []
        if path.endswith('.py'):
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.append(node.name)
                    for decorator in getattr(node, 'decorator_list', []):
                        if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                                and decorator.func.attr in ('get', 'post', 'put', 'patch', 'delete')
                                and decorator.args and isinstance(decorator.args[0], ast.Constant)
                                and isinstance(decorator.args[0].value, str)):
                            routes.append(decorator.func.attr.upper() + ' ' + decorator.args[0].value)
        else:
            names = re.findall(r'\b(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|interface|type|const)\s+(\w+)', text)
        tables = re.findall(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z_][\w.]*)', text, re.I)
        row = encoded({'path': path, 'sha': hashes[path][:16], 'symbols': names[:18],
                       'routes': routes[:20], 'tables': list(dict.fromkeys(tables))[:12]})
        if used + len(row) > max_chars:
            break
        rows.append(row)
        used += len(row)
    return '\n'.join(rows)
