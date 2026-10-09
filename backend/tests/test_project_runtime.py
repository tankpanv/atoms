"""Lifecycle regression tests, plus opt-in real CLI/fullstack/browser integration."""

import asyncio
import json
import os
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
import httpx

import runtime
from agent import run_build, run_command, scaffold_project, snapshot_files
from agent_checks import browser_check, http_request


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_readiness_failure_includes_actual_http_error(self):
        process = SimpleNamespace(returncode=None)
        def handler(request):
            process.returncode = 1
            return httpx.Response(404, json={'detail': 'Not Found'})
        original_client = httpx.AsyncClient
        def client(**kwargs):
            return original_client(transport=httpx.MockTransport(handler), **kwargs)
        with patch('runtime.httpx.AsyncClient', side_effect=client):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 404.*Not Found'):
                await runtime._wait_ready(process, 12345, '/api/runtime/example/', lambda: 'Vite ready')

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / uuid.uuid4().hex
        self.root.mkdir()
        (self.root / '.atoms').mkdir()
        self.project_id = uuid.UUID(hex=self.root.name)
        self.patches = [patch('runtime.ensure_workspace', return_value=self.root), patch('agent.ensure_workspace', return_value=self.root),
                        patch('runtime.project_uid', return_value=os.getuid()), patch('agent.project_uid', return_value=os.getuid())]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        await runtime.stop_runtime(self.project_id)
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def configure_servers(self):
        server = "python -m http.server $PORT --bind 127.0.0.1"
        (self.root / 'index.html').write_text('runtime fixture')
        (self.root / '.atoms-workspace.json').write_text(json.dumps({"dev": server, "services": [
            {"name": "api", "command": server, "port_env": "API_PORT", "ready_path": "/"}]}))

    def declare_api_dependencies(self):
        # Project Python no longer borrows the platform's FastAPI installation.
        # The built worker image restores this exact manifest without PyPI.
        source = Path(__file__).parents[1] / 'templates/api-v1/backend/requirements.txt'
        (self.root / 'requirements.txt').write_bytes(source.read_bytes())

    async def test_dependencies_start_first_and_stop_with_main(self):
        self.configure_servers()
        started = await runtime.start_runtime(self.project_id)
        self.assertEqual(len(started.services), 1)
        self.assertNotEqual(started.port, started.services[0].port)
        reused = await runtime.start_runtime(self.project_id)
        self.assertIs(started, reused)
        code, output = await http_request(self.project_id, {"service": "api", "path": "/", "expect_status": 200})
        self.assertEqual(code, 0, output)
        await runtime.stop_runtime(self.project_id)
        self.assertIsNotNone(started.process.returncode)
        self.assertIsNotNone(started.services[0].process.returncode)
        self.assertNotIn(started.port, runtime._reserved_ports)
        self.assertNotIn(started.services[0].port, runtime._reserved_ports)

    async def test_failed_dependency_is_cleaned_up(self):
        config = {"dev": "python -m http.server $PORT", "services": [{"name": "api", "command": "exit 7", "port_env": "API_PORT"}]}
        (self.root / '.atoms-workspace.json').write_text(json.dumps(config))
        before = set(runtime._reserved_ports)
        with self.assertRaises(RuntimeError):
            await runtime.start_runtime(self.project_id)
        self.assertEqual(runtime._reserved_ports, before)
        self.assertIsNone(runtime.runtime_status(self.project_id))

    async def test_changed_source_restarts_but_repeated_checks_reuse_process(self):
        self.configure_servers()
        first = await runtime.start_runtime(self.project_id)
        for _ in range(5):
            self.assertIs(await runtime.start_runtime(self.project_id), first)
        (self.root/'index.html').write_text('updated implementation')
        second = await runtime.start_runtime(self.project_id)
        self.assertIsNot(first, second)
        self.assertIsNotNone(first.process.returncode)
        self.assertIs(await runtime.start_runtime(self.project_id), second)

    def test_configuration_rejects_duplicate_ports_and_unsafe_variables(self):
        for variable in ('HOME', 'PORT', 'API_PORT'):
            config = {"services": [{"name": "one", "command": "echo ok", "port_env": "API_PORT"},
                                   {"name": "two", "command": "echo ok", "port_env": variable}]}
            (self.root / '.atoms-workspace.json').write_text(json.dumps(config))
            with self.subTest(variable=variable), self.assertRaises(ValueError):
                runtime.configured_services(self.root)

    async def test_real_form_and_file_requests_follow_actual_encoding_contract(self):
        self.declare_api_dependencies()
        (self.root / 'server.py').write_text('''from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from urllib.parse import parse_qs
from email.parser import BytesParser
from email.policy import default
app = FastAPI()
@app.get('/')
@app.get('/health')
def health(): return {'ok': True}
@app.post('/form')
async def form(request: Request):
    content_type = request.headers.get('content-type', '')
    fields = parse_qs((await request.body()).decode()) if content_type.startswith('application/x-www-form-urlencoded') else {}
    if 'occasion' not in fields: return JSONResponse({'error': 'occasion missing'}, status_code=422)
    return {'occasion': fields['occasion'][0]}
@app.post('/upload')
async def upload(request: Request):
    message = BytesParser(policy=default).parsebytes(('Content-Type: ' + request.headers['content-type'] + '\\r\\nMIME-Version: 1.0\\r\\n\\r\\n').encode() + await request.body())
    parts = {part.get_param('name', header='content-disposition'): part for part in message.iter_parts()}
    return {'occasion': parts['occasion'].get_payload(decode=True).decode(), 'filename': parts['image'].get_filename(), 'content': parts['image'].get_payload(decode=True).decode()}
''')
        (self.root / 'actual-upload.txt').write_text('actual project file')
        (self.root / '.atoms-workspace.json').write_text(json.dumps({
            'dev': 'python -m uvicorn server:app --host 127.0.0.1 --port $PORT'}))
        from execution_guard import ExecutionGuard
        guard = ExecutionGuard({})
        wrong = {'path': '/form', 'method': 'POST', 'body': {'occasion': 'daily'}, 'expect_status': 422}
        code, report = await http_request(self.project_id, wrong)
        self.assertEqual(code, 0, report)
        guard.validation_result('http_request', code, report)
        self.assertEqual(guard.limits['failures'], {})  # Expected 422 is a passing error-path check.
        for payload in ({'body': 'occasion=daily', 'headers': {'Content-Type': 'application/x-www-form-urlencoded'}},
                        {'body': {'occasion': 'daily'}, 'headers': {'Content-Type': 'application/x-www-form-urlencoded'}},
                        {'form': {'occasion': 'daily'}}):
            code, report = await http_request(self.project_id, {
                'path': '/form', 'method': 'POST', 'expect_status': 200,
                'expect_json': {'occasion': 'daily'}, **payload})
            self.assertEqual(code, 0, report)
        code, report = await http_request(self.project_id, {
            'path': '/upload', 'method': 'POST', 'expect_status': 200,
            'form': {'occasion': 'daily'}, 'files': [{'field': 'image', 'path': 'actual-upload.txt'}],
            'expect_json': {'occasion': 'daily', 'filename': 'actual-upload.txt', 'content': 'actual project file'}})
        self.assertEqual(code, 0, report)
        await runtime.stop_runtime(self.project_id)
        code, report = await http_request(self.project_id, {'path': '/health', 'expect_status': 200, 'expect_json': {'ok': True}})
        self.assertEqual(code, 0, report)

    async def test_real_business_payload_contract_and_configuration_blocker(self):
        self.declare_api_dependencies()
        from system_contract import contract_acceptance_issues, confirmed_dependency_blocker
        from test_system_contract import system_plan
        # Real uvicorn/socket requests: empty 200 is not business success.
        server = '''from fastapi import FastAPI
from fastapi.responses import JSONResponse
app = FastAPI()
@app.get('/')
def health(): return {'ok': True}
@app.post('/generate')
def generate(): return RESULT
@app.get('/provider')
def provider(): return JSONResponse({'detail':'AI 服务未配置，请设置 OPENAI_API_KEY'}, status_code=503)
'''
        (self.root / 'server.py').write_text(server.replace('RESULT', "{'outfits': []}"))
        (self.root / '.atoms-workspace.json').write_text(json.dumps({'dev': 'python -m uvicorn server:app --host 127.0.0.1 --port $PORT'}))
        plan = system_plan()
        args = {'path': '/generate', 'method': 'POST', 'expect_status': 200,
                'expect_schema': plan['system_contract']['api_contracts'][0]['success_schema']}
        code, report = await http_request(self.project_id, args)
        self.assertNotEqual(code, 0, report)
        self.assertIn('minItems', report)
        code, report = await http_request(self.project_id, {'path': '/provider', 'expect_status': 503})
        self.assertEqual(code, 0, report)
        evidence = [{'id': 'V1', 'kind': 'http_request', 'exit_code': code, 'output': report,
                     'source_digest': 'current', 'arguments': {'path': '/provider'}}]
        self.assertEqual(confirmed_dependency_blocker(evidence, 'current')['required_env'], ['OPENAI_API_KEY'])
        (self.root / 'server.py').write_text(server.replace('RESULT', "{'outfits': [{'image':'/actual-output'}]*3}"))
        await runtime.stop_runtime(self.project_id)
        code, report = await http_request(self.project_id, args)
        self.assertEqual(code, 0, report)
        evidence = [{'id': 'V2', 'kind': 'http_request', 'exit_code': code, 'output': report,
                     'source_digest': 'current', 'arguments': args, 'requirement_ids': ['R1']}]
        self.assertEqual(contract_acceptance_issues(plan, evidence, 'current'), [])

    async def test_disabled_interaction_tool_still_rejects_real_http_javascript_crash(self):
        from agent import verify_delivery_runtime
        from test_agent_harness import example_plan
        plan = {**example_plan(), 'enabled_tools': []}
        (self.root / '.atoms-workspace.json').write_text(json.dumps({'dev': 'python -m http.server $PORT --bind 127.0.0.1', 'build': 'echo build'}))
        (self.root / 'index.html').write_text('<html><body><div id="root"></div><script src="app.js"></script></body></html>')
        script = self.root / 'app.js'
        script.write_text("document.getElementById('root').textContent = crypto.randomUUID()")
        code, report = await http_request(self.project_id, {'path': '/', 'expect_status': 200})
        self.assertEqual(code, 0, report)  # HTML 200 alone misses the crash.
        code, report = await verify_delivery_runtime(self.project_id, self.root, plan)
        self.assertNotEqual(code, 0, report)
        self.assertIn('randomUUID', report)
        helper = (Path(__file__).parents[1] / 'templates/web-v1/frontend/src/lib/id.ts').read_text().replace('export function createId(): string', 'function createId()')
        script.write_text(helper + "\ndocument.getElementById('root').textContent = createId()")
        code, report = await verify_delivery_runtime(self.project_id, self.root, plan)
        self.assertEqual(code, 0, report)
        self.assertIn('"secure_context": false', report)
        self.assertIn('"random_uuid": "undefined"', report)

    async def test_deferred_demo_startup_allows_missing_business_acceptance_but_not_placeholder(self):
        from agent import verify_delivery_runtime
        from test_agent_harness import example_plan
        plan = {**example_plan(), 'enabled_tools': []}
        plan['architecture']['backend']['required'] = True
        (self.root / '.atoms-workspace.json').write_text(json.dumps({'dev': 'python -m http.server $PORT --bind 127.0.0.1', 'build': 'echo build'}))
        html = self.root / 'index.html'
        html.write_text('<html><head><meta charset="UTF-8"></head><body data-atoms-starter="true">在这里实现用户需求。 应用开发起点</body></html>')
        code, report = await verify_delivery_runtime(self.project_id, self.root, plan, require_scenario=True, preview_only=True)
        self.assertNotEqual(code, 0, report)
        self.assertIn('模板占位页', report)
        html.write_text('<html><head><meta charset="UTF-8"></head><body><h1>穿搭工具演示</h1><p>模拟方案；真实 AI 配置待补齐。</p></body></html>')
        code, report = await verify_delivery_runtime(self.project_id, self.root, plan, require_scenario=True, preview_only=True)
        self.assertEqual(code, 0, report)  # Missing business GET evidence is a TODO, not demo stop.
        self.assertIn('真实页面', report)

    @unittest.skipUnless(os.getenv('RUN_CLI_SMOKE') == '1', 'requires real npm downloads and Chromium')
    async def test_real_cli_fullstack_crud_reload_and_restart(self):
        self.declare_api_dependencies()
        # Exercise the actual project Unix identity, not root, in the Docker smoke run.
        if os.geteuid() == 0:
            os.chmod(self.temporary.name, 0o711)
            os.chmod(self.root, 0o700)
            self.patches[2].stop()
            self.patches[3].stop()
        code, output = await scaffold_project(self.project_id)
        self.assertEqual(code, 0, output)
        self.assertTrue((self.root / 'frontend/tsconfig.app.json').exists())
        self.assertTrue((self.root / 'frontend/src/main.tsx').exists())
        (self.root / 'backend/app').mkdir(parents=True)
        (self.root / 'backend/app/main.py').write_text('''import os, sqlite3
from pathlib import Path
from fastapi import FastAPI
from pydantic import BaseModel
data = Path(os.environ['APP_DATA_DIR']); data.mkdir(exist_ok=True)
def db():
    conn = sqlite3.connect(data/'calendar.db')
    conn.execute('CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, title TEXT NOT NULL)')
    return conn
app = FastAPI()
class Event(BaseModel):
    title: str
@app.get('/api/health')
def health(): return {'ok': True}
@app.get('/api/events')
def events():
    with db() as conn: return [{'id': row[0], 'title': row[1]} for row in conn.execute('SELECT id,title FROM events')]
@app.post('/api/events', status_code=201)
def create(event: Event):
    with db() as conn:
        cursor = conn.execute('INSERT INTO events(title) VALUES (?)', (event.title,))
        return {'id': cursor.lastrowid, 'title': event.title}
''')
        (self.root / 'frontend/src/api').mkdir()
        (self.root / 'frontend/src/pages').mkdir()
        (self.root / 'frontend/src/api/events.ts').write_text('''export type CalendarEvent = {id:number;title:string};
const endpoint = import.meta.env.BASE_URL + 'api/events';
export async function listEvents():Promise<CalendarEvent[]> { return (await fetch(endpoint)).json() }
export async function createEvent(title:string) { await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title})}) }
''')
        (self.root / 'frontend/src/pages/Calendar.tsx').write_text('''import {useEffect,useState} from 'react';
import {listEvents,createEvent,type CalendarEvent} from '../api/events';
export default function Calendar(){
const [events,setEvents]=useState<CalendarEvent[]>([]);const [title,setTitle]=useState('');
useEffect(()=>{void listEvents().then(setEvents)},[]);
return <main><h1>Calendar</h1><input aria-label="Event title" value={title} onChange={e=>setTitle(e.target.value)}/><button onClick={async()=>{await createEvent(title);setTitle('');setEvents(await listEvents())}}>Add event</button><ul>{events.map(event=><li className="event" key={event.id}>{event.title}</li>)}</ul></main>
}
''')
        (self.root / 'frontend/src/App.tsx').write_text("import Calendar from './pages/Calendar'; export default function App(){return <Calendar/>}")
        (self.root / 'frontend/vite.config.ts').write_text('''import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
const base=process.env.BASE_PATH||'/';
export default defineConfig({plugins:[react()],server:{proxy:{[base+'api']:{target:'http://127.0.0.1:'+process.env.API_PORT,rewrite:path=>'/api'+path.slice((base+'api').length)}}}})
''')
        config = json.loads((self.root / '.atoms-workspace.json').read_text())
        config['services'] = [{"name": "api", "command": "python -m uvicorn backend.app.main:app --host 127.0.0.1 --port $PORT", "port_env": "API_PORT", "ready_path": "/api/health"}]
        (self.root / '.atoms-workspace.json').write_text(json.dumps(config))
        code, output = await run_build(self.project_id)
        self.assertEqual(code, 0, output)
        self.assertTrue((self.root / 'dist/index.html').exists())
        code, output = await http_request(self.project_id, {"service": "api", "path": "/api/health", "expect_status": 200, "expect_json": {"ok": True}})
        self.assertEqual(code, 0, output)
        actions = [{"action": "fill", "selector": "input", "value": "Persistent meeting"},
                   {"action": "click", "selector": "button"},
                   {"action": "assert_text", "selector": ".event", "value": "Persistent meeting"},
                   {"action": "reload"},
                   {"action": "assert_text", "selector": ".event", "value": "Persistent meeting"}]
        code, output = await browser_check(self.project_id, self.root, {"actions": actions})
        self.assertEqual(code, 0, output)
        await runtime.stop_runtime(self.project_id)
        code, output = await http_request(self.project_id, {"service": "api", "path": "/api/events", "expect_status": 200, "expect_json": [{"title": "Persistent meeting"}]})
        self.assertEqual(code, 0, output)
        self.assertFalse(any(name.startswith('.atoms-data/') or name.startswith('.atoms/screenshots/') for name in snapshot_files(self.root)))
        # Exercise the actual final-delivery gate: it restarts both services
        # and reads the previously created record through the real browser/API.
        from agent import verify_delivery
        config['demo'] = {'path': '/', 'actions': [
            {'action': 'assert_text', 'selector': '.event', 'value': 'Persistent meeting'}]}
        (self.root / '.atoms-workspace.json').write_text(json.dumps(config))
        code, output = await verify_delivery(self.project_id, self.root,
            {'application_type': 'web', 'architecture': {'backend': {'required': True}}},
            require_scenario=True)
        self.assertEqual(code, 0, output)
        self.assertIn('Persistent meeting', output)


if __name__ == '__main__':
    unittest.main()
