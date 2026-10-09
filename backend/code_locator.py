"""Incremental local syntax facts plus on-demand Glob/Grep/Read navigation.

Facts locate candidates, not prove behavior. Model queries provide semantic
keywords; imports are a static approximation, and real tests prove correctness.
"""
import ast
import base64
import fnmatch
import hashlib
import json
import re
import subprocess
from functools import lru_cache
from pathlib import PurePosixPath

from agent_session import SourceManifest, encoded

EXTENSIONS = ('.py', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.vue', '.svelte')
DISCOVERY_TOOLS = [
    {'type': 'function', 'function': {'name': 'locate_change', 'description': 'Locate candidate change sites using query keywords you derive from the CURRENT user request (include English symbols and Chinese UI terms). Returns current symbols/routes/line ranges and static imports/importers. No source bodies or invented semantic matches. Follow with scoped search_code/read_code; expand queries if coverage is incomplete.', 'parameters': {'type': 'object', 'properties': {'queries': {'type': 'array', 'minItems': 1, 'maxItems': 12, 'items': {'type': 'string'}}, 'include': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20}}, 'required': ['queries']}}},
    {'type': 'function', 'function': {'name': 'glob_files', 'description': 'Find project paths by glob, e.g. **/*auth* or frontend/src/**/*.tsx. Returns filenames, not source bodies; supports pagination.', 'parameters': {'type': 'object', 'properties': {'pattern': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, 'required': ['pattern']}}},
    {'type': 'function', 'function': {'name': 'search_code', 'description': 'Grep current source using a literal or regular expression. Scope with include glob. Returns bounded real line matches and offsets, not whole files. Use to find references, UI labels, request paths and data usage; no matches means broaden the search.', 'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}, 'include': {'type': 'string'}, 'regex': {'type': 'boolean'}, 'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 80}}, 'required': ['query']}}},
    {'type': 'function', 'function': {'name': 'read_code', 'description': 'Read a current source range by 1-based start_line, or an indexed symbol name. Returns source hash, actual line range and character offsets for existing edit tools. Bound max_lines and continue only when needed; verify relevant implementation before patching.', 'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'symbol': {'type': 'string'}, 'start_line': {'type': 'integer', 'minimum': 1}, 'max_lines': {'type': 'integer', 'minimum': 1, 'maximum': 160}}, 'required': ['path']}}},
]
DISCOVERY_NAMES = {tool['function']['name'] for tool in DISCOVERY_TOOLS}


def matches(path, pattern):
    if not pattern:
        return True
    parts, patterns = path.split('/'), pattern.split('/')
    @lru_cache(None)
    def match(i, j):
        if j == len(patterns):
            return i == len(parts)
        if patterns[j] == '**':
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return i < len(parts) and fnmatch.fnmatchcase(parts[i], patterns[j]) and match(i + 1, j + 1)
    return match(0, 0)


def facts(path, text):
    result = {'symbols': [], 'routes': [], 'tables': [], 'imports': [], 'parser': 'lexical'}
    if path.endswith('.py'):
        try:
            tree = ast.parse(text)
            result['parser'] = 'python-ast'
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    result['symbols'].append({'name': node.name, 'line': node.lineno, 'end_line': node.end_lineno})
                    for decorator in getattr(node, 'decorator_list', []):
                        if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                                and decorator.func.attr in ('get', 'post', 'put', 'patch', 'delete')
                                and decorator.args and isinstance(decorator.args[0], ast.Constant)
                                and isinstance(decorator.args[0].value, str)):
                            result['routes'].append({'method': decorator.func.attr.upper(), 'path': decorator.args[0].value, 'line': decorator.lineno})
                if isinstance(node, ast.ImportFrom):
                    if node.module:
                        result['imports'].append('.' * node.level + node.module)
                    else:
                        result['imports'].extend('.' * node.level + alias.name for alias in node.names)
                elif isinstance(node, ast.Import):
                    result['imports'].extend(alias.name for alias in node.names)
        except (SyntaxError, ValueError, RecursionError):
            result['parser'] = 'unparsed'
    elif path.endswith(EXTENSIONS):
        pattern = r'\b(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|interface|type|const)\s+(\w+)'
        for match in re.finditer(pattern, text):
            prefix = text[:match.start()]
            result['symbols'].append({'name': match.group(1), 'line': prefix.count('\n') + prefix.count('\r') - prefix.count('\r\n') + 1})
        result['imports'] = re.findall(r'(?:\bfrom\s*|\bimport\s*\(|\brequire\s*\(|\bimport\s*)[\'"]([^\'"\n]+)[\'"]', text)
    result['symbols'] = result['symbols'][:200]
    result['tables'] = list(dict.fromkeys(re.findall(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z_][\w.]*)', text, re.I)))[:60]
    return result


class CodeLocator:
    def __init__(self, root, session):
        self.root, self.session = root, session
        self.entries = {}
        with session.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS source_index(path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, sha TEXT NOT NULL, data TEXT NOT NULL)')

    def sync(self):
        from agent import list_files, safe_file, MAX_FILE_BYTES, MAX_SNAPSHOT_BYTES
        names = list_files(self.root)
        stats = {'files': len(names), 'reindexed_files': 0, 'reused_files': 0, 'bytes_read': 0}
        total = 0
        with self.session.connect() as conn:
            old = {row[0]: row[1:] for row in conn.execute('SELECT path,fingerprint,sha,data FROM source_index')}
            for name in names:
                path = safe_file(self.root, name)
                stat = path.stat()
                total += stat.st_size
                if total > MAX_SNAPSHOT_BYTES:
                    raise ValueError('项目文件超过版本快照大小限制（25 MB）')
                fingerprint = encoded([2, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino])
                if name in old and old[name][0] == fingerprint:
                    sha, data = old[name][1], json.loads(old[name][2])
                    stats['reused_files'] += 1
                else:
                    raw = path.read_bytes()
                    stats['bytes_read'] += len(raw)
                    try:
                        text = raw.decode('utf-8')
                        if '\x00' in text or len(raw) > MAX_FILE_BYTES:
                            raise UnicodeError()
                        sha = hashlib.sha256(text.encode()).hexdigest()
                        data = facts(name, text)
                        data['text'] = True
                    except UnicodeError:
                        sha = hashlib.sha256(encoded({'encoding': 'base64', 'content': base64.b64encode(raw).decode()}).encode()).hexdigest()
                        data = {'text': False, 'symbols': [], 'routes': [], 'tables': [], 'imports': []}
                    conn.execute('INSERT OR REPLACE INTO source_index VALUES(?,?,?,?)', (name, fingerprint, sha, encoded(data)))
                    stats['reindexed_files'] += 1
                self.entries[name] = {'path': name, 'sha': sha, **data}
            for removed in old.keys() - set(names):
                conn.execute('DELETE FROM source_index WHERE path=?', (removed,))
                self.entries.pop(removed, None)
        self.stats = stats
        self.dependencies = {path: list(dict.fromkeys(target for spec in entry['imports']
                              if (target := self.resolve_import(path, spec)))) for path, entry in self.entries.items()}
        self.importers = {path: [] for path in names}
        for path, dependencies in self.dependencies.items():
            for target in dependencies:
                self.importers[target].append(path)
        return SourceManifest({name: row['sha'] for name, row in self.entries.items() if not name.startswith('.atoms/')},
                              {name: {'indexed': True} for name in names})

    def resolve_import(self, path, spec):
        parent = PurePosixPath(path).parent
        if path.endswith('.py'):
            dots = len(spec) - len(spec.lstrip('.'))
            base = parent
            for _ in range(max(0, dots - 1)):
                base = base.parent
            stem = str(base / spec[dots:].replace('.', '/')) if dots else spec.replace('.', '/')
        elif spec.startswith('.'):
            parts = list(parent.parts)
            for part in spec.split('/'):
                if part == '..':
                    if not parts:
                        return None
                    parts.pop()
                elif part != '.':
                    parts.append(part)
            stem = '/'.join(parts)
        elif spec.startswith('@/') and '/src/' in path:
            stem = path.split('/src/')[0] + '/src/' + spec[2:]
        else:
            return None
        candidates = [stem, *[stem + suffix for suffix in EXTENSIONS], stem + '/__init__.py',
                      *[stem + '/index' + suffix for suffix in ('.ts', '.tsx', '.js', '.jsx')]]
        direct = next((candidate for candidate in candidates if candidate in self.entries), None)
        if direct or not path.endswith('.py') or spec.startswith('.'):
            return direct
        # Python apps commonly run from backend/ with absolute app.* imports.
        # Resolve a unique actual file only; ambiguous package roots need grep.
        nested = [name for name in self.entries if any(name.endswith('/' + candidate) for candidate in candidates)]
        return nested[0] if len(nested) == 1 else None

    def overview(self, max_chars=4500):
        rows, size = [], 0
        paths = sorted(self.entries, key=lambda p: (any(part in p.split('/') for part in ('tests', '__tests__', 'ui')), p))
        for path in paths:
            entry = self.entries[path]
            if not path.endswith(EXTENSIONS) or any(part in path.split('/') for part in ('tests', '__tests__', 'ui')) or '.test.' in path:
                continue
            row = encoded({'path': path, 'symbols': [s['name'] for s in entry['symbols'][:8]], 'routes': entry['routes'][:6], 'tables': entry['tables'][:8]})
            if size + len(row) > max_chars:
                continue
            rows.append(row); size += len(row)
        return '\n'.join(rows)

    def locate(self, queries, include='', limit=12):
        if not queries or len(queries) > 12 or any(not isinstance(q, str) or not q.strip() or len(q) > 120 for q in queries):
            raise ValueError('queries 需提供 1–12 个非空搜索词，每项不超过 120 字符')
        found = []
        for path, entry in self.entries.items():
            if not matches(path, include) or not entry['text']:
                continue
            haystack = encoded({key: entry[key] for key in ('symbols', 'routes', 'tables')}).casefold()
            reasons = [q for q in queries if q.casefold() in path.casefold() or q.casefold() in haystack]
            if reasons:
                found.append((sum(3 if q.casefold() in path.casefold() else 1 for q in reasons), path, reasons))
        found.sort(key=lambda item: (-item[0], item[1]))
        candidates = []
        for score, path, reasons in found[:limit]:
            entry = self.entries[path]
            candidates.append({**{key: entry[key] for key in ('path', 'sha', 'parser') if key in entry},
                               'symbols': entry['symbols'][:24], 'routes': entry['routes'][:12], 'tables': entry['tables'][:20],
                               'matched_queries': reasons, 'imports': self.dependencies[path][:20],
                               'imported_by': self.importers[path][:20]})
        return encoded({'candidates': candidates, 'matches': len(found), 'truncated': len(found) > limit,
                        'next': '按行或符号读取候选实现；用 search_code 验证调用/API 路径/界面文案。无命中请换术语或 glob_files，不能据此断言功能不存在。',
                        'dependency_scope': 'static imports only; dynamic calls/API consumers require grep and runtime verification'})

    def glob(self, pattern, offset=0, limit=50):
        if not pattern or len(pattern) > 200 or '..' in PurePosixPath(pattern).parts or pattern.startswith('/'):
            raise ValueError('pattern 必须为项目内 glob')
        names = [path for path in self.entries if matches(path, pattern)]
        return encoded({'paths': names[offset:offset + limit], 'matches': len(names), 'next_offset': offset + limit if len(names) > offset + limit else None})

    def grep(self, query, include='', regex=False, offset=0, limit=40):
        from agent import safe_file
        if not query.strip() or len(query) > 200:
            raise ValueError('搜索词长度需为 1–200 个字符')
        paths = [p for p, entry in self.entries.items() if entry['text'] and matches(p, include) and not p.startswith('.atoms/')]
        for path in paths:
            safe_file(self.root, path)
        rows, truncated = [], False
        # Spawn rg with argv, bounded time and project snapshot size, never a
        # shell. Only the requested page is sent to the model.
        command = ['rg', '--json', '--no-ignore', '--hidden', '--max-columns', '1000', '-i']
        if not regex:
            command.append('-F')
        command.extend(['-e', query, '--', *paths])
        if paths:
            try:
                result = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=4)
            except subprocess.TimeoutExpired as exc:
                raise ValueError('搜索超过 4 秒，请缩小 include 范围或简化表达式') from exc
            if result.returncode not in (0, 1):
                raise ValueError('搜索表达式无效：' + result.stderr[:300])
            for line in result.stdout.splitlines():
                record = json.loads(line)
                if record['type'] != 'match':
                    continue
                data = record['data']
                path, number = data['path']['text'], data['line_number']
                rows.append({'path': path, 'line': number, 'text': data['lines']['text'].rstrip()[:600], 'sha': self.entries[path]['sha']})
            truncated = len(rows) > offset + limit
        return encoded({'matches': rows[offset:offset + limit], 'total_matches': len(rows),
                        'truncated': truncated, 'next_offset': offset + limit if truncated else None})

    def read(self, args, messages):
        from agent import safe_file, MAX_FILE_BYTES
        path = args['path']
        target = safe_file(self.root, path)
        if not target.is_file() or target.stat().st_size > MAX_FILE_BYTES:
            raise ValueError('文件不存在或超过源码读取大小限制')
        text = target.read_bytes().decode('utf-8')
        if '\x00' in text:
            raise ValueError('二进制文件不能作为源码读取')
        lines = text.splitlines(keepends=True)
        start = args.get('start_line', 1)
        if args.get('symbol'):
            definitions = [s for s in self.entries.get(path, {}).get('symbols', []) if s['name'] == args['symbol']]
            if len(definitions) != 1:
                raise ValueError('符号未找到或有多个定义；先 locate_change/search_code 确认具体行号')
            start = definitions[0]['line']
        if start > max(1, len(lines)):
            raise ValueError('start_line 超出文件实际行数')
        end = min(len(lines), start + args.get('max_lines', 100) - 1)
        offset = sum(map(len, lines[:start - 1]))
        length = min(12000, len(''.join(lines[start - 1:end])))
        actual_end = start + len(text[offset:offset + length].splitlines()) - 1
        observed = self.session.read(path, text, offset, max(1, length), messages)
        return f'代码行 {start}–{max(start, actual_end)} / {len(lines)}；字符 offset={offset}，limit={length}\n{observed}', text, offset

    def execute(self, name, args, messages):
        if name == 'locate_change':
            return self.locate(args['queries'], args.get('include', ''), args.get('limit', 12))
        if name == 'glob_files':
            return self.glob(args['pattern'], args.get('offset', 0), args.get('limit', 50))
        if name == 'search_code':
            return self.grep(args['query'], args.get('include', ''), args.get('regex', False), args.get('offset', 0), args.get('limit', 40))
        if name == 'read_code':
            return self.read(args, messages)[0]
        raise ValueError('未知代码定位工具')


def validate_change_map(plan, locator):
    mapping = plan.get('change_map')
    if not isinstance(mapping, list) or not 1 <= len(mapping) <= 60:
        raise ValueError('增量计划须提供 change_map：每个文件的 path、operation(modify/create/delete)、reason、requirement_ids；先定位具体改动位置')
    required = {r['id'] for r in plan['requirements']}
    covered, seen, verified = set(), set(), []
    for change in mapping:
        if not isinstance(change, dict):
            raise ValueError('change_map 每项须为对象')
        path = change.get('path')
        if (not isinstance(path, str) or path in seen or path.startswith('/')
                or '..' in PurePosixPath(path).parts or path != str(PurePosixPath(path))):
            raise ValueError('改动路径必须为不重复的项目内具体文件')
        from agent import safe_file
        target = safe_file(locator.root, path)
        if target.is_dir() or not PurePosixPath(path).name or path.endswith('/'):
            raise ValueError('change_map 必须定位具体文件，不能使用目录')
        seen.add(path)
        entry = locator.entries.get(path)
        operation = change.get('operation')
        if operation not in ('modify', 'create', 'delete') or not isinstance(change.get('reason'), str) or not change['reason'].strip():
            raise ValueError(f'{path} 须说明操作和与新需求的关系')
        if operation in ('modify', 'delete') and not entry:
            raise ValueError(f'{path} 不存在，不能声称修改；先 glob_files/search_code 获取真实路径')
        if operation == 'create' and target.exists():
            raise ValueError(f'{path} 已存在，应修改现有实现而非重新创建')
        references = change.get('requirement_ids')
        if not isinstance(references, list) or not references or not all(isinstance(r, str) for r in references) or not set(references) <= required:
            raise ValueError(f'{path} 必须关联当前 requirements')
        covered.update(references)
        verified.append({**change, 'source_sha': entry['sha'] if entry else None,
                         'imports': locator.dependencies.get(path, [])[:20], 'imported_by': locator.importers.get(path, [])[:20]})
    if covered != required:
        raise ValueError('新需求没有定位到具体修改文件：' + ', '.join(sorted(required - covered)))
    for task in plan.get('tasks', []):
        planned = task.get('files', [])
        if not isinstance(planned, list) or not all(isinstance(path, str) for path in planned):
            raise ValueError('任务 files 必须为具体文件路径列表')
        missing = set(planned) - seen
        if missing:
            raise ValueError(f"{task['id']} 的文件没有对应 change_map：" + ', '.join(sorted(missing)))
    plan['change_map'] = verified
    return plan
