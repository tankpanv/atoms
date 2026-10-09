"""Bounded evidence from real project browser requests, without response secrets."""
import asyncio
from urllib.parse import urlsplit


class BrowserNetwork:
    def __init__(self, page, url, prefix):
        self.origin = urlsplit(url)
        self.prefix = prefix if self.origin.path.startswith(prefix + '/') else ''
        self.responses, self.pending, self.failed, self.expected = [], set(), [], set()
        self.previous_operation = set()
        page.on('request', self.request)
        page.on('response', self.response)
        page.on('requestfinished', lambda request: self.pending.discard(request))
        page.on('requestfailed', self.failure)

    def request(self, request):
        if request.resource_type in ('fetch', 'xhr'):
            self.pending.add(request)

    def response(self, response):
        self.responses.append(response)
        # fetch() resolves when headers arrive. A successful write caller may
        # intentionally ignore its body; Chromium can then omit requestfinished
        # until navigation/cleanup. That must not invalidate a real POST + GET
        # readback. Content is checked by expect_json and workflow assertions;
        # HTTP errors and actual transfer failures remain independently tracked.
        self.pending.discard(response.request)
        if len(self.responses) > 500:
            self.responses.pop(0)
            if 'Network evidence limit exceeded' not in self.failed:
                self.failed.append('Network evidence limit exceeded')

    def failure(self, request):
        self.pending.discard(request)
        self.failed.append(str(request.failure) + ' ' + urlsplit(request.url).path)

    def project_path(self, response):
        url = urlsplit(response.url)
        if (url.scheme, url.netloc) != (self.origin.scheme, self.origin.netloc):
            return None
        if self.prefix and not url.path.startswith(self.prefix + '/'):
            return None
        return url.path[len(self.prefix):]

    def begin_operation(self):
        self.previous_operation = set(self.responses)

    async def assert_response(self, action, contains_json):
        path, method, status = action['selector'], action.get('method', 'GET'), action.get('status', 200)
        deadline = asyncio.get_running_loop().time() + 8
        checked, diagnostic = set(), 'no matching response'
        while asyncio.get_running_loop().time() < deadline:
            for response in list(self.responses):
                if response in self.expected or response in checked or response in self.previous_operation:
                    continue
                if self.project_path(response) != path or response.request.method != method:
                    continue
                checked.add(response)
                diagnostic = f'actual HTTP {response.status}'
                if response.status != status:
                    continue
                if 'expect_json' in action:
                    try:
                        body = await asyncio.wait_for(response.json(), max(.01, deadline - asyncio.get_running_loop().time()))
                    except (ValueError, asyncio.TimeoutError):
                        diagnostic = 'expected JSON was not received'
                        continue
                    if not contains_json(body, action['expect_json']):
                        diagnostic = 'JSON result does not match expected fields'
                        continue
                self.expected.add(response)
                return
            await asyncio.sleep(.05)
        raise AssertionError(f'Expected project response {method} {path} HTTP {status}: {diagnostic}')

    async def settle(self):
        # Wait for requests already triggered by this flow. Do not follow an
        # endless polling cycle or streaming connection indefinitely.
        started = set(self.pending)
        deadline = asyncio.get_running_loop().time() + 5
        while started.intersection(self.pending) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(.05)
        if started.intersection(self.pending):
            waiting = [f'{getattr(request, "method", "GET")} {urlsplit(request.url).path}'
                       for request in started.intersection(self.pending)]
            self.failed.append('Project workflow requests did not complete within 5 seconds: ' + ', '.join(waiting[:8]))

    def failures(self):
        return self.failed + [f'{r.status} {urlsplit(r.url).path}' for r in self.responses
                              if r.status >= 400 and r not in self.expected]

    def summary(self):
        return [{'path': self.project_path(r), 'method': r.request.method, 'status': r.status,
                 'asserted': r in self.expected} for r in self.responses
                if self.project_path(r) is not None and r.request.resource_type in ('fetch', 'xhr')][-50:]
