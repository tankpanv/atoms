"""Persistent requirements, execution evidence and completion gates for coding jobs."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from execution_contract import validate_command, validate_directory, conformance_issues
from build_tools import browser_enabled


PLAN_INSTRUCTIONS = """你是负责理解真实用户目标并交付完整成果的任务规划师。先理解全部对话、现有代码及附件，再制定计划。
初始工作区可能没有应用代码；不能因为存在 React 文件就认定需求只需要前端。
先明确用户目标、关键流程、数据、状态、失败路径、持久化和权限。根据需求决定 frontend/backend/fullstack；
共享数据、账号、跨设备同步、服务端计算、秘密凭据、外部服务集成需要真实后端。纯展示或明确仅本地工具可不建后端，说明原因。
不为展示一个目录树而制造无用后端。保留真实后端和数据交互；未配置外部服务可用明确标注的模拟适配器演示，不能暗中替换成假数据/空按钮或声称真实服务已验收。
用户未指定栈时前端使用 Vite React TypeScript 默认版本化模板；执行器自动安装，按实际职责组织模块；简单页面/小游戏用页面加领域模块即可，不为每个辅助函数创建独立文件。
用户未指定栈且需要后端时使用 FastAPI + PostgreSQL 默认模板（平台数据库连接器，每项目独立 Schema 和受限应用账号）；用户指定技术栈时遵循要求；按路由、业务逻辑、数据访问和 schema 分层。 后端模板已提供 app/routers、app/schemas、app/services 和 /api/ready；优先复用这些边界，不要重新搭建大型框架。
已有项目保持栈和功能，先检查再迁移。App.tsx 是组合入口，不承担全部业务。
所有档位完整覆盖用户实际要求；补充细节和必要扩展按本次构建质量策略决定，不能把无关增强功能变成验收要求。
例如“做一个24点游戏”：四张牌、算式输入与每张牌使用一次的验证、判定24、可解题目及换题/参考答案即可；
普通档没有明确要求时不加难度、倒计时、积分体系、历史统计、账号或云端持久化；其他档位按质量策略解释必要补充，用户明确禁止的能力任何档位都不加入。
所有明确要求仍必须实现；本次质量策略纳入的补充项也要编入任务和验收；非必要的可选增强放 next_steps。
列出可观察的验收步骤及依赖有序的任务。简单需求合并为1–2个连贯功能任务，集中写入相关文件再验证，避免逐文件计划。初始化脚手架属于第一个产品任务的一部分，不单独建立需要通过尚未实现业务测试的任务。
计划要贯穿完整用户流程：先确定共享类型/API/数据模型和模块依赖，再将界面、真实业务与必要持久化整合进可验收的功能单元；不要先分别写完所有页面再补业务。
先验证关键技术风险（依赖、外部服务与持久化可用性），避免完成大量界面后才发现架构不可运行。实现阶段尽早验证第一个端到端流程，随后完成全部明确要求，并预留构建、服务启动、核心功能链路验收和具体故障修复的时间。代码质量在编写过程中保证；最终验收不安排逐文件源码审查，针对观察到的启动、接口、交互或持久化错误定位相关代码并修复。
简单任务建议按少量调用完成“集中实现→构建和针对性测试→真实功能验收”，复杂任务按依赖分批闭环。建议节奏不是缩减需求的上限；超出预期时改进实现方法和定位阻塞，而不是自动把完整目标改成只做演示。
多模块先给出 interfaces:[{"path":"模块路径","exports":["精确的类型或函数签名"],"consumers":["调用模块路径"]}]；实现时遵循契约，不能各自猜导出名。
web 可提供 delivery_checks:{"path":"/","actions":[...]}，使用约定的 data-testid 进行核心交互和具体内容断言。
动作格式必须是 {"action":"fill","selector":"[data-testid=name-input]","value":"访客"}、{"action":"click","selector":"[data-testid=submit]"}、{"action":"assert_text","selector":"[data-testid=feedback]","value":"成功"}、{"action":"reload"}；动作键为 action，不是 type，断言内容键为 value，不是 text。
这是执行器的验证合同，无需让模型反复编辑 demo 配置；未提供时执行器会基于实际页面生成检查场景。
按用户可验收的能力拆分任务，不把每个文件、函数、文档或每次提交各拆成任务。简单功能通常合并实现及真实链路验证。
只读与本需求相关的必要文件；给定文件清单和已验证源码无需再次读取。无源码的新项目直接规划。
未经用户要求或本次质量策略的必要性分析，不增加账号、部署、提交记录等能力。每次回复直接给工具调用或最终 JSON，避免重复叙述完整计划。
目录、tasks.files、interfaces.path/consumers 必须是具体相对路径，不得写“现有项目的相关页面”“沿用现有目录”等说明。commands 只接受可直接执行的命令（如 npm run build 或 python app.py）；自然语言放 verification/design。已有源码或模板已提供时先核对真实入口，再沿用配置；失败先区分协调器配置错误、工具参数、环境故障与业务错误，不可反复改正确代码。
返回一个严格 JSON 对象，结构如下（示例只是字段说明，内容必须根据本需求设计）：
{"goal":"目标", "application_type":"web|service|cli|library",
 "architecture":{"frontend":{"stack":"栈或 none","directory":"frontend 或现有目录"},
 "backend":{"required":true,"reason":"是否需要后端的具体理由","stack":"栈或 none","directory":"backend"},
 "persistence":"数据模型及存储决策"}, "design":"页面、模块、API 契约、实体关系、错误处理、边界与实现设计",
 "requirements":[{"id":"R1","description":"具体需求","acceptance":["用户操作及期望结果"],"verification":"browser|api|command"}],
 "tasks":[{"id":"T1","title":"实现单元","requirement_ids":["R1"],"depends_on":[],"files":["相关文件"],"verification":"验收命令或步骤"}],
 "commands":{"bootstrap":["指定栈所需初始化命令；默认平台模板用空列表"],"build":["构建命令"],"test":["验证命令"],"dev":"启动命令"},
 "assumptions":["合理假设"]}
浏览器交互需求 verification 用 browser，后端数据需求用 api，CLI/源码构建用 command。
新 web 项目必须使用可信版本化平台模板或指定框架官方 CLI，在 frontend 下真实创建完整前端，提供根级 build/dev 启动入口和真实链路验证和 README。
可使用 scaffold_project 工具执行 Vite CLI 初始化，它不会实现产品；其余框架按计划自行命令行生成。
.atoms-workspace.json 支持 dev/build/test；dev 监听 $PORT，Vite 处理 $BASE_PATH。
额外后端服务用 services:[{"name":"api","command":"python -m uvicorn backend.app.main:app --host 127.0.0.1 --port $PORT","port_env":"API_PORT","ready_path":"/api/health"}]。
runtime 自动先启动 services，将 API_PORT 注入前端环境。Vite 使用 API_PORT 配置 /api 代理；API 客户端用 import.meta.env.BASE_URL + 'api/...'。
Vite 代理应以 const base=process.env.BASE_PATH||'/' 的 [base+'api'] 为 key，并 rewrite:path=>'/api'+path.slice((base+'api').length)。不能仅写 '/api' 匹配，否则会拦截整个预览 /api/runtime/... 前端路径导致 404。
平台预览 iframe 为不透明沙箱来源；预览网关支持 Authorization/CORS，但不转发主站 cookie。预览登录使用项目 Bearer token；不要依赖主站会话。
后端 Python 使用项目独立 .venv，不加载平台全局或用户 site-packages；依赖写入 backend/requirements.txt（或根 requirements.txt），run_build/runtime_check 自动安装到项目独立 .python-packages；手动 python -m pip install 也默认安装到该目录。禁止为绕过错误改装到 /tmp（noexec且不持久）或源码中的其他临时依赖目录。默认业务数据用 APP_DATABASE_URL/APP_DATABASE_SCHEMA 注入的托管 PostgreSQL；无需启用数据库 Docker，禁止使用平台管理账号。psycopg 使用 %s 参数和 dict_row。
新项目默认走轻量 demo-first：先写最小接口契约和前端预览，接口暂不可用时使用明确标注的本地 adapter，不等待外部凭据；预览确认后再接真实数据库/服务。后端任务不得阻塞前端首屏和核心交互，除非用户明确要求先完成后端或前端依赖真实数据才能渲染。
前端构建产物统一输出项目根 dist，便于现有静态资源发布。不声称静态发布等于后端部署。
"""

from deliverable_contract import INSTRUCTIONS as DELIVERABLE_INSTRUCTIONS, validate_deliverables
PLAN_INSTRUCTIONS += DELIVERABLE_INSTRUCTIONS

from verification_policy import INSTRUCTIONS as VERIFICATION_POLICY, command_issue
PLAN_INSTRUCTIONS += VERIFICATION_POLICY
from system_contract import INSTRUCTIONS as SYSTEM_CONTRACT_INSTRUCTIONS, validate_system_contract, contract_acceptance_issues
PLAN_INSTRUCTIONS += SYSTEM_CONTRACT_INSTRUCTIONS
PLAN_INSTRUCTIONS += '\n实际预览可能是局域网普通 HTTP 与沙箱来源。不要假定 crypto.randomUUID、剪贴板或其他安全上下文 API 可用；先检查能力并提供合适兼容路径。标识符可使用模板 src/lib/id.ts 的 createId（crypto.getRandomValues 在普通 HTTP 可用），不能为了兼容将认证/额度标识改为不安全的 Math.random。页面 JavaScript 启动检查是必做项，浏览器工具选项只控制完整交互验收。'

def json_object(content: str) -> dict:
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", content):
        try:
            value, _ = decoder.raw_decode(content[match.start():])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("模型未返回有效 JSON 对象")


def validate_plan(plan: dict) -> dict:
    if not isinstance(plan.get("goal"), str) or not plan["goal"].strip():
        raise ValueError("计划缺少目标")
    if plan.get("application_type") not in {"web", "service", "cli", "library", "artifact"}:
        raise ValueError("计划应用类型无效")
    if plan["application_type"] == "artifact":
        plan.setdefault("architecture", {"frontend":{"stack":"none","directory":"frontend"}, "backend":{"required":False,"reason":"交付文件成果，无需运行服务","stack":"none","directory":"backend"}})
        plan.setdefault("commands", {"bootstrap":[],"build":[],"test":[],"dev":""})
    architecture = plan.get("architecture") or {}
    backend = architecture.get("backend") or {}
    frontend = architecture.get("frontend") or {}
    for module in (frontend, backend):
        if not isinstance(module, dict) or not isinstance(module.get("stack"), str):
            raise ValueError("前后端必须明确技术栈（不需要时填 none）")
        directory = module.get("directory")
        validate_directory(directory)
    if not isinstance(backend.get("required"), bool) or not backend.get("reason"):
        raise ValueError("计划必须明确判断后端需求并给出理由")
    if not isinstance(plan.get("design"), str) or not plan["design"].strip():
        raise ValueError("计划缺少实现设计")
    commands = plan.get("commands")
    if not isinstance(commands, dict) or not isinstance(commands.get("dev"), str):
        raise ValueError("计划必须提供启动命令（非服务可为空）")
    for kind in ("bootstrap", "build", "test"):
        if not isinstance(commands.get(kind), list) or not all(isinstance(item, str) and item.strip() for item in commands[kind]):
            raise ValueError(f"计划必须提供 {kind} 命令列表")
        for command in commands[kind]:
            validate_command(command)
    validate_command(commands['dev'], empty=True)
    requirements = plan.get("requirements")
    tasks = plan.get("tasks")
    if not isinstance(requirements, list) or not 1 <= len(requirements) <= 80:
        raise ValueError("计划应包含 1–80 项需求")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 100:
        raise ValueError("计划应包含 1–100 个执行任务")
    ids = set()
    for requirement in requirements:
        identifier = requirement.get("id")
        if not isinstance(identifier, str) or identifier in ids or not requirement.get("description"):
            raise ValueError("需求 ID 重复或内容为空")
        ids.add(identifier)
        acceptance = requirement.get("acceptance")
        if not isinstance(acceptance, list) or not acceptance or not all(isinstance(item, str) and item.strip() for item in acceptance):
            raise ValueError(f"{identifier} 缺少可观察的验收步骤")
        if requirement.get("verification") not in {"browser", "api", "command", "artifact"}:
            raise ValueError(f"{identifier} 缺少明确的验证类型")
    seen, covered = set(), set()
    for task in tasks:
        identifier = task.get("id")
        if not isinstance(identifier, str) or identifier in seen or not task.get("title"):
            raise ValueError("任务 ID 重复或内容为空")
        if not isinstance(task.get("depends_on", []), list) or not set(task.get("depends_on", [])).issubset(seen):
            raise ValueError(f"{identifier} 依赖必须指向前序任务")
        seen.add(identifier)
        references = task.get("requirement_ids")
        if not isinstance(references, list) or not references or not set(references).issubset(ids):
            raise ValueError(f"{identifier} 必须关联已有需求")
        covered.update(references)
        if not task.get("files") or not task.get("verification"):
            raise ValueError(f"{identifier} 缺少文件规划或验证步骤")
        if not isinstance(task['files'], list):
            raise ValueError(f"{identifier} files 必须为具体相对路径列表")
        for path in task['files']:
            validate_directory(path)
    if covered != ids:
        raise ValueError(f"需求未被任务覆盖：{sorted(ids - covered)}")
    contracts = plan.get('interfaces', [])
    if not isinstance(contracts, list):
        raise ValueError('interfaces 必须为模块契约列表')
    for contract in contracts:
        if not isinstance(contract, dict) or not isinstance(contract.get('path'), str):
            raise ValueError('模块契约必须指定路径')
        validate_directory(contract['path'])
        if not isinstance(contract.get('exports'), list) or not all(isinstance(v, str) for v in contract['exports']):
            raise ValueError('模块契约 exports 必须是类型或函数签名列表')
        if not isinstance(contract.get('consumers', []), list) or not all(isinstance(v, str) for v in contract.get('consumers', [])):
            raise ValueError('模块契约 consumers 必须是路径列表')
        for path in contract.get('consumers', []):
            validate_directory(path)
    # A bootstrap-only task cannot validate gameplay that its dependent task
    # has not implemented yet. Keep setup inside the same product task, instead
    # of trapping the agent behind impossible acceptance dependencies.
    if plan['application_type'] == 'web' and not backend['required'] and 1 < len(tasks) <= 3:
        first, following = tasks[0], tasks[1]
        bootstrap_files = {'frontend', 'frontend/', 'package.json', 'package-lock.json',
                           'README.md', '.atoms-workspace.json', '.gitignore'}
        if set(first.get('files', [])).issubset(bootstrap_files) and first['id'] in following.get('depends_on', []):
            old_id, new_id = first['id'], following['id']
            following['files'] = list(dict.fromkeys(first['files'] + following['files']))
            following['requirement_ids'] = list(dict.fromkeys(first['requirement_ids'] + following['requirement_ids']))
            following['title'] = first['title'] + '；' + following['title']
            following['depends_on'] = [d for d in following.get('depends_on', []) if d != old_id]
            for later in tasks[2:]:
                later['depends_on'] = list(dict.fromkeys(new_id if d == old_id else d for d in later.get('depends_on', [])))
            plan['tasks'] = tasks[1:]
    return validate_deliverables(validate_system_contract(plan))


def source_digest(files: dict) -> str:
    source = {name: value for name, value in files.items()
              if not name.startswith(".atoms/") and not name.endswith("tsconfig.tsbuildinfo")}
    return hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def is_continuation(request: str):
    normalized = request.strip().rstrip('。.!！').lower()
    return normalized in {'继续', '继续构建', '继续完成', '继续执行', '继续上次任务',
                           '继续上次构建', '接着做', '继续吧', 'continue', 'resume'}


def resumable_plan(root: Path, request: str):
    path = root / '.atoms/task-state.json'
    if not path.exists():
        return None
    try:
        previous = json.loads(path.read_text())
        original = previous.get('request')
        continuation = is_continuation(request)
        if previous.get('completed') or not isinstance(original, str) or not (request == original or continuation):
            return None
        return original, validate_plan(previous['plan'])
    except (ValueError, KeyError, TypeError):
        return None


class TaskLedger:
    """Harness-owned state; model claims alone cannot turn requirements green."""

    def __init__(self, root: Path, plan: dict, request: str):
        self.root = root
        self.plan = validate_plan(plan)
        self.directory = root / ".atoms"
        self.directory.mkdir(exist_ok=True)
        self.tasks = [{**task, "status": "pending", "note": "", "evidence_ids": []} for task in plan["tasks"]]
        self.evidence: list[dict] = []
        self.request = request
        self.resumed = False
        self.completed = False
        frontend = (plan["architecture"].get("frontend") or {}).get("directory", "frontend")
        self.requires_bootstrap = (plan["application_type"] == "web" and
                                   not (root / "package.json").exists() and
                                   not (root / frontend / "package.json").exists())
        previous = self.directory / "task-state.json"
        if previous.exists():
            (self.directory / "LAST_RUN.json").write_text(previous.read_text())
            try:
                saved = json.loads(previous.read_text())
            except ValueError:
                saved = {}
            if not isinstance(saved, dict):
                saved = {}
            same_plan = isinstance(saved.get('plan'), dict) and (
                {k:v for k,v in saved['plan'].items() if k != 'enabled_tools'} ==
                {k:v for k,v in plan.items() if k != 'enabled_tools'})
            if saved.get('request') == request and same_plan and not saved.get('completed'):
                by_id = {item['id']: item for item in saved.get('tasks', []) if isinstance(item, dict) and 'id' in item}
                for task in self.tasks:
                    old = by_id.get(task['id'], {})
                    if old.get('status') in {'pending', 'in_progress', 'done', 'deferred'}:
                        task.update(status=old['status'], note=str(old.get('note', ''))[:2000], evidence_ids=old.get('evidence_ids', []))
                required = {'id', 'kind', 'command', 'exit_code', 'source_digest', 'requirement_ids'}
                self.evidence = [{**item, 'historical': True} for item in saved.get('evidence', []) if isinstance(item, dict) and required.issubset(item)]
                self.resumed = True
        self.persist()
        (self.directory / "PLAN.md").write_text(self.render_plan())

    def persist(self):
        path = self.directory / "task-state.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"request": self.request, "plan": self.plan,
                                        "tasks": self.tasks, "evidence": self.evidence,
                                        "completed": self.completed}, ensure_ascii=False, indent=2))
        temporary.replace(path)

    def acceptance_evidence(self, item):
        from system_contract import simulated_receipt
        if item['kind'] == 'http_request' and simulated_receipt(item):
            return False
        return (item['kind'] != 'local_unit_check' and not (
            item['kind'] in ('run_shell', 'run_build') and command_issue(item.get('command', ''), self.root)))

    def record(self, kind: str, command: str, code: int, output: str, digest: str,
               requirement_ids: list[str] | None = None, arguments: dict | None = None) -> str:
        if kind == "run_shell" and command_issue(command, self.root):
            kind = "local_unit_check"
        identifier = f"V{len(self.evidence) + 1}"
        item = {"id": identifier, "kind": kind, "command": command, "exit_code": code,
                "output": output[-5000:], "source_digest": digest, "requirement_ids": [] if kind == "local_unit_check" else requirement_ids or [],
                "time": time.time(), "arguments": arguments or {}}
        if kind == 'http_request':
            from system_contract import receipt_response
            response = receipt_response({'output': output})
            if response:
                # Keep machine evidence independent of the transcript tail limit.
                item['response'] = {**response, 'body': response.get('body', '')[:2500]}
        self.evidence.append(item)
        self.persist()
        return identifier

    def update(self, task_id: str, status: str, note: str, evidence_ids: list[str]):
        task = next((item for item in self.tasks if item["id"] == task_id), None)
        if not task or status not in {"pending", "in_progress", "done", "deferred"}:
            raise ValueError("任务或状态无效")
        if status == 'deferred' and not note.strip():
            raise ValueError('延期必须说明已实现/模拟内容、剩余缺口及后续配置或修复步骤')
        if status not in ('pending', 'deferred') and any(item["status"] not in ('done', 'deferred') for item in self.tasks if item["id"] in task.get("depends_on", [])):
            raise ValueError("请先完成依赖任务")
        if status == "done":
            passed = {item["id"] for item in self.evidence if item["exit_code"] == 0 and self.acceptance_evidence(item)}
            if not evidence_ids or not set(evidence_ids).issubset(passed):
                raise ValueError("完成任务必须关联实际执行成功的验证 ID（V1 等），不能只写已完成")
        if status == 'done' and self.plan.get('system_contract'):
            from system_contract import success_receipt
            from agent import snapshot_files
            current = source_digest(snapshot_files(self.root))
            relevant = [e for e in self.evidence if e['id'] in evidence_ids and e['source_digest'] == current and success_receipt(e)]
            if not all(any(rid in e['requirement_ids'] for e in relevant) for rid in task['requirement_ids']):
                raise ValueError('系统任务完成必须关联当前源码的真实成功链路；构建、健康和预期错误路径不能替代')
        task.update(status=status, note=note[:2000], evidence_ids=evidence_ids)
        self.persist()
        return json.dumps({'updated_task': task, 'remaining': sum(t['status'] != 'done' for t in self.tasks),
                           'remaining_active': sum(t['status'] not in ('done', 'deferred') for t in self.tasks),
                           'deferred': sum(t['status'] == 'deferred' for t in self.tasks)}, ensure_ascii=False)

    def model_context(self, files: dict | None = None) -> str:
        """Small execution view; full requirements and original plan stay durable."""
        current_digest = source_digest(files) if files is not None else None
        # Keep recent faults, but never evict successful current requirement
        # receipts merely because later setup/selector attempts failed.
        selected = {e['id']: e for e in self.evidence[-12:]}
        coverage = {}
        for evidence in self.evidence:
            if evidence['exit_code'] == 0 and self.acceptance_evidence(evidence) and not evidence.get('historical') and (
                    current_digest is None or evidence['source_digest'] == current_digest):
                for requirement in evidence['requirement_ids']:
                    coverage[(evidence['kind'], requirement)] = evidence
        selected.update({e['id']: e for e in coverage.values()})
        return json.dumps({'tasks': [{k: t.get(k) for k in ('id', 'title', 'status', 'depends_on', 'files', 'evidence_ids')}
                                     for t in self.tasks],
                           'active': [t for t in self.tasks if t['status'] == 'in_progress'],
                           'evidence': [{**{k: e.get(k) for k in ('id', 'kind', 'exit_code', 'requirement_ids', 'historical')},
                                         'current_source': current_digest is not None and e['source_digest'] == current_digest}
                                        for e in selected.values()],
                           **({'completion_issues': self.completion_issues(files)} if files is not None else {})}, ensure_ascii=False, separators=(',', ':'))

    def context(self) -> str:
        return json.dumps({"goal": self.plan["goal"], "architecture": self.plan["architecture"],
                           "system_contract": self.plan.get('system_contract'),
                           "task_type": self.plan.get("task_type"), "deliverables": self.plan.get("deliverables", []),
                           "requirements": self.plan["requirements"], "tasks": self.tasks,
                           "recent_evidence": [{key: value for key, value in item.items() if key != "output"}
                                               for item in self.evidence[-12:]]}, ensure_ascii=False)

    def next_task(self) -> dict | None:
        active = next((item for item in self.tasks if item["status"] == "in_progress"), None)
        if active:
            return active
        done = {item["id"] for item in self.tasks if item["status"] in ('done', 'deferred')}
        task = next((item for item in self.tasks if item["status"] == "pending" and set(item.get("depends_on", [])).issubset(done)), None)
        if task:
            task["status"] = "in_progress"
            self.persist()
        return task

    def render_plan(self) -> str:
        lines = [f"# {self.plan['goal']}", "", "## Architecture", json.dumps(self.plan["architecture"], ensure_ascii=False, indent=2),
                 "", "## Design", self.plan["design"], "", "## Acceptance"]
        for item in self.plan["requirements"]:
            lines.extend([f"### {item['id']} {item['description']}", *[f"- {step}" for step in item["acceptance"]]])
        lines += ["", "## Implementation tasks", *[f"- {item['id']}: {item['title']} ({', '.join(item['requirement_ids'])})" for item in self.tasks]]
        return "\n".join(lines) + "\n"

    def completion_issues(self, files: dict) -> list[str]:
        issues = [f"任务尚未完成：{item['id']} {item['title']}" for item in self.tasks if item["status"] != "done"]
        digest = source_digest(files)
        current = [item for item in self.evidence if not item.get('historical') and item["exit_code"] == 0 and self.acceptance_evidence(item) and item["source_digest"] == digest]
        if self.requires_bootstrap and not any(item["exit_code"] == 0 and (
                item["kind"] in {"scaffold_project", "install_default_template"} or (item["kind"] == "run_shell" and
                re.search(r"(?:create[- ](?:vite|next-app|react-router)|npm (?:init|create)|pnpm create|npx .*create)", item["command"])))
                for item in self.evidence):
            issues.append("新前端缺少真实框架 CLI 或可信平台模板初始化记录；请使用平台模板、scaffold_project 或官方 CLI")
        build_tier = self.plan.get('build_tier', 'normal')
        for requirement in self.plan["requirements"]:
            # Normal web builds are demo-first. API contracts are still kept in
            # the plan and implemented in source, but an unavailable provider or
            # an unneeded per-endpoint probe must not block the visual preview.
            if (build_tier == 'normal' and self.plan.get('application_type') == 'web'
                    and requirement.get('verification') == 'api'):
                continue
            expected_kind = {"browser": "browser_check", "api": "http_request", "command": "run_shell", "artifact": "artifact_check"}[requirement["verification"]]
            if expected_kind == 'browser_check' and not browser_enabled(self.plan):
                expected_kind = 'run_shell'
            kinds = {expected_kind, "run_build"} if expected_kind == "run_shell" else {expected_kind}
            if not any(requirement["id"] in item["requirement_ids"] and item["kind"] in kinds for item in current):
                method = 'command（浏览器验收未启用）' if requirement['verification'] == 'browser' and not browser_enabled(self.plan) else requirement['verification']
                issues.append(f"{requirement['id']} 缺少当前源码的 {method} 验收证据，验证工具需传 requirement_ids")
        contract_issues = contract_acceptance_issues(self.plan, self.evidence, digest)
        if not (build_tier == 'normal' and self.plan.get('application_type') == 'web'):
            issues.extend(contract_issues)
        architecture = self.plan["architecture"]
        issues.extend(conformance_issues(self.root, self.plan, files))
        for contract in self.plan.get('interfaces', []):
            if contract['path'] not in files:
                issues.append('计划中的模块接口尚未落到实际文件：' + contract['path'])
        if self.plan["application_type"] == "web" and browser_enabled(self.plan) and not any(item["kind"] == "browser_check" for item in current):
            issues.append("Web 项目缺少当前源码的真实浏览器验收")
        backend = architecture["backend"]
        if backend["required"]:
            backend_prefix = (backend.get("directory") or "backend").strip("/") + "/"
            if not any(name.startswith(backend_prefix) and name.endswith((".py", ".ts", ".js", ".go", ".rs")) for name in files):
                issues.append("架构需要真实后端，但后端实现文件缺失")
            if not (build_tier == 'normal' and self.plan.get('application_type') == 'web') and not any(item["kind"] == "http_request" for item in current):
                issues.append("后端没有当前源码的真实 HTTP 验证证据")
        if self.plan["application_type"] != "artifact" and not (self.root / "README.md").exists():
            issues.append("缺少 README：应说明安装、启动、架构、数据持久化、测试和部署限制")
        return issues


def compact_messages(messages: list[dict], prefix_length: int, ledger: TaskLedger,
                     journal: list[dict], max_chars: int = 70000) -> list[dict]:
    """Trim complete conversation turns, retaining durable tasks and command outcomes."""
    if sum(len(json.dumps(item, ensure_ascii=False)) for item in messages) <= max_chars:
        return messages
    # An assistant tool call and all matching tool results must stay together.
    starts = [index for index in range(prefix_length, len(messages)) if messages[index]["role"] == "assistant"]
    if len(starts) < 3:
        return messages
    cut = starts[-3]
    summary = {"role": "user", "content": "Earlier execution was compacted. Current files are authoritative. Preserve all requirements.\n"
               + ledger.context() + "\nRecent actions:\n" + json.dumps(journal[-24:], ensure_ascii=False)}
    (ledger.directory / "checkpoint.json").write_text(json.dumps({"tasks": ledger.tasks, "recent_actions": journal[-24:]}, ensure_ascii=False, indent=2))
    return messages[:prefix_length] + [summary] + messages[cut:]
