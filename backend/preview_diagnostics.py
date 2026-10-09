"""Browser lifecycle reporting shared by live and static previews."""

import re


PREVIEW_DIAGNOSTICS = """<script data-atoms-preview-diagnostics>
(() => {
  let frameId = new URLSearchParams(location.search).get('__atoms_frame') || '';
  const documentId = Math.random().toString(36).slice(2);
  const documentStartedAt = performance.timeOrigin;
  const pending = [];
  const post = data => {
    if (!frameId && parent !== window) { pending.push(data); if (pending.length > 300) pending.shift(); return; }
    parent.postMessage({...data, frameId, documentId, documentStartedAt}, '*');
  };
  // Internal links and reloads can drop the initial query string. Ask the
  // owning iframe for context without using application/session storage.
  window.addEventListener('message', event => {
    if (event.source !== parent || event.data?.type !== 'atoms-preview-context' || event.data.documentId !== documentId) return;
    frameId = event.data.frameId;
    for (const data of pending.splice(0)) post(data);
  });
  if (!frameId && parent !== window) parent.postMessage({type:'atoms-preview-handshake',documentId,documentStartedAt}, '*');
  const send = (level, message) => post({type:'atoms-preview-console',level,message:String(message).slice(0,2000)});
  const format = value => {
    if (typeof value === 'string') return value;
    try { return JSON.stringify(value); } catch { return String(value); }
  };
  for (const level of ['log','info','warn','error','debug']) {
    const original = console[level].bind(console);
    console[level] = (...args) => { send(level, args.map(format).join(' ')); original(...args); };
  }
  let fatal = false;
  const fail = message => {
    fatal = true;
    send('error', message);
    post({type:'atoms-preview-error',kind:'runtime',message});
  };
  window.addEventListener('error', event => {
    if (event.target && event.target !== window) {
      const target = event.target;
      if (target.tagName === 'SCRIPT' || (target.tagName === 'LINK' && target.rel === 'stylesheet')) {
        fail('预览资源加载失败：' + (target.src || target.href));
      }
      return;
    }
    fail(event.message || '预览脚本运行失败');
  }, true);
  window.addEventListener('unhandledrejection', event => fail(String(event.reason)));
  post({type:'atoms-preview-state',state:'loading'});
  let ready = false;
  const mounted = () => {
    const root = document.getElementById('root') || document.getElementById('app');
    if (root) return !!root.childElementCount || !!root.textContent.trim();
    return document.readyState !== 'loading' && !!document.body &&
      (!!document.body.innerText.trim() || !!document.body.querySelector('canvas,svg,img,video,input,button,iframe'));
  };
  const check = () => {
    if (ready || !mounted()) return;
    ready = true;
    clearTimeout(timeout);
    observer.disconnect();
    // A mounted app can still have real script errors. Readiness only recovers
    // a slow-mount warning; the parent retains genuine runtime failures.
    post({type:'atoms-preview-state',state:'ready',fatal});
  };
  const observer = new MutationObserver(check);
  observer.observe(document.documentElement, {childList:true,subtree:true,characterData:true});
  const timeout = setTimeout(() => {
    check();
    if (!ready && !fatal) post({type:'atoms-preview-error',kind:'mount_timeout',message:'应用加载超时，仍在等待页面完成加载'});
  }, 30000);
  document.addEventListener('DOMContentLoaded', check, {once:true});
  window.addEventListener('load', check, {once:true});
  check();
})();
</script>"""


def inject_preview_diagnostics(html: str, scripts: str = PREVIEW_DIAGNOSTICS):
    """Install listeners before application scripts, including headless HTML."""
    if re.search(r'<head(?:\s[^>]*)?>', html, re.IGNORECASE):
        return re.sub(r'<head(?:\s[^>]*)?>', lambda match: match.group(0) + scripts,
                      html, count=1, flags=re.IGNORECASE)
    return scripts + html


def remove_preview_diagnostics(html: str):
    """Replace worker-injected diagnostics at the gateway during upgrades."""
    def remove(match):
        script = match.group(0)
        if 'data-atoms-preview-diagnostics' in script or (
                "const send = (level, message)" in script and "atoms-preview-console" in script):
            return ''
        return script
    return re.sub(r'<script\b[^>]*>.*?</script\s*>', remove, html, flags=re.DOTALL | re.IGNORECASE)
