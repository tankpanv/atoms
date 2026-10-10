"""A persistent, tool-using code agent for each project workspace."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import resource
import signal
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Awaitable, Callable, TypeVar

import httpx
import psycopg
from PIL import Image

from execution_contract import resolve_default_plan, workspace_commands, validate_command, conformance_issues
from build_tiers import normalize_tier, tier_instructions, stamp_plan, tier_budget_limits, POLICY_VERSION
from build_tools import browser_enabled, stamp_tools, tool_instructions
from agent_harness import PLAN_INSTRUCTIONS, TaskLedger, json_object, source_digest, validate_plan
from agent_session import AgentSession, DIRECTORY as SESSION_DIRECTORY, digest as session_digest, estimate_tokens
from agent_delivery import DeliveryBudget, DeliveryLimitReached, ExecutionPacing, complexity, DELIVERY_INSTRUCTION
from verification_policy import INSTRUCTIONS as VERIFICATION_POLICY, command_issue, tool_issue, inspect_sources
from coding_runtime import ModelContextOverflow, ModelTemporaryError
from dependency_policy import INSTRUCTIONS as DEPENDENCY_POLICY, repair_guidance
from billing_context import active_job
from tool_contract import ToolArgumentError, parse_tool_arguments
from tool_limits import (SHELL_COMMAND_MAX, SHELL_TIMEOUT_DEFAULT, SHELL_TIMEOUT_MAX, READ_LIMIT_MAX,
                         READ_BATCH_MAX, READ_BATCH_CHARS_MAX, WRITE_BATCH_MAX, BROWSER_ACTIONS_MAX)
from coding_runtime import (AgentState, ModelGateway, apply_unified_patch, changed_diff, configured_tests,
                             has_build_command, retrieve_context, symbol_search, model_output_limit)
from code_locator import CodeLocator, DISCOVERY_TOOLS, DISCOVERY_NAMES, validate_change_map


ROOT = Path(os.getenv("WORKSPACE_ROOT", "/workspaces"))
SKIP_DIRS = {"node_modules", "dist", "published", ".git", ".npm-cache", ".python-packages", ".python-cache", ".local", ".cache", "__pycache__", ".venv", "venv", ".atoms-snapshots", ".runtime-python-deps", ".build-tmp", ".atoms-attachments", ".atoms-data", ".atoms-runtime", ".pytest_cache", ".ruff_cache", ".mypy_cache", "coverage", SESSION_DIRECTORY}
MAX_FILE_BYTES = 120_000



def project_root(project_id: uuid.UUID, owner_id: uuid.UUID | None = None) -> Path:
    worker_project = os.getenv("PROJECT_ID")
    if worker_project:
        if project_id.hex != uuid.UUID(worker_project).hex:
            raise ValueError("Project is outside this worker")
        return ROOT / project_id.hex
    database_url = os.getenv("DATABASE_URL", "postgresql://atoms:atoms_local_password@localhost:25432/atoms_demo")
    with psycopg.connect(database_url) as conn:
        row = conn.execute("SELECT owner_id,workspace_path FROM projects WHERE id=%s", (project_id,)).fetchone()
    if row and row[1]:
        relative = Path(row[1])
        if relative.is_absolute() or ".." in relative.parts or relative.parts[-1] != project_id.hex:
            raise ValueError("Invalid recorded project workspace")
        return ROOT / relative
    if row:
        owner_id = row[0]
        legacy_user = ROOT / "users" / owner_id.hex / project_id.hex if owner_id else ROOT / project_id.hex
        return legacy_user if legacy_user.exists() else ROOT / project_id.hex
    return ROOT / "projects" / project_id.hex if owner_id else ROOT / project_id.hex


def project_uid(project_id: uuid.UUID) -> int:
    return 100_000 + project_id.int % 1_000_000_000


def ensure_workspace(project_id: uuid.UUID, owner_id: uuid.UUID | None = None, *, title: str = "", prompt: str = ""):
    root = project_root(project_id, owner_id)
    root.parent.mkdir(mode=0o711, parents=True, exist_ok=True)
    if not root.exists():
        root.mkdir(mode=0o700)
        os.chown(root, project_uid(project_id), project_uid(project_id))
        documentation = root / ".atoms"
        documentation.mkdir()
        (documentation / "ARCHITECTURE.md").write_text(
            f"# Project architecture\n\nProject: {title or 'Atoms App'}\nProject ID: {project_id}\n"
            f"Created: {datetime.now(timezone.utc).isoformat()}\n\n## Initial request\n\n{prompt or '等待用户需求'}\n\n"
            "## Architecture status\n\nNot decided yet. Inspect requirements and choose frontend, backend or fullstack before scaffolding.\n"
            "Use the versioned default template when no stack is specified; otherwise respect the requested framework. Keep App as a composition entry; design modules around product responsibilities.\n")
        set_workspace_owner(root, project_uid(project_id))
    return root


def set_workspace_owner(root: Path, uid: int):
    for directory, folders, files in os.walk(root):
        folders[:] = [folder for folder in folders if folder not in SKIP_DIRS]
        for name in [directory, *(str(Path(directory) / folder) for folder in folders),
                     *(str(Path(directory) / file) for file in files)]:
            path = Path(name)
            if not path.is_symlink() and path.stat().st_uid != uid:
                os.chown(path, uid, uid)


def safe_file(root: Path, name: str) -> Path:
    if not name or name.startswith("/") or "\\" in name:
        raise ValueError("无效文件路径")
    parts = Path(name).parts
    if any(part in (".", "..") or part in SKIP_DIRS for part in parts):
        raise ValueError("不允许访问该路径")
    if any(part in {".env", "env.connector"} or part.startswith(".env.") for part in parts):
        raise ValueError("不允许访问该文件")
    target = (root / name).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("文件路径超出项目工作区")
    return target


def list_files(root: Path):
    result = []
    for directory, folders, files in os.walk(root):
        folders[:] = [folder for folder in folders if folder not in SKIP_DIRS]
        if Path(directory) == root / ".atoms":
            folders[:] = [folder for folder in folders if folder not in {"screenshots", "previews"}]
        for name in files:
            if name in {".env", "env.connector", ".coverage"} or name.startswith((".env.", ".atoms-upload-")):
                continue
            path = Path(directory) / name
            if path.is_file() and path.resolve().is_relative_to(root.resolve()):
                result.append(str(path.relative_to(root)))
    return sorted(result)


def read_file(root: Path, name: str):
    path = safe_file(root, name)
    if not path.is_file():
        raise ValueError(f"文件不存在: {name}")
    try:
        content = path.read_bytes().decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError(f"二进制文件不能作为文本读取: {name}") from exc
    if "\x00" in content:
        raise ValueError(f"二进制文件不能作为文本读取: {name}")
    return content


def read_files(root: Path, files: list[dict]):
    """Read a bounded related-file set in one tool call."""
    if not isinstance(files, list) or not 1 <= len(files) <= READ_BATCH_MAX:
        raise ValueError(f'批量读取应包含 1–{READ_BATCH_MAX} 个文件')
    seen = set()
    total = 0
    sections = []
    for index, item in enumerate(files):
        if not isinstance(item, dict) or not isinstance(item.get('path'), str):
            raise ValueError(f'files[{index}] 必须包含字符串 path')
        name = item['path']
        if name in seen:
            raise ValueError(f'批量读取包含重复路径: {name}')
        seen.add(name)
        offset = item.get('offset', 0)
        limit = item.get('limit', READ_LIMIT_MAX)
        if (not isinstance(offset, int) or isinstance(offset, bool) or offset < 0
                or not isinstance(limit, int) or isinstance(limit, bool)
                or not 1 <= limit <= READ_LIMIT_MAX):
            raise ValueError(f'{name} 的 offset/limit 无效')
        from project_snapshots import text_page
        content, total_chars, _ = text_page(safe_file(root, name), offset, limit)
        part = f'{name} [{offset}:{min(offset + limit, total_chars)}/{total_chars}]\n{content}'
        if total + len(part) > READ_BATCH_CHARS_MAX:
            # parse_tool_arguments normally fits the requested limits first;
            # retain a final guard for long paths/headers so a valid model call
            # cannot become a rejected turn because of framing overhead.
            remaining = READ_BATCH_CHARS_MAX - total
            if remaining <= 0:
                break
            marker = '\n[本批次达到总读取预算，后续文件请用 read_file 分页]'
            part = part[:max(0, remaining - len(marker))] + marker[:remaining]
        total += len(part)
        sections.append(part)
    return '\n\n'.join(sections)


def session_read_file(session, root, name, offset, limit, messages):
    path = safe_file(root, name)
    if path.stat().st_size <= MAX_FILE_BYTES:
        text = read_file(root, name)
        from agent_session import inventory
        return session.read(name, text, offset, limit, messages), inventory({name: text}).get(name)
    from project_snapshots import text_page
    part, total, sha = text_page(path, offset, limit)
    return session.read_range(name, sha, part, total, offset, limit, messages), sha


def write_file(root: Path, name: str, content: str):
    path = safe_file(root, name)
    if "\x00" in content:
        raise ValueError("二进制内容不能写入文本文件")
    if path.is_file() and path.read_bytes() == content.encode():
        return f"{name} 内容相同，未重复写入"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    set_workspace_owner(root, project_uid(uuid.UUID(hex=root.name)))
    return f"已写入 {name} ({len(content)} 字符)"


def delete_file(root: Path, name: str):
    path = safe_file(root, name)
    if not path.is_file():
        raise ValueError(f"文件不存在: {name}")
    path.unlink()
    return f"已删除 {name}"


def write_files(root: Path, files: list[dict]):
    if not isinstance(files, list) or not 1 <= len(files) <= WRITE_BATCH_MAX:
        raise ValueError(f'批量写入应包含 1–{WRITE_BATCH_MAX} 个文件')
    seen = set()
    for index, item in enumerate(files):
        if not isinstance(item, dict) or not isinstance(item.get('path'), str) or not isinstance(item.get('content'), str):
            raise ValueError(f'files[{index}] 必须包含字符串 path 和 content；整批尚未写入')
        path = safe_file(root, item['path'])
        content = item['content']
        if path.exists() and not path.is_file():
            raise ValueError('批量写入目标必须是文件')
        if any(parent.exists() and not parent.is_dir() for parent in path.parents):
            raise ValueError('批量写入目标的父路径不是目录')
        if path in seen or not isinstance(content, str) or '\x00' in content:
            raise ValueError('重复路径或无效文本')
        seen.add(path)
    results = []
    for item in files:
        try:
            results.append(write_file(root, item['path'], item['content']))
        except OSError as exc:
            return '\n'.join(results) + f'\n工具错误：{item["path"]} 写入失败：{exc}；前述文件可能已写入，检查后修复，不要盲目重放整批。'
    return '\n'.join(results)


def replace_in_file(root: Path, name: str, old: str, new: str):
    if not old:
        raise ValueError("要替换的内容不能为空")
    content = read_file(root, name)
    matches = content.count(old)
    if matches != 1:
        raise ValueError(f"目标内容匹配 {matches} 次，需要恰好匹配 1 次")
    return write_file(root, name, content.replace(old, new, 1))


def search_files(root: Path, query: str):
    if not query or len(query) > 200:
        raise ValueError("搜索词长度需为 1–200 个字符")
    matches = []
    for name in list_files(root):
        try:
            lines = read_file(root, name).splitlines()
        except ValueError:
            continue
        for line_number, line in enumerate(lines, 1):
            if query.lower() in line.lower():
                matches.append(f"{name}:{line_number}: {line[:240]}")
                if len(matches) >= 80:
                    return "\n".join(matches)
    return "\n".join(matches) if matches else "没有找到匹配内容"


def list_documents(project_id: uuid.UUID):
    database_url = os.getenv("DATABASE_URL", "postgresql://atoms:atoms_local_password@localhost:25432/atoms_demo")
    with psycopg.connect(database_url) as conn:
        rows = conn.execute("""
            SELECT id,filename,length(extracted_text) FROM message_attachments
            WHERE project_id=%s AND kind='document' ORDER BY created_at,id LIMIT 100
        """, (project_id,)).fetchall()
    return [{"id": str(row[0]), "filename": row[1], "characters": row[2]} for row in rows]


def read_document(project_id: uuid.UUID, attachment_id: str, offset: int = 0, limit: int = 12000):
    identifier = uuid.UUID(attachment_id)
    if offset < 0 or not 1 <= limit <= READ_LIMIT_MAX:
        raise ValueError("文档读取范围无效")
    database_url = os.getenv("DATABASE_URL", "postgresql://atoms:atoms_local_password@localhost:25432/atoms_demo")
    with psycopg.connect(database_url) as conn:
        row = conn.execute("""
            SELECT filename,extracted_text FROM message_attachments
            WHERE id=%s AND project_id=%s AND kind='document'
        """, (identifier, project_id)).fetchone()
    if not row:
        raise ValueError("项目文档不存在")
    return f"{row[0]} [{offset}:{min(offset + limit, len(row[1]))}/{len(row[1])}]\n{row[1][offset:offset + limit]}"


def snapshot_files(root: Path):
    from project_snapshots import snapshot
    return snapshot(root, list_files(root), safe_file)


def delivery_fingerprint(root: Path, plan: dict, files: dict, session: AgentSession | None = None) -> str:
    """Stable identity for one executable version and one acceptance contract.

    Runtime tokens, screenshots and tool receipts are deliberately excluded.
    A successful browser result may be reused only while the source, workspace
    runtime configuration and grounded scene are unchanged.
    """
    config = {}
    path = root / '.atoms-workspace.json'
    if path.is_file():
        try:
            config = json.loads(path.read_text())
        except (OSError, ValueError):
            config = {'_invalid': path.read_text(errors='replace')[:20000]}
    scene = None
    if session is not None:
        phase = session.phase('delivery_scene', 'grounded-scene-v3:' + session_digest(plan), [])
        scene = phase.get('scene')
    return session_digest({
        'startup_policy': 'javascript-http-v1',
        'source': source_digest(files),
        'runtime_config': config,
        'application_type': plan.get('application_type'),
        'deliverables': plan.get('deliverables', []),
        'artifact_content': __import__('artifact_preview').artifact_fingerprint(root, plan),
        'enabled_tools': plan.get('enabled_tools', ['browser_check']),
        'build_tier': plan.get('build_tier', 'normal'),
        'build_policy_version': plan.get('build_policy_version'),
        'delivery_checks': plan.get('delivery_checks'),
        'grounded_scene': scene,
    })


def browser_tool_fingerprint(root: Path, args: dict) -> str:
    """Identity for a model-issued browser_check, independent of its receipt ID."""
    from agent_checks import browser_state_path
    try:
        browser_state = browser_state_path(root).read_text()
    except OSError:
        browser_state = ''
    return session_digest({'source': source_digest(snapshot_files(root)), 'args': args, 'browser_state': browser_state})


def restore_files(root: Path, files: dict):
    from project_snapshots import restore
    restore(root, files, list_files(root), safe_file)
    set_workspace_owner(root, project_uid(uuid.UUID(hex=root.name)))


def _drop_privileges(uid: int):
    if os.geteuid() != uid:
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
    resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
    # V8 reserves several GiB of virtual address space even for small builds.
    # Limit resident memory through the container instead of RLIMIT_AS.


async def run_command(project_id: uuid.UUID, args: list[str], timeout: int = 120):
    root = ensure_workspace(project_id)
    uid = project_uid(project_id)
    set_workspace_owner(root, uid)
    cache = root / ".npm-cache"
    cache.mkdir(exist_ok=True)
    if cache.stat().st_uid != uid:
        os.chown(cache, uid, uid)
    env = {"PATH": f"{root / 'node_modules' / '.bin'}:/usr/local/bin:/usr/bin:/bin", "HOME": str(root), "NPM_CONFIG_CACHE": str(cache),
           "PYTHONPATH": f"{root / '.python-packages'}:{root}", "APP_DATA_DIR": str(root / ".atoms-data"),
           "PIP_CACHE_DIR": str(cache / "pip"), "CI": "1", "NODE_ENV": "development", "NO_COLOR": "1", "NODE_OPTIONS": "--max-old-space-size=768",
           "GOMAXPROCS": "2", "RAYON_NUM_THREADS": "2", "TOKIO_WORKER_THREADS": "2", "UV_THREADPOOL_SIZE": "2"}
    from project_python import environment as python_environment
    env.update(python_environment(root, uid))
    from project_database import environment as database_environment
    env.update(database_environment(project_id))
    from project_secrets import environment as secret_environment
    env.update(secret_environment(root, uid))
    process = await asyncio.create_subprocess_exec(*args, cwd=root, env=env,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        preexec_fn=lambda: _drop_privileges(uid), start_new_session=True)
    output_tail = b""
    async def drain_output():
        nonlocal output_tail
        while chunk := await process.stdout.read(8192):
            output_tail = (output_tail + chunk)[-16_000:]
        await process.wait()
        return output_tail
    try:
        output = await asyncio.wait_for(drain_output(), timeout=timeout)
    except TimeoutError:
        return 124, (f"命令在 {timeout} 秒后超时，已终止整个进程组。"
                     "分析下面的真实输出；检查 watch/交互等待、死锁、依赖/网络或长计算。不要无修改重复执行。\n"
                     + output_tail.decode(errors="replace"))
    finally:
        # A shell can exit before a grandchild; kill the whole group even if
        # the leader has exited. Docker init reaps adopted orphan children.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
    return process.returncode, output.decode(errors="replace").replace("\x00", "\\0")


async def run_shell(project_id: uuid.UUID, command: str, timeout: int = SHELL_TIMEOUT_DEFAULT):
    if not command.strip() or len(command) > SHELL_COMMAND_MAX or "\x00" in command:
        raise ValueError(f"命令长度需为 1–{SHELL_COMMAND_MAX} 个字符，不能仅含空白或包含 NUL 字符")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
        raise ValueError("timeout 必须是正整数秒")
    timeout = min(timeout, SHELL_TIMEOUT_MAX)
    validate_command(command)
    return await run_command(project_id, ["/bin/bash", "-e", "-o", "pipefail", "-c", command], timeout)


async def scaffold_project(project_id: uuid.UUID, directory: str = "frontend", template: str = "react-ts"):
    """Run the official generator; never overwrite an existing application's source."""
    root = ensure_workspace(project_id)
    target = safe_file(root, directory)
    if template not in {"react-ts", "vue-ts", "svelte-ts", "vanilla-ts", "solid-ts", "preact-ts"}:
        raise ValueError("请选择支持的 Vite 模板；其他框架可通过 run_shell 初始化")
    if target.exists() and any(target.iterdir()):
        raise ValueError("目标目录已有文件；请保留已有项目或选择空目录，不允许覆盖初始化")
    args = ["npm", "exec", "--yes", "--package=create-vite@latest", "--", "create-vite", directory,
            "--template", template, "--no-interactive"]
    code, output = await run_command(project_id, args, 180)
    if code or not (target / "package.json").exists():
        return code or 1, output + "\n框架 CLI 未生成有效项目"
    if not (root / "package.json").exists():
        import shlex
        quoted = shlex.quote(directory)
        package = {"name": "generated-product", "version": "1.0.0", "private": True, "type": "module",
                   "workspaces": [directory], "scripts": {
                       "dev": f"npm --prefix {quoted} run dev",
                       "build": f"npm --prefix {quoted} run build -- --base=./ --outDir {shlex.quote(os.path.relpath(root / 'dist', target))}"}}
        write_file(root, "package.json", json.dumps(package, indent=2) + "\n")
        config = {"dev": f"npm --prefix {quoted} run dev -- --host 127.0.0.1 --port $PORT --strictPort --base $BASE_PATH",
                  "build": "npm run build"}
        write_file(root, ".atoms-workspace.json", json.dumps(config, indent=2) + "\n")
    return 0, output + "\n真实 CLI 初始化完成；接下来按需求实现模块，配置测试和可选后端服务。根 package.json 使用 npm workspaces，依赖通常安装到根 node_modules；frontend/node_modules 为空不代表依赖缺失。"


async def ensure_npm_dependencies(project_id):
    """Successful installs are keyed by manifests/lock, never by model claims."""
    from agent_session import digest
    root = ensure_workspace(project_id)
    state_file = root/'.npm-cache/dependency-state.json'
    def key():
        names = [n for n in list_files(root) if n.endswith(('package.json', 'package-lock.json', '.npmrc'))]
        import hashlib
        hashes = {}
        for name in names:
            # Internal dependency bookkeeping is not a model text read. Large lockfiles
            # are valid; hash in bounded chunks without exposing their contents.
            checksum = hashlib.sha256()
            with safe_file(root, name).open('rb') as stream:
                for chunk in iter(lambda: stream.read(65536), b''):
                    checksum.update(chunk)
            hashes[name] = checksum.hexdigest()
        return digest(hashes)
    before = key()
    try:
        saved = json.loads(state_file.read_text())
    except (OSError, ValueError):
        saved = {}
    marker = root/'node_modules/.package-lock.json'
    marker_stat = marker.stat().st_mtime_ns if marker.exists() else None
    if marker_stat and saved == {'key':before,'marker':marker_stat}:
        return 0, '依赖声明与已验证安装未变化，复用现有依赖。'
    from project_templates import restore_dependency_cache
    cached = restore_dependency_cache(root, project_uid(project_id))
    if cached:
        code, output = 0, '复用与当前 manifest/lock 完全匹配的预装依赖，独立复制到当前项目。'
    else:
        code, output = await run_command(project_id, ['npm','install','--ignore-scripts','--no-audit','--no-fund'], 120)
    if code == 0 and marker.exists():
        state_file.parent.mkdir(exist_ok=True)
        state_file.write_text(json.dumps({'key':key(),'marker':marker.stat().st_mtime_ns}))
    return code, output


_python_dependency_locks: dict[uuid.UUID, asyncio.Lock] = {}


async def ensure_python_dependencies(project_id):
    async with _python_dependency_locks.setdefault(project_id, asyncio.Lock()):
        return await _ensure_python_dependencies(project_id)


async def _ensure_python_dependencies(project_id):
    """Prepare backend dependencies once per manifest, reusing the image cache."""
    from project_templates import python_requirement_file, python_dependency_fingerprint, restore_python_dependency_cache
    root = ensure_workspace(project_id)
    uid = project_uid(project_id)
    set_workspace_owner(root, uid)
    from project_python import environment as python_environment
    python_env = python_environment(root, uid)
    requirement = python_requirement_file(root)
    if requirement is None:
        return 0, '未配置 Python 依赖清单，跳过 Python 依赖安装。'
    fingerprint = python_dependency_fingerprint(root)
    state_file = root / '.python-cache/dependency-state.json'
    try:
        saved = json.loads(state_file.read_text())
    except (OSError, ValueError):
        saved = {}
    marker = root / '.python-packages/.atoms-dependency-ready'
    state = {'fingerprint': fingerprint, 'interpreter': (root / '.venv/.atoms-interpreter').read_text(), 'environment_version': 2}
    if saved == state and marker.exists():
        return 0, 'Python 依赖声明未变化，复用已验证安装。'
    import shutil
    from project_python import owned_directory
    staging = owned_directory(root, f'.python-cache/install-{uuid.uuid4().hex}', uid)
    backup = root / '.python-cache' / f'previous-{uuid.uuid4().hex}'
    try:
        cached = restore_python_dependency_cache(root, uid, staging)
        if cached:
            code, output = 0, '复用镜像内与 requirements.txt 完全匹配的预装 Python 依赖。'
        else:
            code, output = await run_command(project_id, [
                str(Path(python_env['VIRTUAL_ENV']) / 'bin/python'), '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
                '--ignore-installed', '--target', str(staging), '-r', str(requirement)
            ], 180)
        if code:
            return code, output
        # A changed manifest gets a clean install. Removed packages and old
        # dist-info cannot survive; failed installs leave the last good one intact.
        (staging / marker.name).write_text('ready\n')
        os.chown(staging / marker.name, uid, uid)
        (root / '.python-packages').rename(backup)
        try:
            staging.rename(root / '.python-packages')
        except BaseException:
            backup.rename(root / '.python-packages')
            raise
        state_file.write_text(json.dumps(state, ensure_ascii=False))
        os.chown(state_file, uid, uid)
        return code, output
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)


async def run_build(project_id: uuid.UUID, planned_commands: list[str] | None = None):
    root = ensure_workspace(project_id)
    python_code, python_output = await ensure_python_dependencies(project_id)
    if python_code:
        return python_code, 'Python 依赖准备失败：\n' + python_output
    config_file = root / ".atoms-workspace.json"
    if config_file.exists():
        try:
            configured_build = workspace_commands(root).get("build", "")
        except (ValueError, OSError) as exc:
            return 1, f"工作区配置无效：{exc}"
        if configured_build:
            if (root/'package.json').exists() and re.search(r'\bnpm\s+(?:run|exec)\b', configured_build) and not re.search(r'\bnpm\s+(?:install|ci)\b', configured_build):
                code, output = await ensure_npm_dependencies(project_id)
                if code:
                    return code, '安装依赖失败：\n' + output
            return await run_shell(project_id, configured_build, 180)
    if planned_commands:
        outputs = []
        for command in planned_commands:
            code, output = await run_shell(project_id, command, 180)
            outputs.append(f'{command} exit_code={code}\n{output}')
            if code:
                return code, '\n'.join(outputs)
        return 0, '\n'.join(outputs)
    package = root / "package.json"
    if package.exists():
        try:
            script = json.loads(package.read_text()).get("scripts", {}).get("build", "")
        except (ValueError, OSError) as exc:
            return 1, f"package.json 无效：{exc}"
        if not script:
            return 0, "未配置 build 脚本；可在终端运行项目自己的测试或构建命令。"
        code, output = await ensure_npm_dependencies(project_id)
        if code:
            return code, "安装依赖失败:\n" + output
        args = ["npm", "run", "build"]
        if script.strip().endswith("vite build"):
            args += ["--", "--base=./"]
        code, built = await run_command(project_id, args, 180)
        return code, output + "\n" + built
    return 0, "此项目未配置构建命令；源文件已保存，可在终端执行所需工具。\n" + python_output


def preview_document(root: Path):
    index = root / "dist" / "index.html"
    return index.read_text(errors="replace") if index.exists() else ""


TOOLS = [
    {"type": "function", "function": {"name": "write_files", "description": "Create or replace up to 12 related files with explicit path and complete content. Group files that implement one contract or feature in the same call (a practical batch is up to about 30000 source characters); use focused edits only for genuinely large existing modules. Validate all paths before writing. Not an atomic filesystem transaction; inspect existing files after an interrupted batch.", "parameters": {"type": "object", "properties": {"files": {"type": "array", "minItems": 1, "maxItems": WRITE_BATCH_MAX, "items": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}}, "required": ["files"]}}},
    {"type": "function", "function": {"name": "list_files", "description": "List source files in the project workspace.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "read_files", "description": "Read up to 16 related project source files in one coherent snapshot. Use this before a cross-file change so the model can understand contracts, callers and implementations together. Each item has a path and optional offset/limit; the executor automatically shrinks limits when the combined request exceeds the total budget and reports the adjustment. Do not repeat a path already in the current context unless its version changed.", "parameters": {"type": "object", "properties": {"files": {"type": "array", "minItems": 1, "maxItems": READ_BATCH_MAX, "items": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": READ_LIMIT_MAX}}, "required": ["path"], "additionalProperties": False}}}, "required": ["files"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a project source file by character offset. The response includes total length; request another offset to read the rest.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Create or replace a project source file with complete content.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "delete_file", "description": "Delete a source file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "replace_in_file", "description": "Replace exactly one matching text block in an existing source file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}, "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {"name": "apply_patch", "description": "Apply exact-context changes to an existing file. Use standard unified diff with valid hunk counts, or independent @@ hunks without line numbers containing unique unchanged/deleted context. Prefix each line with space, - or +. Ambiguous or stale context is rejected without writing; read the relevant range and regenerate, or use replace_in_file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "patch": {"type": "string"}}, "required": ["path", "patch"]}}},
    {"type": "function", "function": {"name": "search_files", "description": "Search project source files and return matching file paths and line numbers.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "symbol_search", "description": "Find source definitions by symbol name.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "show_diff", "description": "Show the source changes made during this task, compared with its starting state.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "list_documents", "description": "List all uploaded project documents with stable IDs and extracted text lengths.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "read_document", "description": "Read an extracted uploaded document by ID, in bounded character ranges. Use offsets to inspect large documents.", "parameters": {"type": "object", "properties": {"id": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["id"]}}},
    {"type": "function", "function": {"name": "run_shell", "description": "Run noninteractive initialization, dependency installation, inspections or tests. Pass requirement_ids when verifying acceptance. Returns a verification ID for the actual command result.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer", "minimum": 1, "maximum": SHELL_TIMEOUT_MAX}, "requirement_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "run_build", "description": "Execute the project's actual configured build; returns terminal output and verification ID.", "parameters": {"type": "object", "properties": {"requirement_ids": {"type": "array", "items": {"type": "string"}}}}}},
    {"type": "function", "function": {"name": "scaffold_project", "description": "Initialize a new complete frontend using the real official Vite CLI; refuses to overwrite existing source. Creates root workspace commands and preview config, not product functionality.", "parameters": {"type": "object", "properties": {"directory": {"type": "string"}, "template": {"type": "string", "enum": ["react-ts", "vue-ts", "svelte-ts", "vanilla-ts", "solid-ts", "preact-ts"]}}}}},
    {"type": "function", "function": {"name": "get_tasks", "description": "Read compact task progress and verification IDs. Original requirements and plan remain in the initial prompt and .atoms/PLAN.md.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "read_tool_output", "description": "Retrieve a saved full tool result by output_id, in bounded character ranges. Use to inspect omitted log/error/source details, not to re-execute the command.", "parameters": {"type": "object", "properties": {"output_id": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["output_id"]}}},
    {"type": "function", "function": {"name": "update_task", "description": "Update task status and progress. done requires successful verification IDs. deferred requires a concrete note of implemented or simulated scope, remaining TODO and recovery/configuration steps; it unblocks dependent work but is not full completion.", "parameters": {"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "done", "deferred"]}, "note": {"type": "string"}, "evidence_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["id", "status"]}}},
    {"type": "function", "function": {"name": "runtime_check", "description": "Start/restart the project's configured frontend and backend services together; verify real readiness and return service logs.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "http_request", "description": "Test a live project endpoint with a real HTTP request and status/JSON assertions. service selects a configured backend, or omit to verify the frontend proxy. Returns evidence ID.", "parameters": {"type": "object", "properties": {"service": {"type": "string"}, "path": {"type": "string"}, "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]}, "body": {}, "headers": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Project API headers, including its own Authorization Bearer token when required"}, "expect_status": {"type": "integer"}, "expect_json": {}, "requirement_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["path", "expect_status"]}}},
    {"type": "function", "function": {"name": "browser_check", "description": "Use real Chromium to exercise application user flows and assertions. Runs through the actual preview gateway. Reports console/network failures, DOM text and screenshot path. assert_response uses selector as a project-relative API path and optional method/status/expect_json to verify real results, including expected 401/422 failures. Pass requirement_ids for tested acceptance requirements.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "actions": {"type": "array", "items": {"type": "object", "properties": {"action": {"type": "string", "enum": ["click", "fill", "fill_from_text", "press", "check", "uncheck", "select", "reload", "assert_visible", "assert_text", "assert_value", "assert_count", "assert_response"]}, "method": {"type":"string","enum":["GET","POST","PUT","PATCH","DELETE","HEAD","OPTIONS"]}, "status": {"type":"integer","minimum":100,"maximum":599}, "expect_json": {}, "selector": {"type": "string"}, "source_selector": {"type":"string", "description":"For fill_from_text: copy this visible element text into the target input, allowing random answers without altering production code."}, "value": {}}, "required": ["action"]}}, "requirement_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["actions"]}}},
]
TOOLS.append({'type': 'function', 'function': {
    'name': 'revise_tasks',
    'description': 'Adapt execution when observations show task decomposition, order or focus is ineffective. Merge a vertical slice, split a blocker or reorder independent work. Preserve original requirements and final acceptance. Changed tasks lose completion; real evidence remains. Do not revise to reset counters.',
    'parameters': {'type': 'object', 'properties': {
        'reason': {'type': 'string', 'description': 'Actual observations, why the previous approach failed, and why this new sequence addresses the blocker.'},
        'tasks': {'type': 'array', 'minItems': 1, 'maxItems': 100, 'items': {'type': 'object', 'properties': {
            'id': {'type': 'string'}, 'title': {'type': 'string'},
            'requirement_ids': {'type': 'array', 'items': {'type': 'string'}},
            'depends_on': {'type': 'array', 'items': {'type': 'string'}},
            'files': {'type': 'array', 'items': {'type': 'string'}},
            'verification': {'type': 'string'}},
            'required': ['id', 'title', 'requirement_ids', 'depends_on', 'files', 'verification']}}
    }, 'required': ['reason', 'tasks']}}})
for _tool in TOOLS:
    if _tool['function']['name'] == 'http_request':
        _tool['function']['parameters']['properties'].update({
            'save_as': {'type': 'string', 'pattern': '^[A-Za-z][A-Za-z0-9_-]{0,63}$', 'description': 'Save a successfully asserted JSON response under a project-private name, e.g. alice_login. Reuse its exact token with auth_from; never transcribe JWTs.'},
            'auth_from': {'type': 'object', 'properties': {'response': {'type': 'string'}, 'pointer': {'type': 'string', 'default': '/access_token'}}, 'required': ['response'], 'description': 'Use Bearer token copied by executor from a saved response. Keep separate names for different accounts. Cannot combine with Authorization header.'},
            'expect_body': {'type': 'string', 'description': 'Exact response text assertion. Use empty string for 204 No Content.'},
            'expect_schema': {'description': 'JSON shape assertions on actual response: type, required, properties, items, minItems/maxItems, minLength/maxLength, minimum/maximum, enum/const. No executable expressions.'},
            'form': {'type': 'object', 'additionalProperties': {'type': 'string'}, 'description': 'Real form fields; URL-encoded without files, multipart with files. Do not combine with body.'},
            'files': {'type': 'array', 'maxItems': 8, 'items': {'type': 'object', 'properties': {
                'field': {'type': 'string'}, 'path': {'type': 'string', 'description': 'Existing project-relative file to upload'},
                'content_type': {'type': 'string'}}, 'required': ['field', 'path']}, 'description': 'Upload actual project files, not fake API responses; read/write headers and payload must match the server contract.'},
        })
        _tool['function']['parameters']['properties']['body']['description'] = 'JSON value, or raw request text when a string. For Form/UploadFile APIs use form and files.'

TOOLS.extend(DISCOVERY_TOOLS)


# Schemas and handlers share the same policies; no hidden paging/deadline limits.
for _tool in TOOLS:
    _name = _tool['function']['name']
    _params = _tool['function']['parameters']['properties']
    if _name in ('read_file', 'read_document', 'read_tool_output'):
        _params['offset'].update(minimum=0, description='Nonnegative character offset; use the end offset returned by the previous page.')
        _params['limit'].update(minimum=1, maximum=READ_LIMIT_MAX, description=f'Maximum characters per page: {READ_LIMIT_MAX}; larger positive requests are capped automatically.')
    elif _name == 'run_shell':
        _params['timeout'].update(minimum=1, maximum=SHELL_TIMEOUT_MAX, default=SHELL_TIMEOUT_DEFAULT,
                                 description=f'Positive seconds; execution cap {SHELL_TIMEOUT_MAX}. Higher requests are capped automatically and reported.')
    elif _name == 'browser_check':
        _params['actions'].update(maxItems=BROWSER_ACTIONS_MAX)
        _params['actions']['items']['properties']['value']['description'] = 'fill/press/assert_text/assert_value: string; select: string or string list; assert_count: nonnegative integer. Required for these actions.'
        _params['actions']['items']['properties']['selector']['description'] = 'Required for interaction actions. Read-only assertions may omit it to inspect body.'
        _item = _params['actions']['items']
        _variants = []
        for _action in _item['properties']['action']['enum']:
            _fields = {**_item['properties'], 'action': {'type': 'string', 'enum': [_action]}}
            if _action in ('click', 'press'):
                _fields['dialog'] = {'type':'string','enum':['accept','dismiss'],'default':'dismiss','description':'Expected native confirm/alert/prompt handling for this action; default dismiss. Explicitly accept only an intended operation on test-owned data. UI dialogs rendered in DOM use normal click actions instead.'}
            _required = ['action']
            if _action in ('click','fill','fill_from_text','press','check','uncheck','select','assert_value','assert_response'):
                _required.append('selector')
            if _action in ('fill','press','assert_text','assert_value'):
                _fields['value'] = {'type':'string'}
                _required.append('value')
            elif _action == 'assert_count':
                _fields['value'] = {'type':'integer','minimum':0,'description':'Exact expected number of matching elements, e.g. 2. Never use expressions such as >=1.'}
                _required.append('value')
            elif _action == 'select':
                _fields['value'] = {'oneOf':[{'type':'string'},{'type':'array','items':{'type':'string'}}]}
                _required.append('value')
            if _action == 'fill_from_text':
                _required.append('source_selector')
            _variants.append({'type':'object','properties':_fields,'required':_required})
        _params['actions']['items'] = {'type':'object','oneOf':_variants}
        _tool['function']['description'] += ' Use short flows of 3-12 actions. Copy selectors from the actual controls output; never assume button[type=submit] exists. Email fixtures must satisfy the actual API validator; use valid example.com addresses, not reserved .test domains. An intentional native confirmation uses click/press with dialog="accept"; otherwise dialogs are dismissed. assert_count compares an exact integer; for existence use assert_visible. Never use >=1 or boolean counts. A failed assertion is not a passed acceptance; use actual observations or an alternative test.'

    elif _name == 'http_request':
        _params['expect_status'].update(minimum=100, maximum=599)


from tool_specs import complete_specs
complete_specs(TOOLS)

SYSTEM_PROMPT = """You are Alex, an autonomous agent delivering the actual requested software, presentations, reports, documents, spreadsheets and other complete results in a persistent isolated project workspace.
Understand the complete user request, conversation, attachments, architecture and acceptance ledger before coding.
The workspace is not a predetermined frontend template. Choose and implement the architecture in the plan: frontend, backend, fullstack, CLI or library.
For unspecified-stack new web projects the harness installs a versioned default template and hands off actual source plus docs/AGENT_GUIDE.md; implement business directly, never regenerate it. For explicitly requested stacks use official noninteractive framework CLIs via scaffold_project or run_shell. Preserve existing projects and their stack.
scaffold_project creates an npm workspace: dependencies may be hoisted into root node_modules. Do not assume frontend/node_modules must contain them; resolve packages or run the actual build/test command.
Organize real frontend modules by responsibility (pages, components, hooks/domain, API client, types). App is a composition entry, not the whole application.
If the plan requires a backend, implement real API routes, validation, business logic and durable data. Keep real server routes, persistence and validation. Unconfigured external providers may use clearly labeled simulated demo adapters with TODO configuration; continue remaining work rather than stopping the project.
For default backend persistence use the managed PostgreSQL connector: APP_DATABASE_URL and APP_DATABASE_SCHEMA are injected server-side with a restricted application role and independent project schema. Use the template connection() with psycopg dict_row and %s SQL parameters. Do not launch a PostgreSQL container or access platform administrative credentials. Honor an explicitly requested different store and preserve existing projects.
The current task and complete plan are supplied by the harness. Do not call get_tasks/list_files just to re-obtain them. For a new project use the supplied default-template handoff directly; only if no template was supplied initialize the explicitly requested stack, then inspect only necessary generated files. Work through update_task in dependency order. A task can be marked done only with actual successful verification IDs returned by tools.
For any change touching more than one file, call read_files once with the complete related working set (API contract, types, route/service, callers and configuration), then implement the coherent change with write_files or focused edits. The executor automatically fits the batch to its total character budget. Use read_file only for a genuinely new or changed range; repeated reads waste a model round trip.
Pass requirement_ids on run_shell/run_build/http_request/browser_check to associate evidence with acceptance requirements. Browser acceptance must exercise changed user operations and assert downstream outcomes, not merely show a form. For fullstack flows use assert_response for the actual project API result plus DOM assertions; include expected validation/auth failures explicitly. For persisted data, reload/reopen and read back the unique test record. Preserve customer data. Recheck affected existing flows after new requirements; prefer short focused checks over rereading all source.
Complete all required flows, error states and persistence. Do not weaken the requirements or mark unimplemented features complete.
Read/search relevant files before editing; use paged reads, focused patches and multi-file modules. Avoid rereading unchanged files.
Use provided planning observations and your own completed write/patch calls as known code; do not reread them just to remember what you wrote.
Batch independent related tool calls in one response. Prefer focused patches for existing files; don't rewrite unchanged files or narrate the plan every turn.
Prefer read_files for the complete related working set, then use write_files to change all mutually dependent small/medium files in one call (up to 12 files and about 30000 source characters). Split only genuinely large modules or independent risky changes. Never emit the entire project in one huge tool call: output tokens also include reasoning and JSON escaping. Every files item requires both path and complete content. On OUTPUT_TRUNCATED/INVALID_JSON/MISSING_ARGUMENT, rejected calls made no changes: regenerate only that call in smaller parts; do not replay successful sibling calls. Progress uses update_task; Git commits are only needed when explicitly requested.
For compacted logs use read_tool_output(output_id, offset, limit), preserving original evidence. A cached result never authorizes repeating a side effect.
For follow-up changes use change_map as the starting scope: map the current requirement to real files/symbols, locate_change and scoped search_code reveal imports and callers, read_code obtains the needed line/symbol range. Inspect changed files before editing, follow actual API consumers and data ownership, extend the scope when evidence requires it. Static index facts and prior success are not proof of the new behavior. Keep unrelated code and data intact, then verify the new user flow and the affected existing flows using real command/API/browser checks.
After verifying a coherent feature, update its task and move forward. Don't add unsolicited features or split a small feature into per-file tasks.
Use short noninteractive run_shell commands for initialization, installing dependencies, inspections and tests. Do not start unmanaged background servers.
Configure .atoms-workspace.json dev/build/test (test may be a string or list). Main dev listens on $PORT; Vite uses $BASE_PATH.
Additional backend services use services:[{name:'api',command:'python -m uvicorn backend.app.main:app --host 127.0.0.1 --port $PORT',port_env:'API_PORT',ready_path:'/api/health'}].
The runtime starts dependencies first, injects their port variables into the frontend and manages readiness, logs and cleanup.
For Vite configure API proxy with process.env.API_PORT and use import.meta.env.BASE_URL + 'api/...' in the browser API client. Build frontend to the project root dist.
Vite proxy keys must include the preview base: const base=process.env.BASE_PATH||'/'; proxy:{[base+'api']:{target:'http://127.0.0.1:'+process.env.API_PORT,rewrite:path=>'/api'+path.slice((base+'api').length)}}. A plain '/api' proxy can intercept the entire /api/runtime/... frontend route and cause readiness 404.
Preview is an opaque sandbox origin: the preview gateway supports CORS and project Authorization, but never forwards platform cookies. Use project Bearer tokens for preview authentication.
Python/python3/pip use this project's private .venv, without platform or user site-packages. Declare dependencies in backend/requirements.txt (or root requirements.txt); run_build/runtime_check install them into this project's persistent .python-packages. For manual additions use python -m pip install; its target is already project-local. Never switch installation/runtime paths to /tmp or another ad hoc directory: /tmp is noexec and not persistent. Report an actual managed-environment error instead of creating dependency directories inside source snapshots.
Use run_build for npm builds; the harness installs dependencies when manifests change, so do not reinstall them on every validation. Use runtime_check to check the current implementation, http_request to verify real API behavior and browser_check for user flows, reload persistence and error handling.
If one tool fails, retain successful work and use an alternative tool or focused project test; never abandon the complete user goal solely for an invalid tool call. Temporary tool cooldowns do not mean application failure. Do not mark failed or skipped checks as passed.
browser_check supports click/fill/fill_from_text/press/check/uncheck/select/reload/assert_visible/assert_text/assert_value/assert_count using CSS or Playwright selectors, and assert_response using a project API path with method/status/expect_json. Browser cookies, local storage and session storage persist between calls for this project; reload preserves login, and logout clears it. HTTP tool authentication does not log the browser in. Start from actual DOM evidence and existing browser login state; do not repeatedly register/login or assume a fresh browser on each call.
For random/dynamic data, use fill_from_text with selector for the input and source_selector for the visible reference answer. Use assert_value to check input reset. Never hardcode production state or change random generation merely to make browser tests deterministic. Do not assert a hardcoded random answer; read the actual visible reference in the same scene.
Run build/dependency setup before tests; these are dependent actions, not an independent batch.
It returns real console/network errors, DOM text and a saved screenshot. Assertions must verify requirements, not merely that the page opens.
Repair concrete build, runtime, HTTP and browser failures within the bounded verification allowance; use scoped unit tests only while implementing changes, never as final acceptance. A passing build alone never proves the application fulfills the requirements.
Use disposable test records and preserve existing user data when validating follow-up changes.
Verification receipts are tied to current source: after code changes rerun affected acceptance checks. Keep README, .atoms/ARCHITECTURE.md and configuration accurate.
Browser-only capabilities must degrade gracefully when unsupported. Use real running behavior, not fabricated results.
Only finish when all tasks and requirements pass, source architecture matches the plan, systematic code checks, configured build and real current-source runtime/core workflow checks pass.
Your final summary must describe implemented behavior, executed verification and any deployment limitations in the user's language.
Static resource publication does not deploy a backend; provide a real backend deployment command and explain this in README for fullstack projects.
"""


class AgentStopped(Exception):
    pass


def preferred_context_tokens(model_window, complexity_level=None):
    """Choose a useful working window while keeping provider limits authoritative."""
    configured = os.getenv('AGENT_CONTEXT_TOKENS')
    if configured:
        try:
            return max(4096, int(configured))
        except ValueError:
            pass
    # Input is paid and processed on every turn. A small change should not
    # accumulate hundreds of thousands of tokens just because the provider
    # offers a large window. Full outputs/source versions remain in Session.
    if complexity_level:
        return min({'simple': 32000, 'medium': 48000, 'complex': 96000}[complexity_level],
                   max(4096, model_window - 14000))
    # Long-context models need a coherent multi-file working set. Smaller
    # models receive most of their available window, while the hard provider
    # limit and output reserve remain enforced by the caller.
    if model_window >= 800000:
        # Keep the large-model working set close to the provider limit while
        # reserving room for a 128k completion. The former 512k target left
        # most of a 1M context window unused.
        return max(64_000, model_window - 128_000)
    if model_window >= 256000:
        return 256000
    return max(32000, model_window - 14000)


async def phase_chat(session, gateway, client, state, messages, tools, phase_name, phase, max_tokens, before_call=None):
    from model_catalog import catalog
    selected = gateway.model_for(state) if hasattr(gateway, 'model_for') else ''
    window = next((m['context'] for m in catalog() if m['id'] == selected), 128000)
    base = preferred_context_tokens(window)
    # Review is a bounded reading phase, not a long execution loop. A larger
    # window prevents clearing observations that the reviewer immediately needs.
    target = min(window - min(max_tokens, max(4096, window // 4)) - 2000,
                 max(base * (2 if state == AgentState.REVIEW else 1), estimate_tokens(messages[:2], tools) + 8000))
    trimmed = session.phase_window(messages, target, tools)
    messages[:] = trimmed
    phase['messages'] = messages
    session.save_phase(phase_name, phase)
    options = ({'reasoning': {'effort': 'low'}} if phase_name == 'planning' and (phase.get('incremental') or phase.get('synthesis'))
               and 'openrouter.ai' in getattr(gateway, 'base_url', '') else {})
    if before_call:
        max_tokens = before_call(messages, tools, max_tokens)
    try:
        return await gateway.chat(client, state, messages, tools, max_tokens=max_tokens, **options)
    except ModelContextOverflow:
        reduced = session.phase_window(messages, target, tools, force=True)
        if reduced is messages:
            raise
        messages[:] = reduced
        phase['messages'] = messages
        session.save_phase(phase_name, phase)
        if before_call:
            max_tokens = before_call(messages, tools, max_tokens)
        return await gateway.chat(client, state, messages, tools, max_tokens=max_tokens, **options)


T = TypeVar("T")


async def await_or_stop(work: Awaitable[T], should_stop: Callable[[], bool] | None) -> T:
    """Cancel a model request or build promptly when the user stops its job."""
    task = asyncio.create_task(work)
    try:
        while not task.done():
            if should_stop and should_stop():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise AgentStopped()
            await asyncio.wait({task}, timeout=0.75)
        return await task
    finally:
        if not task.done():
            task.cancel()





async def verify_delivery_runtime(project_id, root, plan, require_scenario=False, scene_resolver=None,
                          preview_only=False, session=None):
    """Runtime evidence is executed, never supplied by the model."""
    try:
        kind = plan['application_type']
        if kind == 'artifact':
            from artifact_preview import verify_artifacts
            return await asyncio.to_thread(verify_artifacts, root, plan)
        if kind in ('cli', 'library', 'artifact'):
            commands = configured_tests(root, plan.get('commands', {}).get('test', []))
            commands = [command for command in commands if not command_issue(command, root)]
            if isinstance(commands, str):
                commands = [commands]
            if not commands:
                return 1, '请配置真实产品执行/演示命令（不使用 mock/单元测试运行器），不能只保存源码。'
            output = []
            for command in commands:
                code, report = await run_shell(project_id, command, 180)
                output.append(f'{command} exit_code={code}\n{report[-6000:]}')
                if code:
                    return code, '\n'.join(output)
            return 0, '\n'.join(output)
        from runtime import start_runtime
        from agent_checks import browser_check, http_request
        runtime_policy_error = tool_issue('runtime_check', {}, root)
        if runtime_policy_error:
            return 1, runtime_policy_error
        try:
            runtime = await start_runtime(project_id, restart=require_scenario)
        except (RuntimeError, httpx.HTTPError) as exc:
            return 1, str(exc)
        if not runtime:
            return 1, '未配置可运行服务，请设置 .atoms-workspace.json dev 及必要 services。'
        async def final_http_chain():
            """Read actual business results after restart; never replay writes."""
            current = source_digest(snapshot_files(root))
            entries = (session.state.get('execution_attempts', {}) if session else {}).values()
            from agent_recovery import final_readback_requests
            mutation_sequence = ((session.state.get('verification_limits') or {}).get('last_http_mutation', 0)
                                 if session else 0)
            checks = final_readback_requests(list(entries), current, mutation_sequence)
            if not checks and plan.get('architecture', {}).get('backend', {}).get('required'):
                return 1, '最终真实启动后缺少最终状态的业务 API 回读。请在最后一次写入/登录/清理之后，对当前状态重新执行业务 GET（传 requirement_ids 与具体 expect_json），再完成任务；不重放修改或删除前的旧断言，健康检查不能替代核心链路。'
            reports = []
            for args in checks:
                check_code, check_report = await http_request(project_id, args)
                reports.append(check_report)
                if check_code:
                    return check_code, '\n'.join(reports)
            if reports:
                return 0, '最终服务启动后真实业务 API 回读：\n' + '\n'.join(reports)
            return await http_request(project_id, {'path': '/', 'expect_status': 200})
        if kind == 'web':
            if not has_build_command(root):
                return 1, 'Web 演示必须配置并通过真实 build 命令。'
            if preview_only or not browser_enabled(plan):
                # Optional model interaction tooling does not disable the
                # platform's JavaScript startup/blank-page acceptance gate.
                boot_code, boot_report = await browser_check(project_id, root, {'path': '/', 'actions': [], 'startup_check': True})
                boot = json.loads(boot_report)
                if boot_code or (not boot.get('text', '').strip() and not boot.get('rendered_elements')):
                    return 1, '真实页面启动检查失败，不能以构建或 API 成功替代：\n' + boot_report
                if boot.get('starter_page') or ('在这里实现用户需求。' in boot.get('text', '') and '应用开发起点' in boot.get('text', '')):
                    return 1, '页面仍是模板占位页。把已实现的业务页面接入实际路由/入口后再交付演示。\n' + boot_report
                code, report = await final_http_chain() if require_scenario and not preview_only else await http_request(project_id, {'path':'/', 'expect_status':200})
                return code, '真实页面 JavaScript 启动检查（未启用完整浏览器交互验收）：\n' + boot_report + '\n服务启动与 HTTP 业务检查：\n' + report
            config = root / '.atoms-workspace.json'
            settings = json.loads(config.read_text()) if config.exists() else {}
            if not isinstance(settings, dict):
                raise ValueError('工作区配置必须为 JSON 对象')
            configured_contract = bool(settings.get('demo'))
            scenario = ({'path': '/', 'actions': [{'action': 'assert_visible', 'selector': 'body'}]} if preview_only else settings.get('demo') or plan.get('delivery_checks')) or {'path': '/', 'actions': [{'action': 'assert_visible', 'selector': 'body'}]}
            call = {'function': {'name': 'browser_check', 'arguments': json.dumps(scenario)}}
            try:
                args = parse_tool_arguments(call, TOOLS)
            except ValueError:
                if not scene_resolver:
                    raise
                args = {'path':'/', 'actions':[{'action':'assert_visible','selector':'body'}]}
            concrete = any(a.get('action', '').startswith('assert_') and a.get('selector') not in (None,'body','html') for a in args.get('actions', []))
            # A generated scene is persisted in the delivery phase. Reuse that
            # grounded contract directly instead of probing and asking the model
            # to rediscover it on every stabilization pass.
            grounded_from_phase = False
            if not concrete and session is not None:
                phase = session.phase('delivery_scene', 'grounded-scene-v3:' + session_digest(plan), [])
                if phase.get('scene'):
                    args = phase['scene']
                    concrete = True
                    grounded_from_phase = True
            # A configured, valid scene is already the acceptance contract. Do
            # not spend another browser run and model call probing the page
            # before executing it. Grounding is only needed for an absent or
            # invalid contract; preview-only intentionally remains a cheap
            # startup probe.
            # Planner-provided scenes are proposals and still get one real-page
            # grounding pass. An explicit workspace demo contract, or a scene
            # already grounded and persisted by the resolver, can execute
            # directly without paying for another probe.
            needs_grounding = (require_scenario and scene_resolver and not preview_only
                               and not configured_contract and not grounded_from_phase)
            if needs_grounding or preview_only:
                # Observe without running a speculative planner assertion.
                # The resolver grounds it once and reuses the persisted scene.
                probe_code, probe_report = await browser_check(project_id, root, {'path':args.get('path','/'), 'actions':[{'action':'assert_visible','selector':'body'}]})
                observed = json.loads(probe_report)
                if probe_code:
                    return probe_code, probe_report
                if not observed.get('text', '').strip() and not observed.get('rendered_elements'):
                    return 1, probe_report + '\n页面为空，请实现可观察的用户功能。'
                if '应用开发起点' in observed.get('text', '') and '在这里实现用户需求。' in observed.get('text', ''):
                    return 1, '页面仍是默认模板占位页，请完整实现用户业务。'
                if 'Vite + React' in observed.get('text', '') and 'Click on the Vite and React logos' in observed.get('text', ''):
                    return 1, '页面仍是官方初始化样例，请按用户需求实现产品内容。'
                if not preview_only:
                    args = await scene_resolver(observed)
            if require_scenario and not any(a.get('action', '').startswith('assert_') and a.get('selector') not in (None, 'body', 'html') for a in args.get('actions', [])):
                return 1, '请在 .atoms-workspace.json demo 配置一个真实核心演示场景，actions 必须包含具体内容或组件断言，不能只有 body 可见。'
            code, report = await browser_check(project_id, root, args)
            data = json.loads(report)
            for scene_correction in range(2):
                if not (code and require_scenario and scene_resolver and data.get('error')
                        and not data.get('errors') and not data.get('failed_responses')):
                    break
                # Locator/expectation failures are scene evidence, not an
                # instruction to rewrite working business code. At most two
                # corrections can observe success and rejection independently.
                # from the actual failure; all calls still use bounded_chat.
                observed['scene_failure'] = {'scene': args, 'result': data}
                observed.setdefault('scene_failures', []).append(observed['scene_failure'])
                args = await scene_resolver(observed)
                code, report = await browser_check(project_id, root, args)
                data = json.loads(report)
                if scene_correction == 1 and code and data.get('error') and not data.get('errors') and not data.get('failed_responses'):
                    from delivery_checks import DeliverySceneError
                    raise DeliverySceneError('演示场景经两次浏览器证据纠正后仍不匹配：'+
                        str(data['error'])[:1200]+'。已保留实际反馈，不自动反复修改业务源码。')
            if code == 0 and not data.get('text', '').strip() and not data.get('rendered_elements'):
                return 1, report + '\n页面没有可观察的演示内容，不能交付空页面。'
            return code, report
        return await final_http_chain() if require_scenario and not preview_only else await http_request(project_id, {'path': '/', 'expect_status': 200})
    except (ValueError, OSError) as exc:
        return 1, str(exc)


async def verify_delivery(project_id, root, plan, require_scenario=False, scene_resolver=None, preview_only=False, session=None):
    from delivery_checks import DeliverySceneError
    try:
        code, report = await verify_delivery_runtime(project_id, root, plan, require_scenario, scene_resolver, preview_only, session)
    except DeliverySceneError as error:
        if session is not None:
            from execution_guard import ExecutionGuard
            try:
                ExecutionGuard(session.state).validation_result('delivery_check', 1, str(error))
            finally:
                session.save('delivery_scene_failure', {'error': str(error)})
        raise
    if code == 0 and plan.get('deliverables') and plan['application_type'] != 'artifact':
        from artifact_preview import verify_artifacts
        artifact_code, artifact_report = await asyncio.to_thread(verify_artifacts, root, plan)
        code, report = artifact_code, report + '\n' + artifact_report
    if session is not None:
        from execution_guard import ExecutionGuard
        try:
            ExecutionGuard(session.state).validation_result('delivery_check', code, report)
        finally:
            session.save('delivery_check_result', {'code': code, 'report': report[-12000:]})
    return code, report


async def run_agent(project_id: uuid.UUID, prompt: str, model: str,
                    on_step: Callable, history: list[dict] | None = None,
                    should_stop: Callable[[], bool] | None = None, plan: str = "", media_context: str = "",
                    project_memory: str = "", initial_tokens: int = 0, expert_context: str = "", initial_calls: int = 0, build_tier: str | None = None,
                    availability_fallback: bool = True, enabled_tools: list[str] | None = None):
    if should_stop and should_stop():
        raise AgentStopped()
    initial_tokens = max(0, int(initial_tokens))
    initial_calls = max(0, int(initial_calls))
    root = ensure_workspace(project_id)
    initial_files = snapshot_files(root)
    planned = resolve_default_plan(json_object(plan), prompt, history, new_project=not any(not n.startswith(".atoms/") for n in initial_files))
    build_tier = normalize_tier(build_tier if build_tier is not None else planned.get('build_tier'))
    # Existing normal checkpoints predate tier metadata. Keep their plan/hash
    # intact so adding the selector does not discard saved task progress.
    if build_tier != 'normal' or 'build_tier' in planned:
        planned = stamp_plan(planned, build_tier)
    planned = stamp_tools(planned, enabled_tools)
    ledger = TaskLedger(root, planned, prompt)
    session = AgentSession(root)
    resume_context = (session.state.get('request') == prompt and
                      session.state.get('plan_hash') == session_digest(ledger.plan) and
                      not session.state.get('completed'))
    journal = list(session.state.get('journal', [])) if resume_context else []
    if not session.state and ledger.resumed:
        try:
            legacy = json.loads((ledger.directory / 'checkpoint.json').read_text())
            journal = [item for item in legacy.get('recent_actions', []) if isinstance(item, dict)][-24:]
        except (OSError, ValueError, TypeError):
            pass
        project_memory = ledger.context() + '\n旧检查点操作记录：' + json.dumps(journal, ensure_ascii=False)
        on_step('result', '旧任务检查点已加载', '沿用任务进度与执行记录；旧版本未保存的完整模型对话无法追溯，后续会话会持久保存。')
    previous_focus = session.state.get('focus_task') if resume_context else None
    set_workspace_owner(root, project_uid(project_id))
    previous_memory = session.planning_context(initial_files)
    handoff = session.planning_handoff(prompt, ledger.plan, initial_files)
    has_sources = any(not name.startswith('.atoms/') for name in initial_files)
    code_context = handoff or ('' if session.state.get('files') else retrieve_context(root, list_files(root), prompt + "\n" + plan) if has_sources else '新项目没有源码；未指定技术栈时执行器提供版本化默认模板，指定栈时按要求初始化。')
    if handoff:
        on_step('result', '复用规划源码观察', '按内容哈希验证规划阶段读取的源码，直接交给实现阶段，不重复扫描。')
    total_tokens = initial_tokens
    tier_limits = tier_budget_limits(build_tier)
    token_limit = tier_limits['token_limit']
    deadline = time.monotonic() + max(60, int(os.getenv("AGENT_TIMEOUT_SECONDS") or "3600"))
    max_iterations = tier_limits['iteration_limit']
    configured_iterations = max_iterations

    level, adaptive_iterations = complexity(ledger.plan)
    # Complexity supplies a soft pacing target, not a smaller hard budget.
    # Planning/compaction calls must not exhaust a nominal 16-turn simple job
    # when the user configured 200 calls for completing the whole requirement.
    pacing = ExecutionPacing(adaptive_iterations)
    budget = DeliveryBudget(token_limit, max_iterations, deadline, task_tokens=initial_tokens, task_calls=initial_calls,
                            configured_iteration_limit=configured_iterations)
    job_id = active_job.get()
    saved_budget = session.state.get('delivery_budget') or {}
    same_job = bool(job_id and resume_context and saved_budget.get('job_id') == job_id)
    if not same_job and session.state.get('execution_progress'):
        session.state['execution_progress']['stagnant'] = 0
    deadline_at = time.time() + (deadline - time.monotonic())
    if same_job:
        # Automatic retries are one job, not a fresh allocation of tokens,
        # turns, time or stabilization budget. Manual "continue" is a new job.
        for field in ('phase', 'task_tokens', 'task_calls', 'task_iterations',
                      'repair_tokens', 'repair_calls', 'repair_iterations'):
            if field in saved_budget:
                setattr(budget, field, saved_budget[field])
        missing_tokens = max(0, initial_tokens - budget.task_tokens - budget.repair_tokens)
        missing_calls = max(0, initial_calls - budget.task_calls - budget.repair_calls)
        if budget.phase == 'stabilization':
            budget.repair_tokens += missing_tokens
            budget.repair_calls += missing_calls
        else:
            budget.task_tokens += missing_tokens
            budget.task_calls += missing_calls
        deadline_at = saved_budget.get('deadline_at', deadline_at)
        budget.deadline = time.monotonic() + max(0, deadline_at - time.time())
    # Keep acceptance guards across a "继续" request. Resetting these counters
    # on every resumed worker was the loophole that allowed the same failed or
    # successful browser check to start over indefinitely.
    if not resume_context:
        session.state.pop('delivery_limit_reached', None)
        session.state.pop('scene_protocol_failures', None)

    def publish_budget():
        session.state['delivery_budget'] = {**budget.snapshot(), **tier_limits, 'complexity': level,
                                          'job_id': job_id, 'deadline_at': deadline_at,
                                          'suggested_iterations': adaptive_iterations,
                                          'stagnant_iterations': pacing.stagnant}
        session.save('delivery_budget', session.state['delivery_budget'])

    def usage(state: str, metrics: dict):
        nonlocal total_tokens
        total_tokens += metrics["total_tokens"]
        budget.record(metrics["total_tokens"])
        publish_budget()
        on_step("usage", state, metrics["model"], token_usage=metrics, duration_ms=metrics["duration_ms"])

    gateway = ModelGateway(model, usage, lambda: on_step("thinking", "模型正在生成代码与工具调用", "流式响应持续接收中"))
    gateway.cache_scope = project_id.hex
    gateway.on_recovery = lambda: on_step('result', '正在恢复模型连接', '保留可观察上下文与真实工具结果，重新选择可用端点；不重复执行项目工具。')
    def guard_delivery(include_iterations=False, reason=None):
        reason = reason or budget.repair_exhaustion(include_iterations)
        if reason:
            session.state['delivery_limit_reached'] = reason
            publish_budget()
            session.checkpoint(messages, ledger, journal, snapshot_files(root))
            detail = (f'演示收尾达到独立预算（{budget.repair_tokens}/{budget.repair_token_limit} tokens，'
                      f'{budget.repair_calls}/{budget.repair_iteration_limit} 次模型调用，'
                      f'{budget.repair_iterations}/{budget.repair_iteration_limit} 轮）。'
                      '已停止自动修复并保存源码、上下文和需求检查点；本次未确认演示就绪。发送“继续”可重新分配一轮预算继续。')
            on_step('warning', '演示收尾预算已用完', detail)
            raise DeliveryLimitReached(detail)

    from agent_delivery import budgeted_chat
    gateway.chat = budgeted_chat(gateway, budget, guard_delivery)
    # Keep one stable schema per run, but don't send browser/API/scaffolding
    # definitions to CLI and library jobs that cannot use those capabilities.
    unused = {'scaffold_project', 'runtime_check', 'http_request', 'browser_check'} if ledger.plan['application_type'] in ('cli', 'library', 'artifact') else set()
    if not browser_enabled(ledger.plan):
        unused.add('browser_check')
    from build_tools import api_acceptance_required
    normal_api_required = api_acceptance_required(ledger.plan)
    if build_tier == 'normal' and ledger.plan['application_type'] == 'web' and not normal_api_required:
        # A normal web job is a visual delivery unless the user explicitly
        # asked for API acceptance. Keeping http_request out of the model's
        # tool list prevents backend details from creating repair loops.
        unused.add('http_request')
    from tool_recovery import ToolRecovery
    tool_recovery = ToolRecovery()
    from execution_guard import ExecutionGuard

    def enforce_validation(name, code, output):
        try:
            execution_guard.validation_result(name, code, output)
        finally:
            session.checkpoint(messages, ledger, journal, snapshot_files(root))

    async def checked_execution(name, args, action, *, reusable=False):
        source = source_digest(snapshot_files(root))
        key = execution_guard.key(name, args, source)
        prior = execution_guard.prior(key, reusable=reusable)
        if prior:
            if prior["code"] == 0:
                on_step("result", "复用已验证命令", "源码、配置和命令未变化，复用实际成功结果。")
                return 0, "已复用实际成功结果（源码与命令未变化）。\n" + prior["output"]
            on_step("result", "分析重复失败的原因", "同一源码与参数已失败两次；跳过重复执行，保留原始错误并改用其他修复路径。")
            enforce_validation(name, prior['code'], prior['output'])
            return prior["code"], "未重复执行：同一操作与源码已失败两次。先修复实际原因，或更换诊断方法。原始错误：\n" + prior["output"]
        if name == 'run_build':
            issue = tool_issue(name, args, root)
            if issue:
                enforce_validation(name, 1, issue)
                return 1, issue
        try:
            code, output = await await_or_stop(action(), should_stop)
        except (AgentStopped, DeliveryLimitReached):
            raise
        except (ValueError, OSError, RuntimeError, httpx.HTTPError) as error:
            code, output = 1, str(error)
        execution_guard.record(key, name, args, source_digest(snapshot_files(root)), code, output)
        enforce_validation(name, code, output)
        session.save("execution_result", {"tool": name, "exit_code": code})
        return code, output
    execution_tools = [tool for tool in TOOLS if tool['function']['name'] not in unused]
    normal_web_mode = build_tier == 'normal' and ledger.plan['application_type'] == 'web'
    execution_system = SYSTEM_PROMPT
    if ledger.plan['application_type'] in ('cli', 'library', 'artifact'):
        execution_system = SYSTEM_PROMPT[:SYSTEM_PROMPT.index('Configure .atoms-workspace.json')] + SYSTEM_PROMPT[SYSTEM_PROMPT.index('Repair concrete build'):]
        execution_system += '\nFor CLI/library projects configure actual build/test commands in .atoms-workspace.json; no browser or service is required.\n'
    execution_system += VERIFICATION_POLICY
    execution_system += DEPENDENCY_POLICY
    execution_system += ('\nTreat the initial task graph as an execution hypothesis, not the user goal. '
                         'Use actual observations to decide whether to implement, investigate, validate, or revise_tasks. '
                         'When an approach fails, compare expected and actual results and run a discriminating experiment; '
                         'do not invent a business defect from an incorrect test or repeatedly narrate a diagnosis. '
                         'Stage completion may unblock later work; final requirement acceptance remains mandatory.\n')
    execution_system += '\nAcceptance prioritizes actual service startup, working preview and verified core user workflows. Ensure code quality while implementing. Do not perform a separate exhaustive file-by-file source review at acceptance; only inspect code relevant to a concrete observed build/runtime/functional failure and repair it. Preserve all explicit user requirements and real validation evidence. The harness memoizes successful browser checks by source and action fingerprint: after a successful check, do not call the same browser_check again unless source, runtime configuration, or actions changed; continue implementation or update the matching task with the existing V... evidence.\n'
    execution_system += '\nDemo-first policy: automatically fix project-local setup (including auth signing secrets); implement a usable default mock/local adapter for mockable dependencies. Only irreducible configuration for required real services belongs in user configuration TODOs. A missing-config 503 or a TODO alone is not a working mock. Complete independent features and explicitly label simulations; defer only genuinely unresolved scope. The final runnable demo is mandatory; do not claim full production acceptance for simulated or deferred capabilities.\nWhen the harness explicitly enters demo delivery/stabilization, prioritize a genuinely runnable demo and repair its build/runtime/browser failures. Preserve original requirements and evidence; leave unverified tasks open. Full completion rules still apply to COMPLETE, while the harness may separately deliver PREVIEW_READY after actual demo checks. Never claim full acceptance for a demo checkpoint.\n'
    from deliverable_contract import INSTRUCTIONS as deliverable_instructions
    execution_system += deliverable_instructions
    if ledger.plan['application_type'] == 'artifact':
        execution_system += '\n文件任务优先遵循成果合同，不强制创建前后端、README、启动服务、单元测试或API。dist为成果目录；源码文本工具禁止dist路径，使用run_shell运行生成脚本将真实成果写入dist。按照deliverables验证内容；生成脚本不是交付成果。\n'
    execution_system += tier_instructions(build_tier, 'execution')
    execution_system += tool_instructions(ledger.plan.get('enabled_tools', ['browser_check']), build_tier)
    messages = [{"role": "system", "content": execution_system + expert_context}]
    for item in ([] if previous_memory or handoff else (history or [])[-8:]):
        messages.append({"role": "user" if item["role"] == "user" else "assistant", "content": item["content"][:3000]})
    messages.append({"role": "user", "content": (
        f"当前需求：{prompt}\n附件分析与文档摘要：{media_context or '无附件'}\n"
        f"规划：{plan or '按需求实现并验证'}\n项目记忆：{previous_memory or project_memory or '无'}\n"
        f"相关代码：\n{code_context}\n"
        "计划和源码观察已提供；只在缺少具体信息时读取或搜索文件/get_tasks。新项目直接初始化，不要先读取初始化文档；需要文档全文时使用 read_document。"
        "依次实现完整计划和模块，调用验证工具并更新任务状态；全部验收通过后才给总结。"
    )})
    messages, prefix_length = session.start(prompt, ledger.plan, model, execution_system + expert_context, messages, initial_files)
    gateway.session = session
    execution_guard = ExecutionGuard(session.state)
    messages.append({'role': 'user', 'content': VERIFICATION_POLICY + '\n旧计划/模板中统一测试套件验收任务不再适用；允许为当前改动编写和执行局部单元测试，但最终依据当前源码的实际运行链路。'})
    # Migrate the pre-fingerprint checkpoint format. Older runs persisted a
    # successful demo report but no delivery key, so a continuation would
    # otherwise execute the same browser scenario once more before learning
    # that it was already complete.
    existing_demo = session.state.get('demo') or {}
    current_after_start = snapshot_files(root)
    if (existing_demo.get('ready') and existing_demo.get('core_interactions_verified')
            and existing_demo.get('startup_policy') == 'javascript-http-v1'
            and existing_demo.get('browser_check_enabled', True) == browser_enabled(ledger.plan)
            and existing_demo.get('source_digest') == source_digest(current_after_start)
            and not session.state.get('delivery_verification')):
        session.state['delivery_verification'] = {
            'key': delivery_fingerprint(root, ledger.plan, current_after_start, session),
            'mode': 'full', 'report': existing_demo.get('report', ''),
            'source_digest': existing_demo.get('source_digest'), 'verified_at': time.time(),
        }
        session.save('delivery_cache_migrated', session.state['delivery_verification'])
    if session.restored:
        on_step('result', '已恢复构建会话', f'恢复 {len(messages)} 条活动上下文、{len(journal)} 条操作记录；'
                f'发现 {len(session.changed)} 个变化文件。保留已完成任务，不重新初始化项目。')
    from model_catalog import catalog
    model_window = next((m['context'] for m in catalog() if m['id'] == model), 128000)
    context_ceiling = max(4096, model_window - min(32000, model_window // 4) - 4000)
    context_target = min(preferred_context_tokens(model_window, level), context_ceiling)
    if session.state.get('context_target') != context_target:
        # A legacy large-window compaction floor must not defeat a newly
        # configured working target on resume.
        session.state.pop('compaction_floor', None)
        session.state['context_target'] = context_target

    async def compact_context(force=False):
        nonlocal messages, prefix_length
        # Summarizing cannot shrink the tool schema, system instructions and
        # recent complete tool groups. If those exceed the preferred target,
        # require meaningful new growth before paying for another summary.
        # The actual model window remains the hard ceiling, including reserve.
        compact_threshold = min(max(context_target, int(session.state.get('compaction_floor', 0)) + 8000),
                                context_ceiling)
        if not force and estimate_tokens(messages, execution_tools) <= compact_threshold:
            return
        before_prune = messages
        messages = session.prune(messages, prefix_length, context_target, execution_tools)
        if messages is not before_prune:
            session.checkpoint(messages, ledger, journal)
            on_step('result', '早期工具输出已整理', '全文留在会话存储，可按输出 ID 读取；保留最近操作和工具配对，无需模型摘要调用。')
            if not force and estimate_tokens(messages, execution_tools) <= compact_threshold:
                return
        if not force and sum(m.get('role') == 'assistant' for m in messages[prefix_length:]) < 3:
            return  # fixed initial instructions cannot benefit from repeated summaries
        on_step('thinking', '正在整理构建上下文', '保存完整会话和工具结果，压缩早期内容后自动继续。')
        summary = session.state.get('summary', '')
        try:
            response = await await_or_stop(gateway.chat(client, AgentState.COMPACT, [
                {'role': 'system', 'content': '总结构建对话以便继续执行。保留已决定的架构、约束、精确文件路径、'
                 '未完成工作、用户补充要求、失败原因和下一步。继承上次摘要仍有效的约束。'
                 '只输出简短摘要，不重复源码或整份任务清单，不编造完成状态，不执行任何工具。'},
                 {'role': 'user', 'content': session.summary_input(messages, ledger, journal)}],
                max_tokens=model_output_limit(gateway.model_for(AgentState.COMPACT) if hasattr(gateway, 'model_for') else model, AgentState.COMPACT)), should_stop)
            summary = response.get('content') or summary
        except (AgentStopped, DeliveryLimitReached):
            raise
        except (RuntimeError, httpx.HTTPError) as exc:
            on_step('warning', '使用任务检查点整理上下文', f'模型摘要暂不可用，仍保留任务和实际工具结果：{str(exc)[:150]}')
        messages = session.compact(messages, prefix_length, ledger, journal, summary,
                                   target_tokens=context_target, tools=execution_tools)
        prefix_length = session.state['prefix_length']
        session.state['compaction_floor'] = estimate_tokens(messages, execution_tools)
        session.save('compaction_threshold', {'tokens': session.state['compaction_floor']})
        on_step('result', '构建上下文已整理', f'当前估计 {session.state["compaction_floor"]} tokens；任务继续。')
    # A previous PREVIEW_READY result is useful evidence for a continuation;
    # it is invalidated lazily when the source/configuration fingerprint changes.
    publish_budget()
    messages.append({'role': 'user', 'content': f'执行规模：{level}。优先用最少的必要步骤完整实现全部明确需求并通过真实验收。建议 {adaptive_iterations} 轮是执行节奏参考，配置上限为 {max_iterations} 次；超过建议节奏时调整执行方法，不缩减需求。简单需求合并同一功能的实现和验收，避免逐文件规划、重复读取和重复总结。演示交付只是接近真实配置预算时的兜底。'})
    last_summary = ""
    last_validation = ""
    repair_feedback = ""
    successful_build = None
    final_verified_key = None
    browser_tool_cache = session.state.setdefault('browser_tool_cache', {})
    on_step("state", AgentState.IMPLEMENT.value, "开始修改代码")
    frontend = ledger.plan['architecture']['frontend']
    from project_templates import select_template, install_template, requested_stack
    selected_template = select_template(ledger.plan, prompt, history)
    if not has_sources and selected_template:
        on_step('thinking', '加载默认应用模板', '复用已验证的工程、启动和部署配置，直接实现用户业务。')
        manifest = install_template(root, selected_template)
        set_workspace_owner(root, project_uid(project_id))
        receipt = ledger.record('install_default_template', json.dumps({k:manifest[k] for k in ('name','version','sha256')}), 0,
                                'Trusted platform template installed', source_digest(snapshot_files(root)), [])
        code, output = await await_or_stop(ensure_npm_dependencies(project_id), should_stop)
        python_code, python_output = await await_or_stop(ensure_python_dependencies(project_id), should_stop)
        if code == 0 and python_code:
            code, output = python_code, output + '\n' + python_output
        elif code == 0:
            output += '\n' + python_output
        on_step('tool', '准备应用工程', f'verification_id={receipt}\n模板={selected_template}\nexit_code={code}\n{output[-2000:]}',
                tool_name='install_default_template', tool_input=selected_template, tool_output=output[-3000:])
        # One verified handoff; lockfile/package metadata is cached without sending its thousands of lines.
        handoff_files = [n for n in manifest['files'] if (n.endswith(('.ts','.tsx','.css','.py')) and not n.startswith(('frontend/src/components/ui/','frontend/src/hooks/'))) or n in
                         ('package.json','frontend/package.json','frontend/tsconfig.json','.atoms-workspace.json','README.md','frontend/README.md','docs/AGENT_GUIDE.md','docs/UI_COMPONENTS.md','frontend/components.json','frontend/postcss.config.js')]
        handoff_parts = []
        for name in handoff_files:
            content = read_file(root, name)
            # Guides may exceed one tool page; deliver each range once instead of silently truncating.
            handoff_parts.extend(session.read(name, content, offset, READ_LIMIT_MAX, messages)
                                 for offset in range(0, max(1, len(content)), READ_LIMIT_MAX))
        observations = '\n\n'.join(handoff_parts)
        messages.append({'role':'user','content':'执行器已安装真实版本化默认模板：'+json.dumps(manifest,ensure_ascii=False)+
                         f'\n基线依赖准备 exit_code={code}：{output[-1500:]}\n实际模板源码与规范全文如下，不要重新初始化或读取已提供的文件。直接集中实现原始业务，局部单元测试仅用于当次修改验证；系统检查代码并启动真实链路验收，占位首页不能交付。\n'+observations})
        session.checkpoint(messages, ledger, journal, snapshot_files(root))
        execution_tools = [tool for tool in execution_tools if tool['function']['name'] != 'scaffold_project']
    elif (not has_sources and not requested_stack(prompt, history) and ledger.plan['application_type'] == 'web'
            and frontend['directory'] == 'frontend'
            and 'vite' in frontend['stack'].lower() and 'react' in frontend['stack'].lower()):
        on_step('thinking', '正在初始化已规划的前端', '执行真实 Vite React TypeScript CLI，无需模型重复查看空目录。')
        code, output = await await_or_stop(scaffold_project(project_id), should_stop)
        receipt = ledger.record('scaffold_project', 'Vite React TypeScript CLI', code, output, source_digest(snapshot_files(root)), [])
        output = f'verification_id={receipt}\nexit_code={code}\n' + output
        on_step('tool', '命令行初始化项目', output[:3000], tool_name='scaffold_project', tool_input='frontend/react-ts', tool_output=output[:12000])
        template_files = ['package.json', '.atoms-workspace.json', 'frontend/package.json',
                          'frontend/tsconfig.app.json', 'frontend/tsconfig.json', 'frontend/vite.config.ts',
                          'frontend/index.html', 'frontend/src/main.tsx', 'frontend/src/App.tsx',
                          'frontend/src/App.css', 'frontend/src/index.css']
        observations = '\n\n'.join(session.read(name, read_file(root,name), 0, READ_LIMIT_MAX, messages)
                                     for name in template_files if (root/name).is_file()) if code == 0 else ''
        messages.append({'role':'user','content':f'执行器已运行真实 scaffold_project，exit_code={code}。\n{output[-3000:]}\n当前文件：'+json.dumps(list_files(root),ensure_ascii=False)+'\n已提供需修改的实际模板文件全文（不是节选），无需重新读取：\n'+observations+'\n初始化成功时不要再运行脚手架、列目录或读取以上文件；直接集中实现当前产品任务。'})
        session.checkpoint(messages, ledger, journal, snapshot_files(root))
    async with httpx.AsyncClient(timeout=180) as client:
        turn = -1
        while True:
            turn += 1
            if budget.due(estimate_tokens(messages, execution_tools) + 12000):
                budget.phase = 'stabilization'
                messages.append({'role': 'user', 'content': DELIVERY_INSTRUCTION})
                on_step('state', AgentState.STABILIZE.value, '优先修复启动与预览，准备可演示版本')
            stabilizing = budget.phase == 'stabilization'
            if should_stop and should_stop():
                raise AgentStopped()
            guard_delivery(include_iterations=True)
            if stabilizing:
                budget.repair_iterations += 1
            else:
                budget.task_iterations += 1
            publish_budget()
            if time.monotonic() >= budget.deadline:
                session.checkpoint(messages, ledger, journal, snapshot_files(root))
                raise DeliveryLimitReached('任务已达到总执行时间上限，已停止并保存源码、验证证据和检查点。不会自动重启验证循环。')
            checkpoint_due = stabilizing or turn == max_iterations - 1
            execution_source = source_digest(snapshot_files(root))
            try:
                recovery_due = execution_guard.observe(execution_source, ledger.tasks, ledger.evidence)
            except DeliveryLimitReached:
                session.checkpoint(messages, ledger, journal, snapshot_files(root))
                raise
            if recovery_due:
                diagnostic = execution_guard.diagnostic(execution_source)
                failures = execution_guard.failures(execution_source)
                from runtime import runtime_status
                live_runtime = runtime_status(project_id)
                from agent_recovery import recovery_facts, adaptive_recovery_context
                incident = execution_guard.begin_recovery(execution_source)
                facts = recovery_facts(root, ledger, journal, execution_source, live_runtime, failures)
                diagnostic = adaptive_recovery_context(facts, incident)
                incident['status'] = 'acting_model_reassessing_from_evidence'
                session.checkpoint(messages, ledger, journal, snapshot_files(root))
                session.feedback(messages, 'recovery', diagnostic)
                on_step("result", "正在调整问题解决路径", "结合全局需求、已有实验与真实缺口，由执行模型调整路径并继续行动。")
                # Recovery provides guidance for the next implementation
                # action; it is not an acceptance boundary. Running the full
                # checks here caused inspect/build/completion loops without
                # any source or task progress.
                checkpoint_due = False
            if should_stop and should_stop():
                raise AgentStopped()
            await compact_context()
            task = ledger.next_task()
            if task and not stabilizing and task["id"] != previous_focus:
                session.feedback(messages, 'focus_task', "执行检查点：聚焦当前可推进任务，持续判断整体目标与依赖；必要时用 revise_tasks 调整路径，不反复通读整个项目。"
                                 "如已经实现，运行针对性验证并用返回的 V... 证据调用 update_task 标记 done，再进入下一任务。\n"
                                 + json.dumps(task, ensure_ascii=False))
                previous_focus = task["id"]
                session.state['focus_task'] = previous_focus
            directive = '执行协调器当前指令：'
            messages = [m for m in messages if not (m.get('role') == 'user' and isinstance(m.get('content'), str) and m['content'].startswith(directive))]
            instruction = None
            if stabilizing:
                instruction = directive + DELIVERY_INSTRUCTION + '\n当前唯一修复目标：' + (repair_feedback[-4000:] or '完成当前已有功能的启动与演示，不重新设计或通读项目。')
            elif task:
                contracts = [c for c in ledger.plan.get('interfaces', []) if c['path'] in task.get('files', []) or set(c.get('consumers', [])).intersection(task.get('files', []))]
                instruction = directive + '当前重点是以下任务；以整体用户目标为准。发现依赖、分解或验证路径不合理时，使用 revise_tasks 调整执行，不必困在此任务；不能削减原需求或放宽最终验收：' + json.dumps({'task':task,'interfaces':contracts}, ensure_ascii=False)
            from system_contract import decision_frame
            frame = decision_frame(ledger, snapshot_files(root), session.state)
            instruction = (instruction or directive) + '\n当前系统决策依据（实际状态，不是新规划）：' + json.dumps(frame, ensure_ascii=False)
            if not stabilizing:
                pacing_instruction, pacing_changed = pacing.guide(ledger, journal, budget)
                instruction = (instruction or directive) + '\n' + pacing_instruction
                if pacing_changed:
                    on_step('result', '调整完整需求的实现节奏', '复用已确认信息，集中推进实现和针对性验收；保留原需求目标。')
            if instruction:
                # Keep the most recent tool receipt/error last and preserve the
                # uninterrupted assistant/tool exchange required by providers.
                position = len(messages)
                if messages and messages[-1].get('role') == 'tool':
                    while position and messages[position-1].get('role') == 'tool':
                        position -= 1
                    position -= 1
                messages.insert(position, {'role':'user','content':instruction})
            on_step("thinking", "Alex 正在分析需求" if turn == 0 else "Alex 正在继续构建", "")
            session.checkpoint(messages, ledger, journal)
            turn_tools = tool_recovery.available(execution_tools)
            tool_recovery.next_turn()
            availability_error = None
            try:
                assistant = await await_or_stop(gateway.chat(client, AgentState.STABILIZE if stabilizing else AgentState.IMPLEMENT, messages,
                                                             turn_tools, max_tokens=model_output_limit(gateway.model_for(AgentState.STABILIZE if stabilizing else AgentState.IMPLEMENT) if hasattr(gateway, 'model_for') else model, AgentState.STABILIZE if stabilizing else AgentState.IMPLEMENT)), should_stop)
            except ModelContextOverflow:
                await compact_context(force=True)
                assistant = await await_or_stop(gateway.chat(client, AgentState.STABILIZE if stabilizing else AgentState.IMPLEMENT, messages,
                                                             turn_tools, max_tokens=model_output_limit(gateway.model_for(AgentState.STABILIZE if stabilizing else AgentState.IMPLEMENT) if hasattr(gateway, 'model_for') else model, AgentState.STABILIZE if stabilizing else AgentState.IMPLEMENT)), should_stop)
            except ModelTemporaryError as exc:
                # Provider availability must not discard working output. Try
                # a real local delivery check before scheduling recovery.
                availability_error = exc
                checkpoint_due = True
                assistant = {'content': ''}
                on_step('result', '正在恢复构建服务', '模型连接暂时异常；保留进度，先验证已有成果是否可以实际交付。')
                session.checkpoint(messages, ledger, journal, snapshot_files(root))
            tool_calls = assistant.get("tool_calls") or []
            content = assistant.get("content") or ""
            if tool_calls:
                messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls,
                                 **{key: assistant[key] for key in ("reasoning", "reasoning_details") if key in assistant}})
                session.checkpoint(messages, ledger, journal)
                rejected = 0
                recovery_messages = []
                for call in tool_calls:
                    if should_stop and should_stop():
                        raise AgentStopped()
                    name = call["function"]["name"]
                    if name == "browser_check" and not browser_enabled(ledger.plan):
                        messages.append({"role":"tool", "tool_call_id":call["id"], "content":"工具未启用，此调用未执行。用户未勾选浏览器验收；继续通过系统代码检查、构建和真实 HTTP/API 校验完成任务，不得要求 browser_check。"})
                        continue
                    args = {}
                    started = time.monotonic()
                    code = None
                    contract_error = None
                    adjustments = []
                    try:
                        args = parse_tool_arguments(call, turn_tools, assistant, adjustments)
                        policy_error = tool_issue(name, args, root)
                        if name == 'run_shell' and stabilizing:
                            policy_error = policy_error or command_issue(args.get('command', ''), root)
                        if policy_error:
                            try:
                                execution_guard.policy_rejected(policy_error)
                            finally:
                                session.checkpoint(messages, ledger, journal, snapshot_files(root))
                            raise ValueError(policy_error)
                        if name in ('runtime_check', 'http_request', 'browser_check'):
                            failed_key = execution_guard.key(name, args, source_digest(snapshot_files(root)))
                            previous_failure = execution_guard.prior(failed_key)
                            if previous_failure and previous_failure['code'] != 0:
                                code = previous_failure['code']
                                execution_guard.request_recovery(name, previous_failure['output'],
                                    '同一源码和参数已失败两次；先取得不同判别检查的新证据或修复原因，再重试。')
                                raise ValueError('未重复执行同一失败检查。先执行其他真实判别检查、验证接口合同或修复具体原因。原始结果：' + previous_failure['output'][-2500:])
                        references = args.get("requirement_ids", [])
                        valid_references = {item["id"] for item in ledger.plan["requirements"]}
                        if not isinstance(references, list) or not set(references).issubset(valid_references):
                            raise ValueError("requirement_ids 必须引用当前需求清单中的 ID")
                        if name in {"run_shell", "scaffold_project"}:
                            successful_build = None  # Commands may mutate dependencies outside source snapshots.
                        if name in {"run_shell", "run_build", "scaffold_project", "runtime_check", "http_request", "browser_check"}:
                            on_step("thinking", "正在执行项目工具", f"{name}: {args.get('command', args.get('path', ''))}")
                        if name == "list_files":
                            result = json.dumps(list_files(root), ensure_ascii=False)
                        elif name == "read_files":
                            sections = []
                            for item in args['files']:
                                offset = int(item.get('offset', 0))
                                limit = int(item.get('limit', READ_LIMIT_MAX))
                                if offset < 0 or not 1 <= limit <= READ_LIMIT_MAX:
                                    raise ValueError(f"{item['path']} 的文件读取范围无效")
                                sections.append(session_read_file(session, root, item['path'], offset, limit, messages)[0])
                            result = '\n\n'.join(sections)
                        elif name == "read_file":
                            offset = int(args.get("offset", 0))
                            limit = int(args.get("limit", 10000))
                            if offset < 0 or not 1 <= limit <= READ_LIMIT_MAX:
                                raise ValueError("文件读取范围无效")
                            result = session_read_file(session, root, args['path'], offset, limit, messages)[0]
                        elif name == "write_file":
                            result = write_file(root, args["path"], args["content"])
                        elif name == 'write_files':
                            result = write_files(root, args['files'])
                        elif name == "delete_file":
                            result = delete_file(root, args["path"])
                        elif name == "replace_in_file":
                            result = replace_in_file(root, args["path"], args["old"], args["new"])
                        elif name == "apply_patch":
                            result = apply_unified_patch(root, args["path"], args["patch"], read_file, write_file)
                        elif name == "search_files":
                            result = search_files(root, args["query"])
                        elif name == "symbol_search":
                            result = symbol_search(root, list_files(root), args["query"])
                        elif name in DISCOVERY_NAMES:
                            locator = CodeLocator(root, session)
                            locator.sync()
                            result = locator.execute(name, args, messages)
                        elif name == "show_diff":
                            result = changed_diff(initial_files, snapshot_files(root)) or "当前任务没有文件差异"
                        elif name == "list_documents":
                            result = json.dumps(list_documents(project_id), ensure_ascii=False)
                        elif name == "read_document":
                            result = read_document(project_id, args["id"], args.get("offset", 0), args.get("limit", 12000))
                        elif name == "run_shell":
                            timeout = args.get("timeout", SHELL_TIMEOUT_DEFAULT)
                            code, output = await checked_execution("run_shell", args, lambda: run_shell(project_id, args["command"], timeout))
                            result = f"exit_code={code}\n{output}"
                        elif name == "run_build":
                            code, output = await checked_execution("run_build", args, lambda: run_build(project_id), reusable=True)
                            result = f"exit_code={code}\n{output}"
                        elif name == "scaffold_project":
                            code, output = await await_or_stop(scaffold_project(project_id, args.get("directory", "frontend"), args.get("template", "react-ts")), should_stop)
                            result = f"exit_code={code}\n{output}"
                        elif name == "get_tasks":
                            result = ledger.model_context(snapshot_files(root))
                        elif name == 'revise_tasks':
                            result = ledger.revise_tasks(args['tasks'], args['reason'])
                            session.state.update(plan=ledger.plan, plan_hash=session_digest(ledger.plan))
                            session.save('execution_strategy_revision', {'reason': args['reason'], 'tasks': ledger.tasks})
                            previous_focus = None
                            checkpoint_due = False
                        elif name == 'read_tool_output':
                            result = session.read_output(args['output_id'], int(args.get('offset', 0)), int(args.get('limit', 6000)))
                        elif name == "update_task":
                            result = ledger.update(args["id"], args["status"], args.get("note", ""), args.get("evidence_ids", []))
                            # Only a completed/deferred task is an acceptance
                            # boundary. Progress notes must not rerun the full
                            # build and startup pipeline.
                            if args["status"] in {"done", "deferred"} and not any(
                                    item["status"] not in {"done", "deferred"}
                                    for item in ledger.tasks):
                                # The final task transition is the acceptance
                                # boundary. Earlier task receipts are scoped
                                # evidence; running whole-project checks after
                                # every task serializes a multi-task plan.
                                checkpoint_due = True
                        elif name == "runtime_check":
                            from runtime import start_runtime, runtime_status
                            runtime = await await_or_stop(start_runtime(project_id), should_stop)
                            code = 0 if runtime else 1
                            result = json.dumps(runtime_status(project_id) or {"error": "没有配置开发服务"}, ensure_ascii=False)
                        elif name == "http_request":
                            # Normal web mode is a visual demo path. Keep one
                            # representative model-issued API probe for an
                            # explicitly planned contract, then stop probing
                            # endpoint/error matrices. The final startup check
                            # is executed by the harness separately.
                            if normal_web_mode and not normal_api_required:
                                probe_count = int(session.state.get('normal_api_checks', 0))
                                if probe_count >= 1:
                                    code = None
                                    result = ('{"skipped":true,"detail":"普通档位已完成一次代表性 API 检查；'
                                              '继续前端演示实现，不重复探测接口或外部依赖。"}')
                                else:
                                    session.state['normal_api_checks'] = probe_count + 1
                                    session.save('normal_api_probe', session.state['normal_api_checks'])
                                    from agent_checks import http_request
                                    code, result = await await_or_stop(http_request(project_id, args), should_stop)
                            else:
                                from agent_checks import http_request
                                code, result = await await_or_stop(http_request(project_id, args), should_stop)
                        elif name == "browser_check":
                            # Do not re-run an identical successful browser
                            # check while the source and actions are unchanged.
                            # The model may still request it while reasoning;
                            # returning the stored evidence keeps the tool
                            # contract intact without creating another runtime,
                            # screenshot or billable verification receipt.
                            cache_key = browser_tool_fingerprint(root, args)
                            cached = browser_tool_cache.get(cache_key)
                            delivery_evidence = session.state.get('delivery_verification') or {}
                            if (stabilizing and delivery_evidence.get('mode') == 'full'
                                    and delivery_evidence.get('source_digest') == source_digest(snapshot_files(root))):
                                # Stabilization has already passed the harness
                                # core workflow for this exact source. Extra
                                # model-issued browser scenarios are redundant;
                                # feature work belongs in implementation mode.
                                cached = {'code': 0, 'report': delivery_evidence.get('report', ''),
                                          'receipt': 'harness-delivery'}
                            if cached and cached.get('code') == 0:
                                code = None  # no new ledger evidence/receipt
                                # Treat cached acceptance as a checkpoint. Do
                                # not spend another implementation turn merely
                                # to replay the same browser action sequence.
                                # A cached browser result is already a
                                # checkpoint. Continue implementation instead
                                # of re-entering the complete acceptance pass.
                                checkpoint_due = False
                                result = ('{"ok":true,"reused":true,"detail":"已复用相同源码和参数的成功浏览器验收；'
                                          '不要重复调用，继续实现或更新任务。","evidence":"' +
                                          str(cached.get('receipt', '')) + '"}\n' + str(cached.get('report', '')))
                            else:
                                from agent_checks import browser_check
                                code, result = await await_or_stop(browser_check(project_id, root, args), should_stop)
                                if code == 0:
                                    # Bound the durable cache so a project with
                                    # many legitimate scenarios cannot grow the
                                    # session checkpoint without limit.
                                    if len(browser_tool_cache) >= 24:
                                        oldest = next(iter(browser_tool_cache))
                                        browser_tool_cache.pop(oldest, None)
                                    browser_tool_cache[cache_key] = {
                                        'code': code, 'report': result[-12000:],
                                        'receipt': None, 'source': source_digest(snapshot_files(root)),
                                    }
                        else:
                            result = "未知工具"
                        if code is not None:
                            if name in ('browser_check','http_request','runtime_check','scaffold_project'):
                                current_source = source_digest(snapshot_files(root))
                                key = execution_guard.key(name,args,current_source)
                                execution_guard.record(key,name,args,current_source,code,result)
                                enforce_validation(name, code, result)
                            if name == 'browser_check' and code != 0:
                                recovery_messages.append({'role':'user','content':'浏览器检查未通过。先区分错误选择器/测试步骤与业务错误：返回的 controls 是当前真实控件，复用其中 selector，注意 a 和 button、真实文案及当前登录状态。找不到旧测试假定的控件不等于功能缺失；已有等价操作时修正检查步骤，不要为错误测试重写页面、账号流程或整个项目。只有真实 API/页面错误或需求功能确实缺失才修改对应源码。保留已执行的准备步骤，不盲目重复创建数据。'})
                            local_unit_check = name == 'run_shell' and bool(command_issue(args.get('command', ''), root))
                            receipt = ledger.record('local_unit_check' if local_unit_check else name,
                                                    args.get("command", args.get("path", name)), code, result,
                                                    source_digest(snapshot_files(root)), [] if local_unit_check else references, arguments=args)
                            if local_unit_check:
                                result += '\n局部单元测试结果仅用于本次改动诊断，不作为需求通过或最终交付验收证据。'
                            result = f"verification_id={receipt}\n{result}"
                    except (AgentStopped, DeliveryLimitReached):
                        raise
                    except ToolArgumentError as exc:
                        contract_error = exc
                        rejected += 1
                        result = exc.result()
                    except ValueError as exc:
                        if name in DISCOVERY_NAMES or name in ('update_task', 'revise_tasks', 'read_files', 'read_file', 'read_document', 'read_tool_output', 'replace_in_file', 'apply_patch', 'delete_file', 'write_file', 'write_files'):
                            result = ToolArgumentError('TOOL_PRECONDITION_FAILED', str(exc), name).result()
                        else:
                            result = f"工具错误: {exc}"
                            if name in ('runtime_check', 'http_request', 'browser_check'):
                                code = 1
                                failed_source = source_digest(snapshot_files(root))
                                execution_guard.record(execution_guard.key(name, args, failed_source), name, args, failed_source, code, result)
                                enforce_validation(name, code, result)
                    except Exception as exc:
                        result = f"工具错误: {exc}"
                        if name in ('runtime_check', 'http_request', 'browser_check'):
                            code = 1
                            failed_source = source_digest(snapshot_files(root))
                            execution_guard.record(execution_guard.key(name, args, failed_source), name, args, failed_source, code, result)
                            enforce_validation(name, code, result)
                    from system_contract import configuration_issue
                    config_issue = (configuration_issue(ledger.evidence, source_digest(snapshot_files(root)), ledger.plan)
                                    if name == 'http_request' and code else None)
                    if normal_web_mode and not normal_api_required and name == 'http_request' and code and not config_issue:
                        # API details are deliberately non-blocking in the
                        # normal demo tier. Preserve the failed receipt for
                        # transparency, then clear its recovery trigger so a
                        # missing provider cannot start an API repair loop.
                        session.state.setdefault('deferred_dependencies', []).append({
                            'kind': 'api_probe', 'detail': '普通档代表性 API 检查失败，已转为演示 TODO；继续前端演示。'
                        })
                        session.state['deferred_dependencies'] = session.state['deferred_dependencies'][-8:]
                        execution_guard.limits.pop('pending_recovery', None)
                        result += '\n普通档策略：该 API 缺口已记录为 TODO，不阻塞前端演示，也不会重复验证或进入 API 恢复循环。'
                    if tool_recovery.record(name, bool(contract_error) or result.startswith('工具错误:') or result.startswith('未知工具') or ('"executed": false' in result and '"ok": false' in result)):
                        recovery_messages.append({'role':'user','content':f'工具 {name} 连续调用失败，接下来三轮暂时跳过此工具。保留成功的修改，继续其他任务或改用可用工具：源码读取优先使用 read_files/read_file，写入使用 write_files/write_file，验证可用 run_build/runtime_check/http_request 或系统代码检查。不得声称失败验收通过；不要重复原调用。完成业务实现后系统会独立启动并检查实际预览。'})
                        on_step('result', '继续实现与交付', '已跳过重复失败的工具调用，保留当前代码并采用其他路径推进。')
                    execution_guard.recovery_action(name, args, code, result)
                    if adjustments:
                        result += "\n参数已按工具执行限制调整：" + json.dumps(adjustments, ensure_ascii=False)
                    label = {"list_files": "查看项目文件", "read_files": "批量读取相关文件", "read_file": "读取文件", "write_file": "编写代码", "write_files": "批量编写代码",
                             "delete_file": "删除文件", "replace_in_file": "修改文件", "apply_patch": "应用补丁",
                             "search_files": "搜索代码", "symbol_search": "查找符号", "show_diff": "检查改动",
                             "locate_change": "定位需求改动范围", "glob_files": "查找相关文件", "search_code": "搜索相关调用", "read_code": "读取相关代码区间",
                             "list_documents": "查看项目文档", "read_document": "阅读文档",
                             "run_shell": "运行命令", "run_build": "运行构建", "scaffold_project": "命令行初始化项目",
                             "get_tasks": "查看任务和验收", "update_task": "更新任务进度", "runtime_check": "启动前后端并检查",
                             "http_request": "验证真实接口", "browser_check": "浏览器验收"}.get(name, name)
                    detail = f"{args.get('path', args.get('command', args.get('query', '')))}\n{result}"
                    if contract_error:
                        label = '工具参数需修正：' + label
                    on_step("tool", label, detail[:3000], tool_name=name,
                            tool_input=(call['function'].get('arguments') or '')[:12000],
                            tool_output=result[:12000], duration_ms=round((time.monotonic() - started) * 1000))
                    messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": session.output(call, result)})
                    journal.append({"tool": name, "input": {key: str(value)[:250] for key, value in args.items() if key not in {"content", "patch", "old", "new"}}, "result": result[:600]})
                    ledger.persist()
                    from system_contract import defer_dependency, refresh_deferred_dependencies
                    config_source = source_digest(snapshot_files(root))
                    refresh_deferred_dependencies(session.state, ledger.evidence, config_source, ledger.plan)
                    config_issue = configuration_issue(ledger.evidence, config_source, ledger.plan)
                    if config_issue:
                        key = [config_issue['evidence_id'], config_issue['resolution']]
                        changed = (session.state.get('configuration_guidance_receipt') != key
                                   if config_issue['resolution'] == 'auto_configure'
                                   else defer_dependency(session.state, config_issue))
                        if changed:
                            label, guidance = repair_guidance(config_issue)
                            recovery_messages.append({'role': 'user', 'content': guidance})
                            on_step('result', label, guidance)
                        session.state['configuration_guidance_receipt'] = key
                    session.checkpoint(messages, ledger, journal, snapshot_files(root))
                    (ledger.directory / "checkpoint.json").write_text(json.dumps({"tasks": ledger.tasks, "recent_actions": journal[-24:]}, ensure_ascii=False, indent=2))
                    set_workspace_owner(root, project_uid(project_id))
                messages.extend(recovery_messages)
                session.state['invalid_tool_turns'] = (session.state.get('invalid_tool_turns', 0) + 1
                                                       if rejected == len(tool_calls) else 0)
                session.checkpoint(messages, ledger, journal)
                if session.state['invalid_tool_turns'] >= 3:
                    session.state['invalid_tool_turns'] = 0
                    checkpoint_due = True
                    messages.append({'role':'user','content':'连续无效调用已跳过；现在改用其他有效工具完成剩余需求。系统会执行真实构建检查，依据实际反馈修复，禁止重复无效调用或修改已有正确业务代码来适配错误测试。'})
                    session.checkpoint(messages, ledger, journal)
                await compact_context()
                # Implementation uses scoped checks. At a delivery checkpoint,
                # inspect/build current source and verify real startup/core flows;
                # never run a complete unit-test suite as acceptance.
                if not checkpoint_due or (stabilizing and all(call['function']['name'] in {
                        'list_files', 'read_files', 'read_file', 'search_files', 'symbol_search', 'show_diff', *DISCOVERY_NAMES,
                        'list_documents', 'read_document', 'read_tool_output', 'get_tasks', 'update_task'} for call in tool_calls)):
                    continue
                on_step("thinking", "达到阶段检查点，自动验证当前代码", "")
            # Analysis from an earlier read/implementation turn is not a final
            # delivery summary. A tool checkpoint can finish without another
            # model request; use the verified platform summary in that case.
            if not tool_calls and all(t['status'] == 'done' for t in ledger.tasks):
                last_summary = content.strip() or last_summary
            if not tool_calls:
                messages.append({'role': 'assistant', 'content': content})
            session.checkpoint(messages, ledger, journal)
            current_files = snapshot_files(root)
            if not has_sources and not ledger.resumed and source_digest(current_files) == source_digest(initial_files) and not (ledger.plan.get('deliverables') and any(__import__('artifact_preview').artifact_fingerprint(root, ledger.plan).values())):
                if availability_error:
                    raise availability_error
                messages.append({"role": "user", "content": "项目文件尚未发生变化。请读取相关文件并实际完成需求。"})
                continue
            if any(t['status'] != 'done' for t in ledger.tasks) and not checkpoint_due:
                session.feedback(messages, 'task_gaps', '按当前任务完成实际需求并更新任务；使用已成功且 current_source=true 的验证 ID，无源码变化不要重复验收。completion_issues 是尚需处理的具体事项；不要重复总结。\n' + ledger.model_context(snapshot_files(root)))
                continue
            drift = conformance_issues(root, ledger.plan, current_files)
            if drift and not stabilizing:
                enforce_validation('conformance_check', 1, '\n'.join(drift))
                session.feedback(messages, 'conformance', "核对工程配置与计划，保持用户完整需求和已有业务：\n"+"\n".join(drift))
                on_step("result", "修正工程与计划的一致性", '\n'.join(drift))
                continue
            on_step("state", AgentState.TEST.value, "系统代码检查、真实构建与启动链路验证")
            inspection_code, inspection_report = inspect_sources(current_files)
            on_step('result', '系统代码检查', inspection_report[:3000])
            enforce_validation('source_check', inspection_code, inspection_report)
            if inspection_code:
                repair_feedback = inspection_report
                messages.append({'role': 'user', 'content': inspection_report + '\n只修复已定位的代码/配置问题；可用局部单元测试验证当前改动，不执行全量测试套件。'})
                on_step('state', AgentState.STABILIZE.value if stabilizing else AgentState.IMPLEMENT.value, '修复系统代码检查发现的问题')
                continue
            try:
                planned_build = ledger.plan['commands']['build'] if ledger.plan['application_type'] in ('cli', 'library', 'artifact') else []
                build_configured = has_build_command(root) or bool(planned_build)
                test_commands = []  # Web/service acceptance uses real startup and core workflows.
                if ledger.plan['application_type'] in ('cli', 'library'):
                    test_commands = [command for command in configured_tests(root, ledger.plan['commands']['test']) if not command_issue(command, root)]
                # Reuse only this run's successful build for identical source/config.
                # Browser evidence is reused across turns by delivery_fingerprint;
                # tool-issued builds always execute, and failed builds are never
                # memoized.
                build_key = source_digest(current_files)
                if successful_build and successful_build[0] == build_key:
                    build_code, build_output = 0, successful_build[1]
                    on_step('result', '复用已验证构建', '源码和配置未变化；继续真实启动与核心链路检查。')
                else:
                    build_code, build_output = await checked_execution("run_build", {"planned_commands": planned_build}, lambda: run_build(project_id, planned_build) if planned_build else run_build(project_id), reusable=True)
                    if build_code == 0:
                        successful_build = (source_digest(snapshot_files(root)), build_output)
            except (ValueError, OSError) as exc:
                if availability_error:
                    raise availability_error from exc
                messages.append({'role': 'user', 'content': '工作区构建/测试配置错误，请修复 .atoms-workspace.json 或项目配置后重新检查：' + str(exc)})
                on_step('result', '正在修复项目配置', str(exc)[:3000])
                on_step('state', AgentState.STABILIZE.value if stabilizing else AgentState.IMPLEMENT.value, '修复实际工作区配置')
                continue
            last_validation = f"构建 {'已配置' if build_configured else '未配置'} exit_code={build_code}\n{build_output[-6000:]}"
            on_step("result", ("构建通过" if build_code == 0 else "构建失败") if build_configured else "未配置构建命令",
                    build_output[:3000])
            if build_code == 0 and build_configured:
                current_digest = source_digest(snapshot_files(root))
                command_requirements = [r['id'] for r in ledger.plan['requirements'] if r['verification'] == 'command']
                if not any(e['kind'] == 'run_build' and e['exit_code'] == 0 and not e.get('historical')
                           and e['source_digest'] == current_digest and set(command_requirements).issubset(e['requirement_ids'])
                           for e in ledger.evidence):
                    ledger.record('run_build', '平台实际构建/类型检查', 0, build_output, current_digest, command_requirements)
            if build_code != 0:
                on_step("warning", "构建未通过", "Agent 将读取具体错误并有限次修复")
            if build_code:
                repair_feedback = last_validation[-9000:]
                messages.append({"role": "user", "content": f"验证失败。分析原因、修复代码并重新验证：\n{last_validation[-9000:]}"})
                if availability_error:
                    session.checkpoint(messages, ledger, journal, snapshot_files(root))
                    raise availability_error
                on_step("state", AgentState.STABILIZE.value if stabilizing else AgentState.IMPLEMENT.value, "根据代码或构建错误修复")
                continue
            # A build is insufficient: verify the actual runnable result even on
            # the fully completed path, so a normal exit cannot skip demo checks.
            all_tasks_closed = not any(t['status'] not in ('done', 'deferred') for t in ledger.tasks)
            normal_web_demo = normal_web_mode and not normal_api_required and (stabilizing or all_tasks_closed)
            if stabilizing or availability_error or all_tasks_closed:
                from delivery_checks import resolve_scene, DeliverySceneError
                async def scene_resolver(observed):
                    return await resolve_scene(root, ledger.plan, observed, session, gateway, client, execution_tools)
                scene_diagnostic = ''
                current_files = snapshot_files(root)
                delivery_key = delivery_fingerprint(root, ledger.plan, current_files, session)
                delivery_cache = session.state.get('delivery_verification') or {}
                deferred_delivery = bool(session.state.get('deferred_dependencies') or any(t['status'] == 'deferred' for t in ledger.tasks))
                # Normal mode is intentionally a fast visual delivery path. It
                # still runs the real service and JavaScript startup gate, but
                # skips the expensive full API/browser journey unless a deeper
                # tier or an explicit recovery run asks for it.
                preview_only_delivery = ledger.plan['application_type'] == 'web' and bool(
                    (normal_web_mode and not normal_api_required) or stabilizing or availability_error or deferred_delivery)
                try:
                    if availability_error:
                        # Provider availability may leave TODOs; still execute real startup for demo delivery.
                        demo_code, demo_output = await await_or_stop(verify_delivery(
                            project_id, root, ledger.plan, require_scenario=True,
                            scene_resolver=scene_resolver, preview_only=preview_only_delivery, session=session), should_stop)
                    elif (final_verified_key == delivery_key and delivery_cache.get('key') == delivery_key and delivery_cache.get('mode') == 'full'
                            and delivery_cache.get('report')):
                        demo_code, demo_output = 0, ('已复用成功的演示验收（源码、运行配置和验收场景均未变化）。\n' +
                                                      delivery_cache['report'])
                        on_step('result', '复用已验证演示', '当前源码版本和验收合同未变化，跳过重复浏览器操作。')
                    else:
                        attempts = session.state.setdefault('delivery_attempts', {})
                        entry = attempts.setdefault(delivery_key, {'count': 0, 'failures': []})
                        entry['count'] = entry.get('count', 0) + 1
                        session.save('delivery_attempt', {'key': delivery_key, 'count': entry['count']})
                        demo_code, demo_output = await await_or_stop(verify_delivery(project_id, root, ledger.plan, require_scenario=True, scene_resolver=scene_resolver, preview_only=preview_only_delivery, session=session), should_stop)
                except DeliverySceneError as exc:
                    scene_diagnostic = str(exc)
                    session.state['scene_diagnostic'] = scene_diagnostic
                    session.state.pop('demo', None)
                    repair_feedback = ('核心流程的浏览器验收场景需要修正：' + scene_diagnostic +
                        '\n保留已通过的业务代码，不盲目重构。根据实际控件用 browser_check 运行完整核心流程，'
                        '将真实通过的 actions 保存为 .atoms-workspace.json demo；不能只断言登录页/body 可见。'
                        '后续会在真实预览网关重新执行该场景，验证提交后的数据读取与受影响流程。')
                    messages.append({'role': 'user', 'content': repair_feedback})
                    session.checkpoint(messages, ledger, journal, snapshot_files(root))
                    on_step('result', '正在补充核心流程验收', '保留代码与启动结果，修正交互验收场景后继续验证实际用户流程。')
                    on_step('state', AgentState.STABILIZE.value if stabilizing else AgentState.IMPLEMENT.value, '补充实际核心流程验证')
                    scene_failures = session.state.get('scene_protocol_failures', 0) + 1
                    session.state['scene_protocol_failures'] = scene_failures
                    session.checkpoint(messages, ledger, journal, snapshot_files(root))
                    if scene_failures >= 2:
                        execution_guard.request_recovery('delivery_check', scene_diagnostic, '交互检查场景错误，依据真实控件/接口验证不同场景假设，禁止降级为首页验收。')
                    continue
                on_step('result', '演示检查通过' if demo_code == 0 else '正在修复演示环境', demo_output[:3000])
                if demo_code:
                    if availability_error:
                        messages.append({'role':'user','content':'自动恢复后优先修复已定位的演示问题：'+demo_output[-6000:]})
                        session.checkpoint(messages, ledger, journal, snapshot_files(root))
                        raise availability_error
                    entry = session.state.setdefault('delivery_attempts', {}).setdefault(delivery_key, {'count': 1, 'failures': []})
                    entry.setdefault('failures', []).append(demo_output[-3000:])
                    entry['failures'] = entry['failures'][-3:]
                    session.state.pop('demo', None)
                    repair_feedback = demo_output[-9000:]
                    messages.append({'role': 'user', 'content': '实际演示检查发现问题，修复具体原因后再检查：\n' + demo_output[-9000:]})
                    on_step('state', AgentState.STABILIZE.value if stabilizing else AgentState.IMPLEMENT.value, '修复实际启动或预览问题')
                    continue
                last_validation += '\n演示检查：\n' + demo_output[-6000:]
                current_files = snapshot_files(root)
                if not preview_only_delivery:
                    # Scene grounding may have populated the durable phase
                    # during this very check; calculate the cache key again so
                    # the next loop sees the exact same identity.
                    verified_key = delivery_fingerprint(root, ledger.plan, current_files, session)
                    final_verified_key = verified_key
                    session.state['delivery_verification'] = {
                        'key': verified_key, 'mode': 'full', 'report': demo_output[-12000:],
                        'source_digest': source_digest(current_files), 'verified_at': time.time(),
                    }
                session.state['demo'] = {'ready': True, 'kind': ledger.plan['application_type'],
                                         'report': demo_output[-12000:], 'source_digest': source_digest(current_files)}
                session.state['demo']['core_interactions_verified'] = not preview_only_delivery and (ledger.plan['application_type'] != 'web' or browser_enabled(ledger.plan))
                session.state['demo']['browser_check_enabled'] = browser_enabled(ledger.plan)
                session.state['demo']['startup_policy'] = 'javascript-http-v1'
                session.state['demo']['task_type'] = ledger.plan.get('task_type', 'software')
                session.state['demo']['deliverables'] = ledger.plan.get('deliverables', [])
                if ledger.plan.get('deliverables'):
                    ledger.record('artifact_check', '实际成果内容与预览验收', 0, demo_output, source_digest(current_files),
                                  [r['id'] for r in ledger.plan['requirements'] if r['verification']=='artifact'])
                if availability_error:
                    session.state['delivery_limitations'] = ['智能体模型服务暂不可用；已保留当前可演示版本，剩余开发可稍后继续。']
                if stabilizing or availability_error or deferred_delivery or normal_web_demo:
                    # Delivery checks can cover implemented features whose full
                    # task receipts/review remain open. Do not label those same
                    # visible features as work that still needs implementing.
                    summary = ('应用已启动，可在应用查看器预览当前版本（verified: 服务启动检查通过）。' if preview_only_delivery else
                               '演示版本已就绪，已通过实际运行与演示检查。')
                    from system_contract import delivery_todo
                    summary += '\n\n' + delivery_todo(root, ledger, session.state, current_files)
                    current_files = snapshot_files(root)
                    summary += '\n\n配置外部依赖或继续开发时可沿用当前代码；以上延期/模拟能力不代表真实服务验收完成。'
                    budget.phase = 'demo_ready'
                    publish_budget()
                    session.state['summary'] = summary
                    session.checkpoint(messages, ledger, journal, current_files)
                    on_step('state', AgentState.PREVIEW_READY.value, '实际启动与演示检查通过，需求与检查点已保存')
                    return {'summary': summary, 'preview_html': preview_document(root), 'files': current_files, 'delivery': 'demo'}
            current_files = snapshot_files(root)
            completion_issues = ledger.completion_issues(current_files)
            if ledger.plan["application_type"] == "web" and not build_configured:
                completion_issues.append("Web 项目必须配置并通过真实 build 命令")
            if completion_issues:
                enforce_validation('completion_check', 1, '\n'.join(completion_issues))
                on_step("result", "正在补充功能验收", "进一步验证当前需求与实现的一致性。")
                session.feedback(messages, 'completion', "构建通过并不代表需求完成。尽可能解决以下缺口；难解部分可用 deferred 记录 TODO，继续其他功能并保证最终演示：\n" + "\n".join(completion_issues))
                on_step("state", AgentState.IMPLEMENT.value, "继续完成任务与真实验收")
                continue
            # Acceptance is grounded in actual build, fresh requirement
            # receipts, and real startup/core workflow checks above. Do not launch a
            # second source-reading model loop after the executable result is verified.
            ledger.completed = True
            ledger.persist()
            summary = last_summary or ("应用已启动，核心功能链路已验证，可在应用查看器预览。"
                                       if ledger.plan['application_type'] == 'web' else "成果已生成，实际文件内容与预览验证通过，可直接查看和下载。" if ledger.plan['application_type'] == 'artifact' else "项目已完成，核心功能运行验证通过。")
            session.state['summary'] = summary
            session.state['acceptance'] = {
                'mode': 'runtime_and_core_workflows',
                'source_digest': source_digest(current_files),
                'report': last_validation[-12000:],
            }
            session.checkpoint(messages, ledger, journal, current_files, completed=True)
            on_step("result", "核心功能验收通过", "服务启动、应用预览和核心功能链路验证通过。" if ledger.plan["application_type"] == "web" else "核心功能的实际执行验证通过。")
            note = "核心功能运行验收通过；" + ("系统代码检查、构建与真实运行校验通过" if build_configured or test_commands else "未配置统一构建命令")
            on_step("state", AgentState.COMPLETE.value, f"文件已保存；{note}")
            if not build_configured and not test_commands and ledger.plan["application_type"] != "artifact":
                summary += "\n\n未配置统一构建命令；功能已由实际执行工具验收。"
            return {"summary": summary, "preview_html": preview_document(root), "files": current_files}



async def make_plan(project_id: uuid.UUID, prompt: str, model: str, media_context: str = "",
                    on_step: Callable | None = None, history: list[dict] | None = None, expert_context: str = "", build_tier: str = "normal", enabled_tools: list[str] | None = None, resume_planning: bool = False, initial_tokens: int | None = None):
    build_tier = normalize_tier(build_tier)
    root = ensure_workspace(project_id)
    gateway = ModelGateway(model, (lambda state, metrics: on_step(
        "usage", state, metrics["model"], token_usage=metrics, duration_ms=metrics["duration_ms"])) if on_step else None)
    gateway.cache_scope = project_id.hex
    session = AgentSession(root)
    locator = CodeLocator(root, session)
    current_sources = locator.sync()
    from incremental_planning import (baseline_plan, change_contract, assemble_plan,
                                      verified_observations, exploration_limit, plan_object)
    baseline = baseline_plan(root, session, current_sources)
    saved_memory = session.planning_context(current_sources)
    files = list_files(root)
    useful_history = [item for item in (history or []) if item['content'] != prompt and
                      (not saved_memory or item['role'] == 'user')]
    history_text = "\n".join(f"{item['role']}: {item['content']}" for item in useful_history[-6:])
    known = session.state.get('files', {})
    # Existing project knowledge is reused. Read only genuinely changed source
    # excerpts; the planner can request precise additional files with its tools.
    changed = []
    if known:
        from agent_session import inventory
        current_inventory = inventory(current_sources)
        changed = [name for name in files if current_inventory.get(name) != known.get(name)]
    # A directory manifest plus verified project memory is enough to select
    # precise reads. Automatic excerpts used to be read again during exploration.
    relevant = '按需读取以下变化文件：' + ', '.join(changed[:60]) if known else '需要源码时使用只读工具，文件清单已提供。'
    if saved_memory and on_step:
        on_step('result', '已加载项目记忆', '沿用已确认的架构和实现记录；只分析当前新增要求及变化文件。')
    if on_step:
        on_step('result', '已更新代码定位索引', '按文件元数据复用本地事实，只重建变化文件的索引；源码正文按需检索。',
                tool_name='code_index', tool_output=json.dumps(locator.stats, ensure_ascii=False))
    if on_step:
        on_step("state", AgentState.UNDERSTAND.value, "理解完整需求及现有项目")
    from project_templates import planner_contract
    template_contract = planner_contract(prompt, history, existing_files=files)
    new_project = not any(not n.startswith(".atoms/") for n in current_sources)
    project_facts = "实际项目状态：全新项目，无业务源码。必须给出确定的技术栈、frontend 目录、具体业务文件和实际命令，不能写沿用现有/若为新项目等条件。" if new_project else "实际项目状态：已有业务源码。先定位真实入口和运行配置，沿用实际技术栈与明确目录，不能写条件目录或命令说明。"
    messages = [
        {"role": "system", "content": PLAN_INSTRUCTIONS + tool_instructions(enabled_tools if enabled_tools is not None else ['browser_check'], build_tier) + '\n模板策略（优先于通用初始化说明）：' + template_contract + "\n按真实需求规模规划：简单单页或小功能只安排 1–3 个连贯任务；功能范围按构建质量策略决定，避免不必要的后端或复杂架构。新项目只有 .atoms 文档时无需读这些初始化元数据，直接制定计划。新项目默认按 demo-first：先输出最小接口契约与前端目标预览，再接后端真实实现；不要让数据库、AI、支付或其他外部凭据阻塞页面预览。需要后端的新项目，tasks 让接口契约和前端预览先行，后端接线作为后续任务；已有项目按真实新增能力和依赖安排任务，不重复预览初始化；接口、模拟边界和 TODO 要明确记录。先形成完整系统设计并打通风险最高的真实链路，再集中完成其余模块；可预览不能替代完整目标。" + expert_context + change_contract(baseline) + tier_instructions(build_tier)},
        {"role": "user", "content": f"完整对话：\n{history_text}\n当前需求：{prompt}\n附件参考：{media_context or '无'}\n"
         f"{project_facts}\n项目记忆：\n{saved_memory}\n现有文件：{', '.join(files[:200])}\n相关变化代码：\n{relevant}\n"
         "完成需求和架构判断。已有项目按需使用只读工具；新项目或信息充足时直接输出完整计划 JSON。"},
    ]
    # One tool-driven planning loop covers understanding, exploration and plan.
    # Phase state survives interruption and passes verified source observations on.
    from agent_session import inventory, repair_tool_boundaries
    phase_identity = {'request': prompt, 'files': inventory(current_sources), 'media': session_digest(media_context),
                                'policy': 'incremental-v7-evidence-repair', 'enabled_tools': enabled_tools, 'build_tier': build_tier, 'build_policy_version': POLICY_VERSION, 'expert': expert_context, 'model': gateway.model_for(AgentState.PLAN) if hasattr(gateway, 'model_for') else model}
    context_key = session_digest(phase_identity)
    phase_key = session_digest({**phase_identity, 'history': history_text})
    phase = session.phase('planning', phase_key, messages)
    initialize_context = len(phase['messages']) == 2
    previous = session.saved_phase('planning') if resume_planning else {}
    if previous.get('request') == prompt and not previous.get('completed', True):
        if previous.get('context_key') == context_key:
            phase = previous
            initialize_context = len(phase['messages']) == 2
        else:
            # New source/config invalidates the old transcript, but keep its
            # draft as explicitly unverified input for the model to revise.
            from incremental_planning import plan_object
            candidate = previous.get('candidate')
            if not candidate:
                for message in reversed(previous.get('messages', [])):
                    if message.get('role') == 'assistant' and not message.get('tool_calls'):
                        try:
                            candidate = plan_object(message.get('content') or '')
                            break
                        except ValueError:
                            pass
            if candidate:
                phase['candidate'] = candidate
                phase['messages'].append({'role': 'user', 'content': '恢复上次未完成的规划；以下草稿未经当前源码校验，以本次实际文件事实为准，可用 plan_patch 修正：\n' + json.dumps(candidate, ensure_ascii=False)})
            phase['last_error'] = previous.get('last_error', '')
    phase['context_key'] = context_key
    phase['request'] = prompt
    phase['build_tier'] = build_tier
    phase['incremental'] = bool(baseline)
    # Keep a normal plan compact in content, while reserving enough completion
    # budget for one complete JSON object instead of an avoidable retry.
    if not phase.get('output_budget') or phase.get('output_budget') in (8000, 12000):
        phase['output_budget'] = model_output_limit(gateway.model_for(AgentState.PLAN) if hasattr(gateway, 'model_for') else model, AgentState.PLAN)
    if initialize_context:
        explicit_paths = [path for path in files if path in prompt]
        reused = verified_observations(session, current_sources, preferred_paths=explicit_paths) if baseline else {}
        phase['observations'].update(reused)
        if reused:
            phase['messages'][1]['content'] += '\n已按完整文件哈希核验的历史源码区间（无需重复读取）：\n' + '\n'.join(o['text'] for o in reused.values())
        architecture_doc = read_file(root, '.atoms/ARCHITECTURE.md') if '.atoms/ARCHITECTURE.md' in files else ''
        if baseline and architecture_doc:
            phase['messages'][1]['content'] += '\n当前项目架构文档（以实际源码为准）：\n' + architecture_doc[:6000]
        if baseline:
            phase['messages'][1]['content'] += '\n当前模块入口概览（本地事实，非完成证明）：\n' + locator.overview()
            phase['messages'][1]['content'] += '\n增量工作流程：先解释当前 prompt 的新增能力、约束和可观察验收；提取需要查找的界面文案、API、实体、英文符号及同义词，用 locate_change/glob_files/search_code 定位，再用一次 read_files 读取同一功能的接口、类型、路由、服务和调用方，必要时再 read_code 分页。追踪 imports/imported_by 及接口消费者；无命中要换术语，不能猜路径或断言功能不存在。任务 depends_on 只表达真实的数据/接口依赖；前端预览、后端接口、文档和独立配置没有真实依赖时应放在可并行的任务组，避免人为串行。最终计划必须有 change_map:[{"path":"真实现有或拟新建文件","operation":"modify|create|delete","reason":"对应新需求及修改原因","requirement_ids":["R1"]}]。保留原功能，说明迁移/兼容和对应回归；change_map 与 tasks.files 的具体文件路径必须一致；不能用目录或猜测的路径。改动位置、任务与验收须覆盖所有当前 requirements。'
    gateway.session = session
    if phase.get('completed'):
        return json.dumps(stamp_tools(stamp_plan(validate_plan(phase['plan']), build_tier), enabled_tools), ensure_ascii=False)
    if not phase.get('runtime_capabilities_provided'):
        phase['messages'].append({'role': 'user', 'content': '实际运行能力边界：项目服务使用独立 UID 和项目托管 PostgreSQL 连接。代码智能体模型网关及计费凭据仅供 harness，不注入生成的业务服务。不要规划为“已有 OPENAI_API_KEY”。需要真实外部 AI/邮件/支付时声明 dependencies 的实际配置和探测，未绑定服务记录 TODO 并提供明确标注的模拟演示适配，继续其他功能；不冒充真实 provider 成功。实际预览采用 BASE_PATH，API 必须使用同源代理；浏览器工具只在用户启用时使用。'})
        phase['runtime_capabilities_provided'] = True
    messages = repair_tool_boundaries(phase['messages'])
    if on_step:
        on_step('state', AgentState.EXPLORE.value, '分析新增需求影响；复用已有架构与有效源码观察' if baseline else '只检查必要源码；新项目直接规划')
        on_step('state', AgentState.PLAN.value, '制定增量功能计划，保留已有业务' if baseline else '合并理解、按需探索和完整计划生成')
    exploration_tools = [tool for tool in TOOLS if tool['function']['name'] in
                         {'list_files', 'read_files', 'read_file', 'search_files', 'symbol_search', 'read_tool_output', *DISCOVERY_NAMES}]
    if not any(not name.startswith('.atoms/') for name in current_sources):
        exploration_tools = []  # Empty new projects have no business source to explore.
    async with httpx.AsyncClient(timeout=180) as client:
        read_rounds = exploration_limit(bool(baseline)) if exploration_tools else 0
        from planning_recovery import PlanningRecovery
        recovery = PlanningRecovery(session, phase, read_rounds, bool(exploration_tools), initial_tokens=initial_tokens)
        announced_mode = None
        while True:
            active_tools = exploration_tools if recovery.tools_enabled() else []
            if recovery.mode == 'synthesis' and announced_mode != 'synthesis':
                messages.append({'role': 'user', 'content': '现在依据实际源码观察和当前要求提交可执行计划 JSON；停止泛读，仍有事实冲突时校验恢复会重新开放只读工具。保持用户目标与全部需求；设计简洁具体，不重复历史文档。若已有计划草稿，优先用 plan_patch 局部修正，避免重生成整个计划。'})
                if on_step:
                    on_step('result', '正在形成增量计划' if baseline else '正在形成实现计划', '复用已确认的源码观察，将当前需求整理为可执行任务与验收步骤。')
            announced_mode = recovery.mode
            phase['messages'] = messages
            session.save_phase('planning', phase)
            # Planning is a bounded handoff, not a second implementation
            # session. Large output budgets made malformed plans expensive to
            # retry and encouraged the model to repeat design prose. Keep the
            # first plan compact; only a length-truncated response gets the
            # slightly larger repair budget below.
            # Normal plans are intentionally compact, but need enough room for
            # one complete JSON handoff.  The old 8k ceiling regularly cut the
            # first plan in half and forced a second model call before any code
            # could be written.  A single larger handoff is faster overall.
            plan_ceiling = model_output_limit(gateway.model_for(AgentState.PLAN) if hasattr(gateway, 'model_for') else model, AgentState.PLAN)
            response = await phase_chat(session, gateway, client, AgentState.PLAN, messages, active_tools, 'planning', phase,
                                        min(plan_ceiling,
                                            int(phase.get('output_budget', plan_ceiling))),
                                        before_call=recovery.before_call)
            recovery.record_response(response)
            phase['last_response_meta'] = response.get('_response_meta', {})
            calls = response.get('tool_calls') or []
            if calls:
                messages.append({'role': 'assistant', 'content': response.get('content') or '', 'tool_calls': calls,
                                 **{k: response[k] for k in ('reasoning', 'reasoning_details') if k in response}})
                phase['messages'] = messages
                session.save_phase('planning', phase)
                for call in calls:
                    args = {}
                    adjustments = []
                    observed = False
                    name = call['function']['name']
                    try:
                        args = parse_tool_arguments(call, active_tools, response, adjustments)
                        if name == 'list_files':
                            result = json.dumps(files, ensure_ascii=False)
                        elif name == 'read_files':
                            sections = []
                            for item in args['files']:
                                offset, limit = int(item.get('offset', 0)), int(item.get('limit', READ_LIMIT_MAX))
                                if offset < 0 or not 1 <= limit <= READ_LIMIT_MAX:
                                    raise ValueError(f"{item['path']} 的文件读取范围无效")
                                rendered, sha = session_read_file(session, root, item['path'], offset, limit, messages)
                                sections.append(rendered)
                                phase['observations'][item['path'] + ':' + str(offset)] = {
                                    'path': item['path'], 'sha': sha,
                                    'text': sections[-1]}
                            result = '\n\n'.join(sections)
                        elif name == 'read_file':
                            offset, limit = int(args.get('offset', 0)), int(args.get('limit', 6000))
                            if offset < 0 or not 1 <= limit <= READ_LIMIT_MAX:
                                raise ValueError('无效的读取范围')
                            result, sha = session_read_file(session, root, args['path'], offset, limit, messages)
                            phase['observations'][args['path'] + ':' + str(offset)] = {
                                'path': args['path'], 'sha': sha, 'text': result}
                        elif name == 'search_files':
                            result = search_files(root, args['query'])
                        elif name == 'symbol_search':
                            result = symbol_search(root, files, args['query'])
                        elif name == 'read_tool_output':
                            result = session.read_output(args['output_id'], args.get('offset', 0), args.get('limit', 6000))
                        elif name in DISCOVERY_NAMES:
                            if name == 'read_code':
                                result, text, offset = locator.read(args, messages)
                                phase['observations'][args['path'] + ':' + str(offset)] = {
                                    'path': args['path'], 'sha': inventory({args['path']: text}).get(args['path']), 'text': result}
                            else:
                                result = locator.execute(name, args, messages)
                        else:
                            raise ValueError('规划阶段仅允许只读工具')
                        observed = True
                    except ToolArgumentError as exc:
                        result = exc.result()
                    except (ValueError, KeyError, OSError, TypeError) as exc:
                        result = f'工具错误: {exc}'
                    if observed:
                        paths = [item['path'] for item in args['files']] if name == 'read_files' else [args['path']] if name in {'read_file', 'read_code'} else None
                        versions = {path: locator.entries.get(path, {}).get('sha') for path in paths} if paths is not None else None
                        recovery.observe(name, args, result, source_versions=versions)
                    if adjustments:
                        result += '\n参数已按读取页大小调整：' + json.dumps(adjustments, ensure_ascii=False)
                    messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': session.output(call, result)})
                    phase['messages'] = messages
                    session.save_phase('planning', phase)
                    if on_step:
                        on_step('tool', {'locate_change': '规划：定位需求影响', 'glob_files': '规划：查找相关文件',
                                         'search_code': '规划：搜索相关调用', 'read_code': '规划：读取相关代码区间'}.get(name, '规划阶段读取项目'), result[:3000], tool_name=name,
                                tool_input=json.dumps(args, ensure_ascii=False), tool_output=result[:12000])
                continue
            text = response.get('content') or ''
            try:
                if phase['last_response_meta'].get('finish_reason') == 'length':
                    phase['output_budget'] = model_output_limit(gateway.model_for(AgentState.PLAN) if hasattr(gateway, 'model_for') else model, AgentState.PLAN)
                    raise ValueError('计划输出达到长度限制；缩短重复设计描述，直接提交完整 JSON。下一次将使用模型阶段上限')
                value = resolve_default_plan(recovery.candidate(text), prompt, history, new_project=new_project)
                plan = stamp_tools(stamp_plan(assemble_plan(value, baseline), build_tier), enabled_tools)
                from system_contract import require_system_contract
                require_system_contract(plan)
                if baseline:
                    plan = validate_change_map(plan, locator)
                phase.update(completed=True, plan=plan, plan_hash=session_digest(plan), last_error='', validation_issues=[])
                phase.pop('checkpoint_reason', None)
                session.save_phase('planning', phase)
                (root / '.atoms' / 'requirements.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2))
                set_workspace_owner(root, project_uid(project_id))
                return json.dumps(plan, ensure_ascii=False)
            except (ValueError, TypeError, AttributeError, KeyError) as exc:
                messages.append({'role': 'assistant', 'content': text})
                phase['messages'] = messages
                feedback = recovery.feedback(exc, locator)
                messages.append({'role': 'user', 'content': feedback})
                phase['messages'] = messages
                session.save_phase('planning', phase)
                if on_step:
                    on_step('recovery', '正在分析并修正计划冲突', str(exc),
                            tool_name='plan_validation',
                            tool_output=json.dumps({'issues': phase['validation_issues'], 'mode': recovery.mode}, ensure_ascii=False))


def prepare_local_vision_image(path: Path) -> tuple[str, str]:
    with Image.open(path) as source:
        source.seek(0)
        original_size = source.size
        alpha = source.convert("RGBA").getchannel("A")
        transparent = alpha.getextrema()[0] < 255
        source.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
        image = Image.new("RGB", source.size, "white")
        if source.mode in ("RGBA", "LA") or "transparency" in source.info:
            overlay = source.convert("RGBA")
            image.paste(overlay, mask=overlay.getchannel("A"))
        else:
            image.paste(source.convert("RGB"))
        output = io.BytesIO()
        image.save(output, "JPEG", quality=88, optimize=True)
    metadata = f"原图 {original_size[0]}×{original_size[1]} 像素；" + ("含透明像素，白色合成背景不是原图背景" if transparent else "无透明像素")
    return base64.b64encode(output.getvalue()).decode("ascii"), metadata


async def analyze_attachments(project_id: uuid.UUID, prompt: str, model: str, attachments: list[dict], on_step=None):
    if not attachments:
        return "", model
    if any(item["kind"] != "image" for item in attachments):
        raise RuntimeError("目前仅支持图片附件；视频识别已关闭")
    key = os.getenv("AI_API_KEY", "").strip()
    if not key and not os.getenv("WORKER_TOKEN"):
        raise RuntimeError("未配置 AI_API_KEY，无法运行代码智能体")
    base_url = os.getenv("AI_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    if base_url.endswith("/chat/completions"):
        base_url = base_url[:-len("/chat/completions")]
    root = project_root(project_id)
    instruction = ("按每张图片的文件名分别报告能直接确认的视觉事实，不要混淆不同图片，不要提供设计建议或实现步骤。"
                   "逐项记录可见文字、区域布局、主体与背景颜色、物体及控件；最后说明图片之间可确认的关系。"
                   "对于看不清的衣物、表情、文字等明确写不确定，不要由颜色或用户需求推测画面内容。"
                   "用户关注点：" + prompt)
    async with httpx.AsyncClient(timeout=240) as client:
        response = await client.get(f"http://agent-service:9001/projects/{project_id}/models" if os.getenv("WORKER_TOKEN") else f"{base_url}/models", headers={"X-Worker-Token": os.environ["WORKER_TOKEN"]} if os.getenv("WORKER_TOKEN") else {"Authorization": f"Bearer {key}"})
        response.raise_for_status()
        models = {item["id"]: set(item.get("architecture", {}).get("input_modalities") or [])
                  for item in response.json().get("data", [])}
        if model not in models:
            raise RuntimeError(f"无法确认模型 {model} 的图片输入能力")
        files = [(item, base64.b64encode((root / item["relative_path"]).read_bytes()).decode("ascii"))
                 for item in attachments]
        source = ""
        description = ""
        if "image" in models[model]:
            content: list[dict] = [{"type": "text", "text": instruction}]
            for item, encoded in files:
                content.append({"type": "text", "text": f"图片：{item['filename']}"})
                content.append({"type": "image_url", "image_url": {
                    "url": f"data:{item['mime_type']};base64,{encoded}"}})
            gateway = ModelGateway(model, (lambda state, metrics: on_step("usage", "图片识别", model, token_usage=metrics, duration_ms=metrics['duration_ms'])) if on_step else None)
            message = await gateway.chat(client, AgentState.UNDERSTAND, [{"role": "user", "content": content}],
                                         max_tokens=model_output_limit(model, AgentState.UNDERSTAND))
            description = message.get('content') or ''
            source = model
        else:
            worker_token = os.getenv("WORKER_TOKEN", "")
            if not worker_token:
                raise RuntimeError("本地图片识别只能在项目隔离工作容器中运行")
            descriptions = []
            for item, _ in files:
                encoded, metadata = prepare_local_vision_image(root / item["relative_path"])
                response = await client.post(f"http://agent-service:9001/projects/{project_id}/vision",
                    headers={"X-Worker-Token": worker_token},
                    json={"prompt": f"图片名：{item['filename']}。{metadata}。{instruction}", "image": encoded})
                if response.status_code >= 400:
                    raise RuntimeError(f"本地图片识别失败 ({response.status_code}): {response.text[:300]}")
                result = response.json()
                descriptions.append(f"{item['filename']}（{metadata}）：{result['description']}")
                source = f"本地 {result['model']}"
            description = "\n\n".join(descriptions)
        if not isinstance(description, str) or not description.strip():
            raise RuntimeError("图片识别未返回可用内容")
        paths = "\n".join(f"- {item['filename']}: {item['relative_path']}" for item in attachments)
        return (f"图片识别来源：{source}。以下是视觉模型的参考描述，无法确认的细节应保持保守。"
                f"\n附件已保存在本项目工作区，必要时可复制到 public 目录供应用使用：\n{paths}\n\n"
                + description.strip())[:12000], source
