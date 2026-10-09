"""Real runtime, HTTP and browser acceptance tools scoped to a project."""

from __future__ import annotations

import json
import mimetypes
import re
from contextlib import ExitStack
import os
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from runtime import start_runtime, upstream_path
from tool_limits import BROWSER_ACTIONS_MAX


def browser_state_path(root):
    # Private harness state is excluded from source snapshots and downloads.
    return Path(root) / '.atoms' / '.agent-session' / 'browser.json'


def load_browser_state(root, project_id):
    path = browser_state_path(root)
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(Path(root).resolve()) or path.stat().st_size > 5_000_000:
            return {}
        saved = json.loads(path.read_text())
        return saved if saved.get('project_id') == str(project_id) else {}
    except (OSError, ValueError, AttributeError):
        return {}


async def page_controls(page):
    # Return actual attributes and usable selectors, also after a failed action.
    # A button's implicit submit behavior does not imply a type=submit attribute.
    return await page.locator('a,button,input,select,textarea,[role="button"]').evaluate_all("""els => els.filter(e => e.getBoundingClientRect().width && e.getBoundingClientRect().height).slice(0,60).map(e => {
      const text=(e.innerText||e.getAttribute('aria-label')||'').trim();
      // Select innerText contains option labels and newlines, not a reliable
      // Playwright text target. A real DOM path also disambiguates equal labels.
      const path=[];let node=e;
      while(node && node.nodeType===1){
        if(node.id && document.querySelectorAll('#'+CSS.escape(node.id)).length===1){path.unshift('#'+CSS.escape(node.id));break;}
        const tag=node.tagName.toLowerCase();
        const siblings=node.parentElement?[...node.parentElement.children].filter(s=>s.tagName===node.tagName):[node];
        path.unshift(tag+(siblings.length>1?':nth-of-type('+(siblings.indexOf(node)+1)+')':''));node=node.parentElement;
      }
      const testid=e.getAttribute('data-testid');
      const testSelector=testid?'[data-testid='+JSON.stringify(testid)+']':null;
      return {tag:e.tagName.toLowerCase(),text:text.slice(0,200),id:e.id,testid:e.getAttribute('data-testid'),placeholder:e.getAttribute('placeholder'),type:e.getAttribute('type'),href:e.getAttribute('href'),disabled:!!e.disabled,
        selector:testSelector&&document.querySelectorAll(testSelector).length===1?testSelector:path.join(' > '),
        ...(e.tagName==='SELECT'?{options:[...e.options].map(o=>({value:o.value,text:o.text,disabled:o.disabled}))}: {})};
    })""")


def local_path(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "\\" in path:
        raise ValueError("仅允许当前项目服务的相对 HTTP 路径，例如 /api/health")
    return path


def project_relative_path(runtime, path):
    """Accept the current runtime URL returned by tools without doubling it."""
    prefix = getattr(runtime, 'prefix', '')
    path_part = urlsplit(path).path
    if prefix and (path_part == prefix or path_part.startswith(prefix + '/')):
        return '/' + path[len(prefix):].lstrip('/')
    return path


def contains_json(actual, expected) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and contains_json(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        # An empty expected list asserts absence, not a vacuous subset match.
        return isinstance(actual, list) and (not actual if not expected else
            all(any(contains_json(item, value) for item in actual) for value in expected))
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(actual) is type(expected) and actual == expected
    return actual == expected


def response_value(session, name, pointer):
    if not isinstance(pointer, str) or (pointer and not pointer.startswith('/')):
        raise ValueError('响应字段使用 JSON Pointer，例如 /access_token 或 /user/id')
    value = session.http_response(name)
    try:
        for part in pointer.split('/')[1:] if pointer else []:
            key = part.replace('~1', '/').replace('~0', '~')
            value = value[int(key)] if isinstance(value, list) and key.isdecimal() else value[key]
        return value
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f'HTTP 响应 {name} 中不存在字段 {pointer}') from exc


def resolve_http_args(project_id, args):
    """Copy opaque values in the executor, never through model transcription."""
    if not (args.get('save_as') or args.get('auth_from') or '{{http:' in json.dumps(args)):
        return args, None
    from agent import ensure_workspace
    from agent_session import AgentSession
    session = AgentSession(ensure_workspace(project_id))
    pattern = re.compile(r'\{\{http:([A-Za-z][A-Za-z0-9_-]{0,63}):([^{}]*)\}\}')
    def resolve(value):
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if not isinstance(value, str):
            return value
        match = pattern.fullmatch(value)
        if match:
            return response_value(session, *match.groups())
        def substitute(match):
            item = response_value(session, *match.groups())
            if not isinstance(item, (str, int, float)) or isinstance(item, bool):
                raise ValueError('字符串内的 HTTP 响应引用必须指向字符串或数字')
            return str(item)
        return pattern.sub(substitute, value)
    resolved = resolve(args)
    if args.get('auth_from'):
        auth = args['auth_from']
        token = response_value(session, auth['response'], auth.get('pointer', '/access_token'))
        if not isinstance(token, str) or not token or '\r' in token or '\n' in token:
            raise ValueError('认证响应字段必须为非空单行字符串')
        if any(key.lower() == 'authorization' for key in resolved.get('headers', {})):
            raise ValueError('auth_from 与 Authorization header 不得同时提供，避免账号身份混淆')
        resolved['headers'] = {**resolved.get('headers', {}), 'Authorization': 'Bearer ' + token}
    return resolved, session


async def http_request(project_id: uuid.UUID, args: dict):
    args, bindings = resolve_http_args(project_id, args)
    if args.get('save_as'):
        # Validate the name before executing a potentially mutating request.
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', args['save_as']):
            raise ValueError('save_as 必须为合法的 HTTP 响应名称')
    runtime = await start_runtime(project_id, args.get('configured_command', ''))
    if runtime is None:
        raise ValueError("项目未配置可运行服务")
    path = project_relative_path(runtime, local_path(args.get("path", "/")))
    service_name = args.get("service", "")
    if service_name:
        service = next((item for item in runtime.services if item.name == service_name), None)
        if not service or service.process.returncode is not None:
            raise ValueError("所选项目服务不存在或已停止")
        port = service.port
    else:
        port = runtime.port
        path = upstream_path(runtime, path.lstrip("/"))
    method = args.get("method", "GET").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
        raise ValueError("不支持的 HTTP 方法")
    expected = args.get("expect_status", 200)
    headers = args.get("headers", {})
    if not isinstance(headers, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in headers.items()):
        raise ValueError("headers 必须为字符串键值对象，可携带项目自己的 Authorization token")
    content_type = next((value for key, value in headers.items() if key.lower() == 'content-type'), '').lower()
    if 'form' in args and 'body' in args:
        raise ValueError('form 与 body 不能同时提供；根据真实接口选择请求编码')
    with ExitStack() as opened:
        payload = {}
        uploads = args.get('files', [])
        if uploads:
            from agent import ensure_workspace, safe_file
            root = ensure_workspace(project_id)
            files = []
            for upload in uploads:
                file = safe_file(root, upload['path'])
                files.append((upload['field'], (file.name, opened.enter_context(file.open('rb')),
                              upload.get('content_type') or mimetypes.guess_type(file.name)[0] or 'application/octet-stream')))
            payload['files'] = files
            headers = {key: value for key, value in headers.items() if key.lower() != 'content-type'}
            payload['data'] = args.get('form', args.get('body', {}))
            if not isinstance(payload['data'], dict):
                raise ValueError('multipart 字段必须为对象；文件使用 files 提供真实项目文件')
        elif 'form' in args:
            payload['data'] = args['form']
        elif isinstance(args.get('body'), str):
            payload['content'] = args['body']  # Raw form/text/JSON must not be JSON-string encoded.
        elif 'application/x-www-form-urlencoded' in content_type:
            payload['data'] = args.get('body') or {}
        elif 'body' in args:
            payload['json'] = args['body']
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.request(method, f"http://127.0.0.1:{port}{path}", headers=headers, **payload)
    passed = response.status_code == expected
    assertion_errors = []
    if not passed:
        assertion_errors.append(f'HTTP 状态不匹配：期望 {expected}，实际 {response.status_code}')
    if "expect_json" in args:
        try:
            if not contains_json(response.json(), args["expect_json"]):
                passed = False
                assertion_errors.append('JSON 内容与 expect_json 不匹配；空数组要求实际数组为空')
        except ValueError:
            passed = False
            assertion_errors.append('响应不是有效 JSON')
    if 'expect_body' in args and response.text != args['expect_body']:
        passed = False
        assertion_errors.append('响应正文与 expect_body 不匹配')
    if 'expect_schema' in args:
        from runtime_assertions import shape_issues
        try:
            schema_errors = shape_issues(response.json(), args['expect_schema'])
            assertion_errors.extend(schema_errors)
            passed = passed and not schema_errors
        except ValueError as error:
            passed = False
            assertion_errors.append(str(error))
    try:
        data = response.json()
        simulated = isinstance(data, dict) and data.get('simulated') is True
        if bindings and args.get('save_as') and passed:
            bindings.http_response(args['save_as'], data)
    except ValueError:
        simulated = False
        if bindings and args.get('save_as') and passed:
            passed = False
            assertion_errors.append('save_as 要求有效 JSON 响应，未保存响应绑定')
    output = json.dumps({"method": method, "path": path, "status": response.status_code, "simulated": simulated,
                         "assertion_version": 2,
                         "expected_status": expected, "passed": passed, "body": response.text[:8000],
                         "validated_json": args.get('expect_json') if passed else None,
                         "validated_schema": args.get('expect_schema') if passed else None,
                         "validated_body": args.get('expect_body') if passed else None,
                         "saved_as": args.get('save_as') if passed else None,
                         "auth_response": (args.get('auth_from') or {}).get('response'),
                         "next_action": ('重新登录并 save_as，然后用 auth_from 引用完整响应；先判别凭据/实验输入，不据此直接修改业务鉴权。'
                                         if response.status_code == 401 and expected != 401 else None),
                         "assertion_errors": assertion_errors}, ensure_ascii=False)
    return 0 if passed else 1, output


async def browser_preview_url(project_id, runtime, path):
    # Accept a path copied from runtime_check without duplicating its prefix.
    path = project_relative_path(runtime, path)
    target = upstream_path(runtime, path.lstrip('/'))
    # localhost is a secure context even over HTTP. Use an ordinary HTTP
    # origin so fixture checks expose the same API limits as LAN deployment.
    url = f"http://atoms-preview.test:{runtime.port}{target}"
    gateway = os.getenv('PREVIEW_GATEWAY_URL') or ('http://preview:8002' if os.getenv('WORKER_TOKEN') else '')
    if gateway:
        if os.getenv('WORKER_TOKEN'):
            if str(uuid.UUID(os.environ['PROJECT_ID'])) != str(project_id):
                raise ValueError('浏览器验收不能跨项目访问')
            async with httpx.AsyncClient(timeout=10) as client:
                prepared = await client.post(f'http://agent-service:9001/projects/{project_id}/preview-check/{runtime.token}',
                                            headers={'X-Worker-Token': os.environ['WORKER_TOKEN']}, json={})
                prepared.raise_for_status()
        # Production checks use exactly the gateway/sandbox/storage path shown
        # in App Viewer. Never put worker/platform secrets in browser headers.
        url = gateway.rstrip('/') + runtime.prefix + '/' + path.removeprefix(runtime.prefix).lstrip('/')
    return url


async def browser_check(project_id: uuid.UUID, root, args: dict):
    from playwright.async_api import async_playwright

    runtime = await start_runtime(project_id, args.get('configured_command', ''))
    if runtime is None:
        raise ValueError("项目未配置浏览器开发服务")
    path = local_path(args.get("path", "/"))
    try:
        url = await browser_preview_url(project_id, runtime, path)
    except httpx.HTTPError as exc:
        return 1, json.dumps({'error': '预览网关准备失败，请检查运行环境：' + str(exc),
                              'errors': [], 'failed_responses': ['preview_gateway_unavailable']}, ensure_ascii=False)
    actions = args.get("actions", [])
    if not isinstance(actions, list) or len(actions) > BROWSER_ACTIONS_MAX:
        raise ValueError(f"浏览器动作必须为最多 {BROWSER_ACTIONS_MAX} 项的列表")
    if args.get("requirement_ids") and not any(str(item.get("action", "")).startswith("assert_") and item.get("selector") not in (None,"body","html") for item in actions):
        raise ValueError("需求验收必须包含可观察的 assert_* 断言，不能仅打开页面")
    errors, failures, results = [], [], []
    dialogs = []
    resource_errors = []
    screenshot = root / ".atoms" / "screenshots" / f"{uuid.uuid4().hex}.png"
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    passed = True
    # Chromium subprocesses drop capabilities and cannot traverse UID-owned project
    # directories (0700). Give them a private browser-owned temporary directory.
    with tempfile.TemporaryDirectory(prefix="atoms-browser-") as temporary:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--host-resolver-rules=MAP atoms-preview.test 127.0.0.1", "--no-proxy-server"],
                env={"PATH": os.getenv("PATH", "/usr/bin:/bin"), "HOME": temporary, "TMPDIR": temporary})
            try:
                saved = load_browser_state(root, project_id)
                origin = '{0.scheme}://{0.netloc}'.format(urlsplit(url))
                if saved.get('origin') != origin:
                    saved = {}
                context = await browser.new_context(viewport={"width": 1440, "height": 900}, storage_state=saved.get('storage'))
                # Playwright storage_state does not include sessionStorage.
                # Restore only this project's origin, before application scripts.
                await context.add_init_script('''(() => {
                  const saved = ''' + json.dumps({'origin': origin, 'session': saved.get('session', {})}) + ''';
                  if (location.origin !== saved.origin || window.name) return;
                  const scope = location.pathname.match(/^\\/api\\/(?:runtime|preview)\\/[^/]+/);
                  window.name = scope ? JSON.stringify({atomsSession:{scope:scope[0],values:saved.session}}) : 'atoms-browser-session-restored';
                  try { if (!sessionStorage.length) for (const [key,value] of Object.entries(saved.session)) sessionStorage.setItem(key,value); } catch (_) {}
                })();''')
                page = await context.new_page()
                dialog_policy = 'dismiss'
                async def handle_dialog(dialog):
                    dialogs.append({'type':dialog.type,'message':dialog.message[:1000],'response':dialog_policy})
                    if dialog_policy == 'accept':
                        await dialog.accept()
                    else:
                        await dialog.dismiss()
                page.on('dialog', handle_dialog)
                page.set_default_timeout(8000)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("crash", lambda: errors.append("Chromium 页面进程崩溃"))
                def console_error(message):
                    if message.type == 'error':
                        (resource_errors if message.text.startswith('Failed to load resource:') else errors).append(message.text)
                page.on('console', console_error)
                from browser_evidence import BrowserNetwork
                network = BrowserNetwork(page, url, runtime.prefix)
                response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if response is None or response.status >= 400:
                    passed = False
                if args.get('startup_check'):
                    await page.wait_for_function("() => document.body && (document.body.innerText.trim().length > 0 || [...document.querySelectorAll('canvas,svg,img,video,input,button')].some(e => e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0))", timeout=8000)
                for action in actions:
                    kind = action.get("action")
                    dialog_policy = action.get('dialog', 'dismiss')
                    if kind in {"click","fill","fill_from_text","press","check","uncheck","select","reload"}:
                        network.begin_operation()
                    locator = page.locator(action.get("selector", "body"))
                    if kind == "assert_response":
                        await network.assert_response(action, contains_json)
                    elif kind == "click":
                        await locator.click()
                    elif kind == "fill":
                        await locator.fill(action["value"])
                    elif kind == "fill_from_text":
                        await locator.fill(await page.locator(action['source_selector']).inner_text())
                    elif kind == "press":
                        await locator.press(action["value"])
                    elif kind == "check":
                        await locator.check()
                    elif kind == "uncheck":
                        await locator.uncheck()
                    elif kind == "select":
                        await locator.select_option(action["value"])
                    elif kind == "reload":
                        await page.reload(wait_until="domcontentloaded")
                    elif kind == "assert_visible":
                        await locator.first.wait_for(state="visible")
                    elif kind == "assert_text":
                        from playwright.async_api import expect
                        await expect(locator).to_contain_text(action["value"])
                    elif kind == "assert_value":
                        from playwright.async_api import expect
                        await expect(locator).to_have_value(action['value'])
                    elif kind == "assert_count":
                        from playwright.async_api import expect
                        await expect(locator).to_have_count(int(action["value"]))
                    else:
                        raise ValueError(f"不支持的浏览器动作：{kind}")
                    results.append(action)
                await page.wait_for_timeout(300)
                await network.settle()
                failures = network.failures()
                for service in ([runtime] if getattr(runtime, 'process', None) else []) + list(getattr(runtime, 'services', [])):
                    if service.process.returncode is not None:
                        failures.append('Required runtime service stopped: ' + getattr(service, 'name', 'frontend'))
                if failures:
                    errors.extend(resource_errors)
                await page.screenshot(path=str(screenshot), full_page=True)
                output = {"url": url, "title": await page.title(), "text": (await page.locator('body').inner_text())[:5000],
                          "controls": await page_controls(page),
                          # Text outputs (answers, feedback, counters) are not
                          # controls. Expose their actual selectors as evidence
                          # so the scene does not have to invent a source node.
                          "observables": await page.locator('[data-testid],[id],[role="status"],[role="alert"]').evaluate_all("els => els.filter(e => e.getBoundingClientRect().width && e.getBoundingClientRect().height).slice(0,80).map(e => ({tag:e.tagName.toLowerCase(),id:e.id,testid:e.getAttribute('data-testid'),role:e.getAttribute('role'),text:(e.innerText||'').slice(0,300),children:Array.from(e.children).filter(c => c.getBoundingClientRect().width && c.getBoundingClientRect().height).slice(0,8).map(c => ({tag:c.tagName.toLowerCase(),text:(c.innerText||'').slice(0,200)}))}))"),
                          "rendered_elements": await page.locator('canvas,svg,img,video,input,button').evaluate_all("els => els.filter(e => e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0).length"),
                          "starter_page": await page.locator('[data-atoms-starter="true"]').count() > 0,
                          "actions_completed": results, "dialogs":dialogs, "network": network.summary(), "browser_environment": await page.evaluate("() => ({secure_context:isSecureContext,origin:location.origin,random_uuid:typeof crypto.randomUUID})"), "verification_environment": "preview_gateway" if os.getenv("WORKER_TOKEN") or os.getenv("PREVIEW_GATEWAY_URL") else "local_fixture", "errors": errors, "failed_responses": failures,
                          "screenshot": str(screenshot.relative_to(root))}
                passed = passed and not errors and not failures
            except Exception as exc:
                if "network" in locals():
                    failures = network.failures()
                errors.extend(resource_errors)
                passed = False
                output = {"url": url, "actions_completed": results, "dialogs":dialogs, "error": str(exc), "errors": errors, "failed_responses": failures}
                if 'page' in locals():
                    try:
                        output['controls'] = await page_controls(page)
                        output['text'] = (await page.locator('body').inner_text(timeout=1000))[:5000]
                        output['observables'] = await page.locator('[data-testid],[id],[role="status"],[role="alert"]').evaluate_all("els => els.filter(e => e.getBoundingClientRect().width && e.getBoundingClientRect().height).slice(0,80).map(e => ({tag:e.tagName.toLowerCase(),id:e.id,testid:e.getAttribute('data-testid'),role:e.getAttribute('role'),text:(e.innerText||'').slice(0,300),children:Array.from(e.children).filter(c => c.getBoundingClientRect().width && c.getBoundingClientRect().height).slice(0,8).map(c => ({tag:c.tagName.toLowerCase(),text:(c.innerText||'').slice(0,200)}))}))")
                    except Exception:
                        pass
                    try:
                        await page.screenshot(path=str(screenshot), full_page=True, timeout=5000)
                        output["screenshot"] = str(screenshot.relative_to(root))
                    except Exception as capture_error:
                        output["screenshot_error"] = str(capture_error)
            finally:
                if 'page' in locals() and not page.is_closed():
                    try:
                        session = await page.evaluate("() => Object.fromEntries(Array.from({length:sessionStorage.length},(_,i)=>{const k=sessionStorage.key(i);return [k,sessionStorage.getItem(k)]}))")
                        state = {'project_id': str(project_id), 'origin': origin, 'session': session,
                                 'storage': await context.storage_state()}
                        target = browser_state_path(root)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if not target.parent.resolve().is_relative_to(Path(root).resolve()):
                            raise ValueError('browser state outside project')
                        with tempfile.NamedTemporaryFile(mode='w', dir=target.parent, prefix='browser-', delete=False) as handle:
                            json.dump(state, handle)
                            temporary_state = Path(handle.name)
                        temporary_state.replace(target)
                    except Exception as capture_error:
                        output['browser_state_error'] = type(capture_error).__name__
                await browser.close()
    return 0 if passed else 1, json.dumps(output, ensure_ascii=False)
