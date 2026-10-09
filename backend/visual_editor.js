(() => {
  if (window.__atomsVisualEditor) return;
  window.__atomsVisualEditor = true;
  let mode = 'none';
  const selections = new Map();
  const selectedTargets = new Map();
  const selectionBoxes = [];
  const edits = new Map();
  const send = (type, payload = {}) => parent.postMessage({ type, ...payload }, '*');
  const overlay = document.createElement('div');
  overlay.dataset.atomsEditor = 'true';
  Object.assign(overlay.style, { position: 'fixed', pointerEvents: 'none', zIndex: '2147483647', border: '2px solid #4267ff', background: '#4267ff12', display: 'none', boxSizing: 'border-box' });
  const label = document.createElement('span');
  Object.assign(label.style, { position: 'absolute', left: '-2px', top: '-24px', background: '#4267ff', color: 'white', padding: '2px 6px', font: '12px sans-serif', whiteSpace: 'nowrap' });
  overlay.append(label);
  const domPath = element => {
    const parts = [];
    let current = element;
    while (current && current.nodeType === 1) {
      const siblings = current.parentElement ? [...current.parentElement.children].filter(child => child.tagName === current.tagName) : [];
      parts.unshift(current.tagName.toLowerCase() + (current.id ? '#' + CSS.escape(current.id) : '') + (siblings.length > 1 ? ':nth-of-type(' + (siblings.indexOf(current) + 1) + ')' : ''));
      current = current.parentElement;
    }
    return parts.join(' > ');
  };
  const reference = element => {
    const fiberKey = Object.keys(element).find(key => key.startsWith('__reactFiber$'));
    let fiber = fiberKey ? element[fiberKey] : null;
    let source = null;
    let component = '';
    for (let depth = 0; fiber && depth < 15; depth++, fiber = fiber.return) {
      source ||= fiber._debugSource;
      component ||= typeof fiber.type === 'function' ? fiber.type.displayName || fiber.type.name || '' : '';
    }
    const path = domPath(element);
    return { value: element.tagName.toLowerCase(), domPath: path, code: element.outerHTML.slice(0, 6000), text: (element.textContent || '').slice(0, 4000), src_path: element.getAttribute('data-source') || source?.fileName || '', component, url: location.href.split('?')[0], parentCode: element.parentElement?.outerHTML.slice(0, 6000) || '', referenceKey: path, referenceNamespace: 'visual-editor-selection' };
  };
  const report = () => {
    const changes = [...edits.values()].filter(edit => edit.element.textContent !== edit.originalText).map(edit => ({ reference: edit.reference, oldText: edit.originalText, newText: (edit.element.textContent || '').slice(0, 4000) }));
    send('atoms-design-state', { mode, references: [...selections.values()], changes });
    drawSelections();
  };
  const drawSelections = () => {
    selectionBoxes.splice(0).forEach(box => box.remove());
    if (mode !== 'select') return;
    for (const element of selectedTargets.values()) {
      if (!element.isConnected) continue;
      const bounds = element.getBoundingClientRect();
      const box = document.createElement('div');
      box.dataset.atomsEditor = 'true';
      Object.assign(box.style, { position: 'fixed', pointerEvents: 'none', zIndex: '2147483646', border: '2px solid #4267ff', boxSizing: 'border-box', left: bounds.left + 'px', top: bounds.top + 'px', width: bounds.width + 'px', height: bounds.height + 'px' });
      document.body.append(box);
      selectionBoxes.push(box);
    }
  };
  const stopEditing = () => {
    for (const edit of edits.values()) {
      if (edit.editable === null) edit.element.removeAttribute('contenteditable');
      else edit.element.setAttribute('contenteditable', edit.editable);
    }
  };
  const discard = () => {
    for (const edit of edits.values()) edit.element.innerHTML = edit.originalHtml;
    stopEditing();
    edits.clear();
    mode = 'none';
    overlay.style.display = 'none';
    report();
  };
  const eligible = target => target instanceof HTMLElement && !target.closest('[data-atoms-editor]') && !['HTML', 'BODY', 'SCRIPT', 'STYLE', 'IFRAME', 'INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName);
  const beginEdit = element => {
    if (!eligible(element) || !element.textContent?.trim()) return;
    const path = domPath(element);
    if (!edits.has(path) && edits.size >= 10) { send('atoms-design-error', { message: '一次最多修改 10 个文本，请先保存或丢弃。' }); return; }
    if (!edits.has(path)) edits.set(path, { element, originalHtml: element.innerHTML, originalText: element.textContent, editable: element.getAttribute('contenteditable'), reference: reference(element) });
    element.setAttribute('contenteditable', 'plaintext-only');
    element.focus();
    mode = 'text';
    overlay.style.display = 'none';
    report();
  };
  document.addEventListener('mousemove', event => {
    if (mode === 'none' || !eligible(event.target)) { overlay.style.display = 'none'; return; }
    if (!overlay.isConnected) document.body.append(overlay);
    const bounds = event.target.getBoundingClientRect();
    Object.assign(overlay.style, { display: 'block', left: bounds.left + 'px', top: bounds.top + 'px', width: bounds.width + 'px', height: bounds.height + 'px' });
    label.textContent = event.target.tagName.toLowerCase();
  }, true);
  document.addEventListener('click', event => {
    if (mode === 'none' || !eligible(event.target)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (mode === 'text') { beginEdit(event.target); return; }
    if (!event.ctrlKey && !event.metaKey) { selections.clear(); selectedTargets.clear(); }
    const path = domPath(event.target);
    if (selections.has(path)) { selections.delete(path); selectedTargets.delete(path); }
    else if (selections.size < 10) { selections.set(path, reference(event.target)); selectedTargets.set(path, event.target); }
    report();
  }, true);
  document.addEventListener('dblclick', event => {
    if (!eligible(event.target)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    beginEdit(event.target);
  }, true);
  document.addEventListener('input', event => {
    if (mode === 'text' && [...edits.values()].some(edit => edit.element === event.target)) report();
  }, true);
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && mode !== 'none') { event.preventDefault(); discard(); }
  }, true);
  window.addEventListener('message', event => {
    if (event.source !== parent || event.data?.type !== 'atoms-design-command') return;
    const command = event.data;
    if (command.action === 'mode' && ['none', 'select', 'text'].includes(command.mode)) {
      mode = command.mode;
      overlay.style.display = 'none';
      report();
    } else if (command.action === 'discard') discard();
    else if (command.action === 'clear') { discard(); selections.clear(); selectedTargets.clear(); report(); }
    else if (command.action === 'remove') { selections.delete(command.domPath); selectedTargets.delete(command.domPath); report(); }
    else if (command.action === 'save') { stopEditing(); report(); }
  });
  window.addEventListener('scroll', drawSelections, true);
  window.addEventListener('resize', drawSelections);
  send('atoms-design-ready');
})();
