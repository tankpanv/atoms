"""Local unit checks support implementation; final acceptance verifies real products."""
import ast
import json
import re
from pathlib import Path

INSTRUCTIONS = '''
验证策略：生成或修改代码期间，可以编写、更新并运行针对当前改动的单元测试。运行时明确指定相关测试文件/用例，不运行整个项目测试套件；局部测试结果仅用于当次诊断，不作为最终验收证据，不为了过期断言改动正确业务。局部测试可用 mock；演示也允许模拟尚未配置的外部服务，但必须标注模拟模式、保留真实适配器和配置 TODO，不冒充真实服务验收。
以系统性代码检查为主：核对实际入口、导入/导出、调用方、前后端接口、数据流、持久化、错误处理及需求覆盖；利用编译/类型检查定位具体问题，不另建测试工程或反复通读无关文件。
最终验收不统一运行单元测试、不汇总历史单元测试结果。Web/service 的 commands.test 留空，build/dev/service 命令不能夹带测试套件；CLI/library 的 test 字段只放真实产品可执行验证，不使用单元测试运行器。
演示优先，最后必须在当前源码下启动实际服务和页面，尽可能验证已实现链路；浏览器可用时检查实际用户操作、真实请求和数据回读，否则直接验证真实 HTTP/API。构建通过和模拟结果不能当成完整真实业务验收；允许在真实页面启动通过后交付有缺失的演示，记录延期/模拟项，继续开发其他功能，不因未配置外部依赖停止整体项目。
检查失败先区分业务错误与验证步骤错误。根据真实接口和控件修正步骤；不为了错误断言改正确业务。重复同类失败会跨源码改动跟踪，并触发新的诊断视角、竞争假设和最小判别检查；不能因为失败次数或缺少证据直接终止。保留代码与真实证据，不伪造通过；总耗时、token 和用户停止仍是资源边界。
'''

UNIT_RUNNER = re.compile(r'\b(?:vitest|jest|pytest|unittest|mocha|ava)\b|\b(?:npm|pnpm|yarn|bun)\s+(?:--prefix\s+\S+\s+)?(?:run\s+)?test(?:\s|$)', re.I)
MOCK_CODE = re.compile(r'\b(?:vi|jest)\s*\.\s*(?:mock|fn|spyOn|stubGlobal)\s*\(|\b(?:unittest\.mock|from\s+unittest\s+import\s+mock)\b')
UNIT_CODE = re.compile(r'\b(?:import\s+(?:unittest|pytest)|from\s+(?:unittest|pytest)\s+import)\b|(?:from\s*|require\s*\()[\"\'](?:vitest|@testing-library/[^\"\']+)[\"\']')


def unit_test_path(path):
    path = Path(path)
    return (any(part in ('tests', '__tests__') for part in path.parts)
            or bool(re.search(r'(?:^test_.+\.py$|.+_test\.py$|.+\.(?:test|spec)\.[cm]?[jt]sx?$)', path.name)))


def command_issue(command, root, visited=None, *, local=False, targeted=False):
    """Inspect aliases as well as inline commands; existing tests are preserved."""
    if local:
        # Each chained test invocation must select an actual file/module/case.
        for part in re.split(r'&&|\|\||[;\n]', command):
            if re.match(r'\s*(?:(?:npm|pnpm|yarn|bun)\s+(?:install|add)|(?:python[\d.]*\s+-m\s+)?pip[\d.]*\s+install)\b', part):
                continue  # Installing a runner does not execute its suite.
            part = re.sub(r'--(?:config|configFile)(?:=|\s+)\S+', '', part)
            if UNIT_RUNNER.search(part) or MOCK_CODE.search(part) or UNIT_CODE.search(part):
                explicit = targeted or bool(re.search(
                    r'[\w./-]+\.(?:py|[cm]?[jt]sx?)\b|\b(?:tests?\.[\w.]+|test_[\w.]+)\b', part))
                if not explicit:
                    return '局部单元测试必须明确指定本次改动相关的测试文件/用例，不运行整个项目测试套件。'
        targeted = targeted or bool(re.search(r'[\w./-]+\.(?:py|[cm]?[jt]sx?)\b|\b(?:tests?\.[\w.]+|test_[\w.]+)\b', command))
    if not local and (UNIT_RUNNER.search(command) or MOCK_CODE.search(command) or UNIT_CODE.search(command)):
        return '最终构建/启动/验收不运行单元测试；局部测试仅在实现期间按文件/用例运行，交付使用真实产品链路。'
    for match in re.finditer(r'\b(?:python[\d.]*|node|tsx|ts-node)\s+([\w./-]+\.(?:py|[cm]?[jt]s))\b', command):
        script = Path(root) / match.group(1)
        try:
            if script.resolve().is_relative_to(Path(root).resolve()):
                content = script.read_text()
                if not local and (unit_test_path(script) or MOCK_CODE.search(content) or UNIT_CODE.search(content)):
                    return '验收命令引用了单元测试脚本；改为执行真实产品及真实链路检查。'
        except (OSError, ValueError):
            pass
    visited = set() if visited is None else visited
    if len(visited) >= 12:
        return '验证命令别名递归过深，请使用直接的真实检查命令。'
    # Resolve common package-script wrappers, including root -> frontend aliases.
    for match in re.finditer(r'\b(npm|pnpm|yarn|bun)\s+(?:(?:--prefix|-C|--cwd|--dir)(?:\s+|=)(\S+)\s+)?(?:run\s+)?([\w:-]+)', command):
        directory, script = match.group(2) or '.', match.group(3)
        if script in ('install', 'ci', 'exec', 'add', 'create', 'init', 'build'):
            # Build scripts still need inspecting; dependency commands do not.
            if script != 'build':
                continue
        working = Path(root)
        for change in re.finditer(r'(?:^|[;&\n])\s*cd\s+([^;&\n]+)', command[:match.start()]):
            working = working / change.group(1).strip().strip('\"\'')
        package = working / directory.strip('\"\'') / 'package.json'
        try:
            resolved = package.resolve()
            if not resolved.is_relative_to(Path(root).resolve()):
                return '命令引用了项目外的 package.json。'
            scripts = json.loads(package.read_text()).get('scripts', {})
        except (OSError, ValueError, AttributeError):
            continue
        body = scripts.get(script)
        key = (str(resolved), script)
        if isinstance(body, str) and key not in visited:
            visited.add(key)
            issue = command_issue(body, package.parent, visited, local=local, targeted=targeted)
            if issue:
                return issue
    return None


def tool_issue(name, args, root):
    if name in ('run_build', 'runtime_check', 'http_request', 'browser_check'):
        config = Path(root) / '.atoms-workspace.json'
        try:
            settings = json.loads(config.read_text()) if config.exists() else {}
        except (ValueError, OSError):
            return None  # The real structural check reports invalid configuration.
        if not isinstance(settings, dict):
            return None
        operation = 'build' if name == 'run_build' else 'dev'
        nested = settings.get('commands', {})
        configured = settings.get(operation) or (nested.get(operation) if isinstance(nested, dict) else None)
        commands = ([configured] if isinstance(configured, str) else configured
                    if isinstance(configured, list) else args.get('planned_commands') or [f'npm run {operation}'])
        if name != 'run_build':
            services = settings.get('services', [])
            if isinstance(services, list):
                commands = [*commands, *(service.get('command', '') for service in services if isinstance(service, dict))]
        for command in commands:
            if isinstance(command, str) and (issue := command_issue(command, root)):
                return issue
    elif name == 'run_shell':
        return command_issue(args.get('command', ''), root, local=True)
    # File tools may create/update tests for the current implementation change.
    return None


def inspect_sources(files):
    """Deterministic structural checks; generated tests cannot gate delivery."""
    issues = []
    checked = 0
    from project_snapshots import text_content
    for name in files:
        if not name.endswith('.py') and Path(name).name not in ('package.json', '.atoms-workspace.json', 'tsconfig.json'):
            continue
        content = text_content(files, name)
        if not isinstance(content, str) or name.startswith('.atoms/') or unit_test_path(name):
            continue
        try:
            if name.endswith('.py'):
                ast.parse(content, filename=name)
                checked += 1
            elif Path(name).name in ('package.json', '.atoms-workspace.json', 'tsconfig.json'):
                # tsconfig permits comments; the TypeScript build validates it.
                if Path(name).name != 'tsconfig.json':
                    value = json.loads(content)
                    if not isinstance(value, dict):
                        raise ValueError('配置根节点必须是对象')
                    checked += 1
        except (SyntaxError, ValueError) as error:
            issues.append(f'{name}: {error}')
    return (1 if issues else 0), (f'系统代码检查：已检查 {checked} 个 Python/JSON 文件；JS/TS 类型与导入由真实构建检查。\n' + '\n'.join(issues))
