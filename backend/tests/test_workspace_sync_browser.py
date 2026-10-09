"""WORKSPACE_TEST_URL should point at frontend Vite /tests/workspace.html."""
import copy
import asyncio
import os
import re
import unittest
from urllib.parse import urlsplit
from playwright.async_api import async_playwright, expect
from preview_diagnostics import inject_preview_diagnostics


@unittest.skipUnless(os.getenv('WORKSPACE_TEST_URL'), 'requires frontend fixture Vite server')
class WorkspaceBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.project = {'id': 'fixture', 'title': '工作区回归测试', 'status': 'running', 'preview_html': '',
                        'published': False, 'model': 'fixture', 'messages': []}
        self.jobs = [{'id': 'job1', 'prompt': 'fixture', 'status': 'running', 'error': '', 'stop_requested': False,
                      'created_at': '2026-10-09T08:00:00Z', 'updated_at': '2026-10-09T08:00:00Z', 'steps': []}]
        self.operation = None
        self.session_fails = False
        self.runtime_starts = 0
        self.preview_html = '<h1>可用的版本预览</h1>'
        self.module_release = None
        self.project_response_delay = 0
        self.jobs_response_delay = 0
        self.restore_response_gate = None
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True, args=['--no-sandbox', '--no-proxy-server'])
        self.page = await self.browser.new_page(viewport={'width': 1440, 'height': 1000})
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.on('dialog', lambda dialog: dialog.accept())
        await self.page.route('**/api/**', self.api)
        await self.page.goto(os.environ['WORKSPACE_TEST_URL'])
        await expect(self.page.get_by_test_id('project-status')).to_have_text('running')
        await expect(self.page.locator('.build-page')).to_be_visible()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()
        self.assertEqual(self.errors, [])

    async def api(self, route):
        request = route.request
        path = urlsplit(request.url).path
        body = {}
        if path == '/api/auth/refresh':
            body = {'access_token': 'test', 'expires_in': 86400, 'user': {'id': 'owner', 'email': 'fixture@example.test'}}
        elif path.endswith('/events'):
            await route.abort()  # Polling must deliver final state without SSE.
            return
        elif path == '/api/projects/fixture':
            body = self.project
            await asyncio.sleep(self.project_response_delay)
        elif path.endswith('/jobs'):
            body = self.jobs
            await asyncio.sleep(self.jobs_response_delay)
        elif path.endswith('/session'):
            if self.session_fails:
                await route.fulfill(status=503, json={'detail': 'checkpoint temporarily locked'})
                return
            body = {'available': True, 'application_type': 'web', 'completed': self.project['status'] == 'ready'}
        elif path.endswith('/restore/1'):
            self.project['status'] = 'restoring'
            self.operation = {'id': 'restore1', 'version': 1, 'status': 'running', 'phase': '正在恢复依赖并重新构建', 'error': '', 'result': {}}
            body = {'restore_id': self.operation['id']}
            if self.restore_response_gate:
                self.project['messages'].extend([
                    {'id':'restore-request','role':'user','content':'从版本 v1 恢复','created_at':'2026-10-09T08:02:00Z','restoration':{'kind':'restore','version':1,'status':'running','phase':'正在准备运行环境','steps':[]}},
                    {'id':'restore-message','role':'assistant','agent':'Mike','content':'正在还原版本 1，需要些时间，请等待…','created_at':'2026-10-09T08:02:01Z','restoration':{'kind':'restore','version':1,'status':'running','phase':'正在重建运行环境','steps':[]}},
                ])
                await self.restore_response_gate.wait()
        elif path.endswith('/restore'):
            body = {'restore': self.operation}
        elif path.endswith('/versions'):
            body = [{'version': 1, 'summary': '旧版', 'created_at': '2026-10-09T08:00:00Z'}]
        elif path.endswith('/files'):
            body = {'files': []}
        elif path.endswith('/runtime'):
            if request.method == 'POST':
                self.runtime_starts += 1
            body = {'mode': 'live', 'url': '/api/runtime/current/', 'output': '',
                    'status': {'running': True, 'url': '/api/runtime/current/', 'output': ''}}
        elif path.startswith('/api/runtime/'):
            if path.endswith('/app.js'):
                await self.module_release.wait()
                await route.fulfill(content_type='application/javascript', body="document.getElementById('root').innerHTML='<h1>加载完成</h1>'")
                return
            await route.fulfill(status=200, content_type='text/html; charset=utf-8', body=inject_preview_diagnostics(self.preview_html))
            return
        await route.fulfill(status=200, json=copy.deepcopy(body))

    def complete_generation(self):
        self.jobs[-1]['status'] = 'done'
        self.jobs[-1]['steps'] = [{'id': 1, 'kind': 'version', 'label': '版本 1 可演示', 'detail': '', 'created_at': '2026-10-09T08:01:00Z'}]
        self.project['status'] = 'ready'
        self.project['preview_html'] = '<h1>Result</h1>'
        self.project['messages'] = [{'id': 'message1', 'role': 'assistant', 'agent': 'Alex', 'content': '生成已经完成', 'created_at': '2026-10-09T08:01:00Z'}]

    async def reconcile(self):
        await self.page.evaluate("window.dispatchEvent(new Event('focus'))")

    async def test_completion_without_stream_or_session_updates_original_page(self):
        self.session_fails = True
        self.complete_generation()
        await expect(self.page.get_by_test_id('project-status')).to_have_text('ready', timeout=10000)
        await expect(self.page.get_by_text('生成已经完成', exact=True)).to_be_visible()
        await expect(self.page.locator('.build-viewer-body iframe')).to_be_visible()

    async def test_restore_progress_survives_reload_then_uses_verified_preview(self):
        self.complete_generation()
        await self.reconcile()
        await expect(self.page.get_by_test_id('project-status')).to_have_text('ready')
        await self.page.get_by_title('历史记录', exact=True).first.click()
        await self.page.get_by_role('button', name='还原', exact=True).click()
        await expect(self.page.locator('.build-preview-empty[role="status"]')).to_contain_text('还原需要些时间，请等待')
        await self.page.reload()
        await expect(self.page.locator('.build-preview-empty[role="status"]')).to_contain_text('正在恢复依赖并重新构建', timeout=10000)
        self.project['status'] = 'ready'
        self.operation['status'] = 'succeeded'
        self.operation['result'] = {'restored_version': 2, 'restored_preview': {'url': '/api/runtime/restored/', 'mode': 'live', 'output': ''}}
        await self.reconcile()
        await expect(self.page.locator('.build-viewer-body iframe')).to_have_attribute('src', re.compile('/api/runtime/restored/'), timeout=10000)
        await expect(self.page.get_by_text('已还原版本 1，预览验证通过', exact=True)).to_be_visible()
        await expect(self.page.locator('.build-preview-error')).to_have_count(0)
        starts = self.runtime_starts
        self.jobs.append({**self.jobs[-1], 'id': 'job2'})
        await self.reconcile()
        await expect(self.page.locator('.build-viewer-body iframe')).to_have_attribute('src', re.compile('/api/runtime/current/'), timeout=10000)
        self.assertGreater(self.runtime_starts, starts)

    async def test_failed_restore_keeps_visible_error_and_never_claims_success(self):
        self.complete_generation()
        await self.reconcile()
        await expect(self.page.get_by_test_id('project-status')).to_have_text('ready')
        await self.page.get_by_title('历史记录', exact=True).first.click()
        await self.page.get_by_role('button', name='还原', exact=True).click()
        await expect(self.page.locator('.build-preview-empty[role="status"]')).to_be_visible()
        self.project['status'] = 'error'
        self.operation['status'] = 'failed'
        self.operation['error'] = '还原后构建失败；已恢复操作前的工作区'
        await self.reconcile()
        await expect(self.page.locator('.build-preview-error')).to_contain_text('已恢复操作前的工作区', timeout=10000)
        await expect(self.page.get_by_text('已还原版本 1，预览验证通过', exact=True)).to_have_count(0)
        await expect(self.page.locator('.build-preview-empty[role="status"]')).to_have_count(0)

    async def test_continued_generation_with_cold_modules_waits_then_clears_loading(self):
        self.complete_generation()
        await self.reconcile()
        await expect(self.page.frame_locator('.build-viewer-body iframe').get_by_text('可用的版本预览')).to_be_visible()
        await expect(self.page.locator('.build-preview-loading')).to_have_count(0)
        starts = self.runtime_starts
        self.project['status'] = 'running'
        self.jobs.append({**self.jobs[-1], 'id': 'job2', 'status': 'running'})
        await self.reconcile()
        await expect(self.page.get_by_test_id('project-status')).to_have_text('running')
        self.preview_html = '<head><script type="module" src="app.js"></script></head><body><div id="root"></div></body>'
        self.module_release = asyncio.Event()
        self.complete_generation()
        # Deliberately separate jobs/project responses to expose duplicate starts.
        self.jobs_response_delay = .7
        await self.reconcile()
        await expect(self.page.locator('.build-preview-loading')).to_be_visible(timeout=10000)
        try:
            await self.page.wait_for_timeout(5000)
            await expect(self.page.locator('.build-preview-error')).to_have_count(0)
            await expect(self.page.locator('.build-preview-loading')).to_be_visible()
            self.assertEqual(self.runtime_starts, starts + 1)
        finally:
            self.module_release.set()
        await expect(self.page.frame_locator('.build-viewer-body iframe').get_by_text('加载完成')).to_be_visible()
        await expect(self.page.locator('.build-preview-loading')).to_have_count(0)
        await expect(self.page.locator('.build-preview-error')).to_have_count(0)

    async def preview_message(self, payload):
        frame = self.page.frames[-1]
        await frame.evaluate("""payload => parent.postMessage({
            frameId:new URLSearchParams(location.search).get('__atoms_frame'),
            documentId:'test-document',documentStartedAt:performance.timeOrigin+1,...payload}, '*')""", payload)

    async def test_recovery_clears_timeouts_but_retains_runtime_errors_and_ignores_stale_messages(self):
        self.complete_generation()
        await self.reconcile()
        await expect(self.page.frame_locator('.build-viewer-body iframe').get_by_text('可用的版本预览')).to_be_visible()
        await expect(self.page.locator('.build-preview-loading')).to_have_count(0)
        await self.preview_message({'type': 'atoms-preview-error', 'kind': 'mount_timeout', 'message': '加载超时'})
        await expect(self.page.locator('.build-preview-error')).to_contain_text('加载超时')
        await self.preview_message({'type': 'atoms-preview-state', 'state': 'ready'})
        await expect(self.page.locator('.build-preview-error')).to_have_count(0)
        await self.preview_message({'type': 'atoms-preview-error', 'kind': 'runtime', 'message': '真实运行错误'})
        await self.preview_message({'type': 'atoms-preview-state', 'state': 'ready'})
        await expect(self.page.locator('.build-preview-error')).to_contain_text('真实运行错误')

        await self.preview_message({'type': 'atoms-preview-error', 'kind': 'runtime', 'message': '旧帧错误', 'frameId': 'old-frame'})
        await self.preview_message({'type': 'atoms-preview-error', 'kind': 'runtime', 'message': '旧文档错误', 'documentId': 'old-document', 'documentStartedAt': 1})
        await expect(self.page.locator('.build-preview-error')).to_contain_text('真实运行错误')

    async def test_iframe_navigation_without_query_negotiates_context_and_recovers(self):
        self.complete_generation()
        await self.reconcile()
        await expect(self.page.frame_locator('.build-viewer-body iframe').get_by_text('可用的版本预览')).to_be_visible()
        await expect(self.page.locator('.build-preview-loading')).to_have_count(0)
        await self.preview_message({'type': 'atoms-preview-error', 'kind': 'runtime', 'message': '旧页面错误'})
        await expect(self.page.locator('.build-preview-error')).to_contain_text('旧页面错误')
        frame = self.page.frames[-1]
        self.preview_html = '<h1>新页面正常</h1>'
        await frame.goto(frame.url.split('?')[0] + 'next')
        await expect(self.page.frame_locator('.build-viewer-body iframe').get_by_text('新页面正常')).to_be_visible()
        await expect(self.page.locator('.build-preview-error')).to_have_count(0)
        await expect(self.page.locator('.build-preview-loading')).to_have_count(0)

    async def test_restoration_chat_updates_while_waiting_for_container_ack(self):
        self.complete_generation()
        await self.reconcile()
        await expect(self.page.get_by_test_id('project-status')).to_have_text('ready')
        self.restore_response_gate = asyncio.Event()
        try:
            await self.page.get_by_title('历史记录',exact=True).first.click()
            await self.page.get_by_role('button',name='还原',exact=True).click()
            card=self.page.locator('.build-restoration-card[data-restore-status="running"]')
            await expect(card).to_contain_text('正在重建运行环境',timeout=10000)
            self.project['messages'][-1]['restoration']['phase']='正在恢复数据库和浏览器数据'
            await self.reconcile()
            await expect(card).to_contain_text('正在恢复数据库和浏览器数据',timeout=10000)
            self.assertFalse(self.restore_response_gate.is_set())
            await expect(self.page.get_by_text('从版本 v1 恢复',exact=True)).to_be_visible()
        finally:
            self.restore_response_gate.set()

    async def test_restoration_chat_progress_survives_reload_and_shows_terminal_result(self):
        self.complete_generation()
        self.project['messages'].extend([
            {'id':'restore-request','role':'user','agent':None,'content':'从版本 v1 恢复','created_at':'2026-10-09T08:02:00Z','restoration':{'kind':'restore','version':1,'status':'running','phase':'正在校验版本文件','steps':[]}},
            {'id':'restore-message','role':'assistant','agent':'Mike','content':'正在还原版本 1，需要些时间，请等待…','created_at':'2026-10-09T08:02:01Z','restoration':{'kind':'restore','version':1,'status':'running','phase':'正在恢复数据库和浏览器数据','steps':[{'phase':'正在恢复数据库和浏览器数据','at':'2026-10-09T08:02:01Z'}]}}
        ])
        self.project['status']='restoring'
        self.operation={'id':'restore-message','version':1,'status':'running','phase':'正在恢复数据库和浏览器数据','error':'','result':{}}
        await self.reconcile()
        card=self.page.locator('.build-restoration-card')
        await expect(card).to_contain_text('正在恢复数据库和浏览器数据',timeout=10000)
        await self.page.reload()
        await expect(card).to_contain_text('正在恢复数据库和浏览器数据',timeout=10000)
        self.project['status']='ready'
        self.operation['status']='succeeded'
        self.operation['result']={'restored_version':2,'restored_preview':{'url':'/api/runtime/restored/','mode':'live','output':''}}
        message=self.project['messages'][-1]
        message['content']='已从版本 v1 恢复，代码、数据库和浏览器数据已同步，预览验证通过。'
        message['restoration']['status']='succeeded'
        message['restoration']['steps'].append({'phase':'还原完成，预览验证通过','at':'2026-10-09T08:03:00Z'})
        await self.reconcile()
        await expect(card).to_have_attribute('data-restore-status','succeeded',timeout=10000)
        await expect(card).to_contain_text('还原完成，预览验证通过')
        await expect(self.page.get_by_text(message['content'],exact=True)).to_be_visible()
        await expect(self.page.locator('.build-workflow')).to_have_count(0)
