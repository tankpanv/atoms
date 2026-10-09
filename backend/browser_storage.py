"""Storage compatibility for opaque sandbox documents; no platform credentials."""

STORAGE_SHIM = r"""<script>
(() => {
  const match = location.pathname.match(/^\/api\/(?:runtime|preview|public)\/([^/]+)/);
  if (!match) return;
  const published = location.pathname.startsWith('/api/public/');
  const sessionId = new URLSearchParams(location.search).get('__atoms_session') || 'preview-default';
  const endpoint = '/api/storage/' + encodeURIComponent(match[1]) + '?session=' + encodeURIComponent(sessionId);
  let epoch = null;
  const exchange = (method, payload) => {
    const request = new XMLHttpRequest();
    request.open(method, endpoint, false);
    if (payload) request.setRequestHeader('Content-Type', 'text/plain');
    request.send(payload ? JSON.stringify({...payload, epoch, session:sessionId}) : null);
    if (request.status !== 200) throw new Error('Preview storage failed: ' + request.status);
    const result = JSON.parse(request.responseText);
    if (result.epoch) epoch = result.epoch;
    return result;
  };
  let saved = Object.create(null);
  let restoredSession = {};
  try { if (!published) { const state=exchange('GET'); Object.assign(saved,state.data || {}); restoredSession=state.session || {}; } }
  catch (error) { console.warn(error); }
  // A sandbox has an opaque origin and native sessionStorage may be denied.
  // window.name survives reload in this browsing context; scope it to this
  // preview route so another project cannot inherit the application's session.
  const sessionScope = location.pathname.match(/^\/api\/(?:runtime|preview|public)\/[^/]+/)[0];
  let sessionValues = Object.create(null);
  if (!published) Object.assign(sessionValues, restoredSession);
  let bridge = '';
  try {
    const previous = JSON.parse(window.name || '{}').atomsSession;
    if (published && previous?.scope === sessionScope) {
      Object.assign(sessionValues, previous.values);
      if (published) { Object.assign(saved, previous.local || {}); bridge = previous.bridge || ''; }
    }
  } catch (_) {}
  const saveSession = () => {
    const state = {scope: sessionScope, values: sessionValues, ...(published ? {local:saved, bridge} : {})};
    window.name = JSON.stringify({atomsSession: state});
    if (published && bridge && parent !== window) parent.postMessage({type:'atoms-public-storage', ...state}, '*');
  };
  const makeStorage = (values, persistent) => {
    const api = {
      get length() { return Object.keys(values).length; },
      key(index) { return Object.keys(values)[Number(index)] ?? null; },
      getItem(key) { const name = String(key); return Object.prototype.hasOwnProperty.call(values, name) ? values[name] : null; },
      setItem(key, value) {
        const name = String(key), text = String(value);
        if (!published) exchange('POST', {operation:'set',area:persistent ? 'local' : 'session',key:name,value:text});
        values[name] = text;
        if (!persistent || published) saveSession();
      },
      removeItem(key) {
        const name = String(key);
        if (!published) exchange('POST', {operation:'remove',area:persistent ? 'local' : 'session',key:name});
        delete values[name];
        if (!persistent || published) saveSession();
      },
      clear() {
        if (!published) exchange('POST', {operation:'clear',area:persistent ? 'local' : 'session'});
        for (const name of Object.keys(values)) delete values[name];
        if (!persistent || published) saveSession();
      },
    };
    for (const name of Object.keys(api)) Object.defineProperty(api, name, {enumerable: false});
    return new Proxy(api, {
      get(target, property, receiver) {
        return typeof property === 'string' && !(property in target) ? values[property] : Reflect.get(target, property, receiver);
      },
      set(target, property, value) {
        if (typeof property !== 'string' || property in target) return false;
        target.setItem(property, value);
        return true;
      },
      deleteProperty(target, property) {
        if (typeof property !== 'string' || property in target) return false;
        target.removeItem(property);
        return true;
      },
      ownKeys(target) { return [...Reflect.ownKeys(target), ...Object.keys(values).filter(name => !(name in target))]; },
      getOwnPropertyDescriptor(target, property) {
        if (typeof property === 'string' && Object.prototype.hasOwnProperty.call(values, property)) {
          return {configurable: true, enumerable: true, writable: true, value: values[property]};
        }
        return Reflect.getOwnPropertyDescriptor(target, property);
      },
    });
  };
  Object.defineProperty(window, 'localStorage', {configurable: true, value: makeStorage(saved, true)});
  Object.defineProperty(window, 'sessionStorage', {configurable: true, value: makeStorage(sessionValues, false)});
  if (!published) {
    // Keep the same tab's session namespace through internal navigation.
    const sessionURL = input => {
      if (input == null) return input;
      const url=new URL(String(input),location.href);
      if (url.origin===location.origin && url.pathname.startsWith(sessionScope+'/')) url.searchParams.set('__atoms_session',sessionId);
      return url.href;
    };
    for (const name of ['pushState','replaceState']) {
      const original=history[name].bind(history);
      history[name]=(state,unused,url)=>original(state,unused,sessionURL(url));
    }
    document.addEventListener('click',event=>{
      const anchor=event.target instanceof Element ? event.target.closest('a[href]') : null;
      if (anchor && !anchor.hasAttribute('download') && (!anchor.target || anchor.target==='_self')) anchor.href=sessionURL(anchor.href);
    },true);
  }
  if (published) {
    // Sec-Fetch-Dest is absent on non-secure LAN URLs. Preserve an explicit
    // frame marker through SPA navigation so a deep reload cannot nest shells.
    const frameURL = input => {
      if (input == null) return input;
      const url = new URL(String(input), location.href);
      if (url.origin === location.origin && (url.pathname === sessionScope || url.pathname.startsWith(sessionScope + '/'))) url.searchParams.set('__atoms_frame', '1');
      return url.href;
    };
    for (const name of ['pushState', 'replaceState']) {
      const original = history[name].bind(history);
      history[name] = (state, unused, url) => original(state, unused, frameURL(url));
    }
    document.addEventListener('click', event => {
      const anchor = event.target instanceof Element ? event.target.closest('a[href]') : null;
      if (anchor && !anchor.hasAttribute('download') && (!anchor.target || anchor.target === '_self')) anchor.href = frameURL(anchor.href);
    }, true);
  }
})();
</script>"""
