"""Real Chromium coverage for slow mounts, late recovery and script failures."""

import asyncio
import re
import unittest
from playwright.async_api import async_playwright, expect
from preview_diagnostics import inject_preview_diagnostics


class PreviewDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        self.page = await self.browser.new_page()
        await self.page.clock.install()
        self.messages = []
        await self.page.expose_function('recordPreview', lambda message: self.messages.append(message))
        await self.page.add_init_script("window.addEventListener('message', e => { if(e.data?.type?.startsWith('atoms-preview-')) window.recordPreview(e.data) })")

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def open(self, body):
        await self.page.route(re.compile(r'^http://preview\.test/\?'), lambda route: route.fulfill(content_type='text/html; charset=utf-8', body=inject_preview_diagnostics(body)))
        await self.page.goto('http://preview.test/?__atoms_frame=test', wait_until='commit')
        await self.page.wait_for_function("!!document.querySelector('[data-atoms-preview-diagnostics]')")

    async def flush(self):
        await self.page.evaluate('() => new Promise(resolve => setTimeout(resolve, 0))')
        await asyncio.sleep(.1)

    def errors(self):
        return [item for item in self.messages if item['type'] == 'atoms-preview-error']

    async def test_cold_module_taking_more_than_four_seconds_is_loading_then_ready(self):
        release = asyncio.Event()
        async def module(route):
            await release.wait()
            await route.fulfill(content_type='application/javascript', body="document.getElementById('root').innerHTML='<h1>加载成功</h1>'")
        await self.page.route('**/app.js', module)
        await self.open('<html><head><script type="module" src="/app.js"></script></head><body><div id="root"></div></body></html>')
        try:
            await self.page.clock.fast_forward(9000)
            await self.flush()
            self.assertEqual(self.errors(), [])
            self.assertIn('loading', [item.get('state') for item in self.messages])
        finally:
            release.set()
        await expect(self.page.get_by_text('加载成功')).to_be_visible()
        await self.flush()
        self.assertEqual(self.messages[-1]['state'], 'ready')
        self.assertEqual(self.messages[-1]['frameId'], 'test')

    async def test_mount_timeout_recovers_when_app_eventually_mounts_without_console_error(self):
        await self.open('<head></head><body><div id="app"></div></body>')
        await self.page.clock.fast_forward(31000)
        await self.flush()
        self.assertEqual([item['kind'] for item in self.errors()], ['mount_timeout'])
        self.assertFalse(any(item.get('level') == 'error' for item in self.messages))
        await self.page.evaluate("document.getElementById('app').innerHTML='<button>恢复成功</button>'")
        await self.flush()
        self.assertEqual(self.messages[-1]['state'], 'ready')

    async def test_inline_script_failure_is_captured_before_mount_and_stays_fatal(self):
        await self.open('<head><script>throw new Error("真实脚本错误")</script></head><body><div id="root">部分内容</div></body>')
        await self.flush()
        self.assertIn('真实脚本错误', self.errors()[0]['message'])
        self.assertEqual(self.errors()[0]['kind'], 'runtime')
        self.assertTrue(next(item for item in self.messages if item.get('state') == 'ready')['fatal'])

    async def test_missing_script_is_runtime_failure(self):
        await self.page.route('**/missing.js', lambda route: route.fulfill(status=404))
        await self.open('<head><script src="/missing.js"></script></head><body><div id="root"></div></body>')
        await self.flush()
        self.assertEqual(self.errors()[0]['kind'], 'runtime')
        self.assertIn('missing.js', self.errors()[0]['message'])

    async def test_headless_static_document_reports_ready(self):
        await self.open('<h1>静态应用</h1>')
        await self.flush()
        self.assertTrue(any(item.get('state') == 'ready' for item in self.messages))
        self.assertEqual(self.errors(), [])
