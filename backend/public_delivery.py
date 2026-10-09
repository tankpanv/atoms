"""Published app shell with an isolated app frame and browser-scoped storage.

Only platform-authored bridge code runs outside the opaque sandbox. App storage
is namespaced by project and browser; it never reads preview users' saved tokens.
"""
import html as html_lib
import json
import re

from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from browser_storage import STORAGE_SHIM


class PlatformCors(CORSMiddleware):
    """Platform CORS must not intercept opaque app-origin public preflights.

    Published handlers provide their own CORS and never use platform cookies.
    Keep the authenticated control-plane origin allowlist unchanged.
    """
    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http' and scope.get('path', '').startswith('/api/public/'):
            return await self.app(scope, receive, send)
        return await super().__call__(scope, receive, send)


def script_json(value):
    return json.dumps(value, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


def thumbnail_document(html):
    """Render a card without letting app startup focus/scroll its parent page.

    An inert iframe element does not make its child document inert. Apply this
    inside the document before any app scripts; full interactive previews omit it.
    """
    script = '''<script data-atoms-thumbnail>
    (() => {
      document.documentElement.inert = true;
      const ignore = () => {};
      for (const prototype of [HTMLElement.prototype, SVGElement.prototype]) {
        Object.defineProperty(prototype, 'focus', {value:ignore, configurable:false, writable:false});
      }
      Object.defineProperty(Element.prototype, 'scrollIntoView', {value:ignore, configurable:false, writable:false});
      window.focus = ignore;
    })();
    </script>'''
    html, count = re.subn(r'<head(?:\s[^>]*)?>', lambda match: match.group(0) + script, html, count=1, flags=re.I)
    return html if count else '<head>' + script + '</head>' + html


def published_document(html, prefix, frame_url, *, framed=False, thumbnail=False):
    headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
               'Referrer-Policy': 'no-referrer'}
    if framed:
        html = re.sub(r'((?:src|href|action)=["\'])/(?!/)', lambda m: m.group(1) + prefix + '/', html)
        if not re.search(r'<base\s', html, re.I):
            html, count = re.subn(r'<head(?:\s[^>]*)?>', lambda m: m.group(0) + f'<base href="{prefix}/">', html, count=1, flags=re.I)
            if not count:
                html = f'<head><base href="{prefix}/"></head>' + html
        html = re.sub(r'<head(?:\s[^>]*)?>', lambda m: m.group(0) + STORAGE_SHIM, html, count=1, flags=re.I)
        if thumbnail:
            html = thumbnail_document(html)
        headers.update({'Content-Security-Policy': 'sandbox allow-scripts allow-forms allow-modals',
                        'Access-Control-Allow-Origin': '*', 'Cross-Origin-Resource-Policy': 'cross-origin'})
        return HTMLResponse(html, headers=headers)

    title = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
    title = html_lib.escape(html_lib.unescape(title[1]) if title else '应用')
    # Do not embed app HTML in the host document. The frame gets a separate
    # request with CSP sandbox and its storage shim before application scripts.
    shell = '''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>''' + title + '''</title><style>html,body{margin:0;height:100%;overflow:hidden}iframe{display:block;border:0;width:100%;height:100%}</style></head>
    <body><script>
    (() => {
      const scope = ''' + script_json(prefix) + ''';
      const localKey = 'atoms-published:' + scope + ':local';
      const sessionKey = 'atoms-published:' + scope + ':session';
      const read = (storage, key) => {try {return JSON.parse(storage.getItem(key) || '{}')} catch (_) {return {}}};
      const random = new Uint8Array(24); crypto.getRandomValues(random);
      const bridge = Array.from(random, x => x.toString(16).padStart(2, '0')).join('');
      let local, session;
      try {local = read(localStorage, localKey)} catch (_) {local = {}}
      try {session = read(sessionStorage, sessionKey)} catch (_) {session = {}}
      const frame = document.createElement('iframe');
      frame.title = document.title;
      frame.setAttribute('sandbox', 'allow-scripts allow-forms allow-modals');
      frame.name = JSON.stringify({atomsSession:{scope, local, values:session, bridge}});
      window.addEventListener('message', event => {
        const data = event.data;
        if (event.source !== frame.contentWindow || !data || data.type !== 'atoms-public-storage' || data.bridge !== bridge || data.scope !== scope) return;
        const valid = values => values && typeof values === 'object' && !Array.isArray(values) && Object.values(values).every(v => typeof v === 'string');
        if (!valid(data.local) || !valid(data.values)) return;
        const localText = JSON.stringify(data.local), sessionText = JSON.stringify(data.values);
        if (localText.length > 2000000 || sessionText.length > 2000000) return;
        try {localStorage.setItem(localKey, localText)} catch (_) {}
        try {sessionStorage.setItem(sessionKey, sessionText)} catch (_) {}
      });
      frame.src = ''' + script_json(frame_url) + ''';
      document.body.append(frame);
    })();
    </script></body></html>'''
    return HTMLResponse(shell, headers=headers)
