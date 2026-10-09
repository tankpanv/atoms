"""Storage compatibility for opaque sandbox documents; no platform credentials."""

STORAGE_SHIM = r"""<script>
(() => {
  const match = location.pathname.match(/^\/api\/(?:runtime|preview|public)\/([^/]+)/);
  if (!match) return;
  const published = location.pathname.startsWith('/api/public/');
  const endpoint = '/api/storage/' + encodeURIComponent(match[1]);
  const exchange = (method, payload) => {
    const request = new XMLHttpRequest();
    request.open(method, endpoint, false);
    if (payload) request.setRequestHeader('Content-Type', 'text/plain');
    request.send(payload ? JSON.stringify(payload) : null);
    if (request.status !== 200) throw new Error('Preview storage failed: ' + request.status);
    return JSON.parse(request.responseText);
  };
  let saved = Object.create(null);
  try { if (!published) Object.assign(saved, exchange('GET').data || {}); }
  catch (error) { console.warn(error); }
  // A sandbox has an opaque origin and native sessionStorage may be denied.
  // window.name survives reload in this browsing context; scope it to this
  // preview route so another project cannot inherit the application's session.
  const sessionScope = location.pathname.match(/^\/api\/(?:runtime|preview|public)\/[^/]+/)[0];
  let sessionValues = Object.create(null);
  let bridge = '';
  try {
    const previous = JSON.parse(window.name || '{}').atomsSession;
    if (previous?.scope === sessionScope) {
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
        if (persistent && !published) exchange('POST', {operation: 'set', key: name, value: text});
        values[name] = text;
        if (!persistent || published) saveSession();
      },
      removeItem(key) {
        const name = String(key);
        if (persistent && !published) exchange('POST', {operation: 'remove', key: name});
        delete values[name];
        if (!persistent || published) saveSession();
      },
      clear() {
        if (persistent && !published) exchange('POST', {operation: 'clear'});
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
