import { useEffect, useRef, useState, type ClipboardEvent } from 'react'
import { File, X } from 'lucide-react'
import { authFetch } from './auth'

export default function FileMentionInput({ projectId, draft, setDraft, references, setReferences, onSubmit, onPaste, onError, placeholder }: { projectId: string; draft: string; setDraft: (value: string) => void; references: string[]; setReferences: (paths: string[]) => void; onSubmit: () => void; onPaste: (event: ClipboardEvent<HTMLTextAreaElement>) => void; onError: (message: string) => void; placeholder: string }) {
  const input = useRef<HTMLTextAreaElement>(null)
  const [mention, setMention] = useState<{ start: number; end: number; query: string } | null>(null)
  const [files, setFiles] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [selected, setSelected] = useState(0)
  useEffect(() => { setFiles([]); setMention(null) }, [projectId])
  useEffect(() => {
    if (!mention) return
    let disposed = false
    const controller = new AbortController()
    setLoading(true)
    void authFetch(`/projects/${projectId}/files`, { signal: controller.signal }).then(async response => {
      if (!response.ok) throw new Error('无法获取项目文件')
      return await response.json() as { files: string[] }
    }).then(result => { if (!disposed) setFiles(result.files) }).catch(error => { if (!disposed) onError(error.message) }).finally(() => { if (!disposed) setLoading(false) })
    return () => { disposed = true; controller.abort() }
  }, [projectId, !!mention])
  const matches = files.filter(file => !references.includes(file) && file.toLowerCase().includes((mention?.query || '').toLowerCase())).slice(0, 30)
  const detectMention = () => {
    const element = input.current
    if (!element) return
    const position = element.selectionStart
    const match = /(?:^|\s)@([^\s@]*)$/.exec(element.value.slice(0, position))
    setMention(match ? { start: position - match[1].length - 1, end: position, query: match[1] } : null)
    setSelected(0)
  }
  const choose = (path: string) => {
    if (!mention) return
    if (references.length >= 10) { onError('最多引用 10 个文件'); return }
    setReferences([...references, path])
    setDraft(draft.slice(0, mention.start) + draft.slice(mention.end))
    const position = mention.start
    setMention(null)
    requestAnimationFrame(() => { input.current?.focus(); input.current?.setSelectionRange(position, position) })
  }
  return <div className="build-file-mention-input">
    {!!references.length && <div className="build-file-reference-chips">{references.map(path => <span key={path} title={path}><File size={13} />{path}<button type="button" aria-label={`移除引用 ${path}`} onClick={() => setReferences(references.filter(item => item !== path))}><X size={12} /></button></span>)}</div>}
    {mention && <div className="build-file-mention-menu" role="listbox" aria-label="引用项目文件"><strong>项目文件</strong>{loading ? <p>正在读取文件…</p> : matches.length ? matches.map((path, index) => <button type="button" role="option" aria-selected={index === selected} className={index === selected ? 'active' : ''} key={path} onMouseDown={event => event.preventDefault()} onClick={() => choose(path)}><File size={14} /><span>{path}</span></button>) : <p>没有匹配文件</p>}</div>}
    <textarea ref={input} value={draft} placeholder={placeholder} onChange={event => { setDraft(event.target.value); detectMention() }} onClick={detectMention} onKeyUp={event => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) detectMention() }} onBlur={() => setMention(null)} onPaste={onPaste} onKeyDown={event => {
      if (event.nativeEvent.isComposing) return
      if (mention) {
        if (event.key === 'Escape') { event.preventDefault(); setMention(null); return }
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); setSelected(index => Math.max(0, Math.min(matches.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)))); return }
        if (event.key === 'Enter' || event.key === 'Tab') { event.preventDefault(); if (!loading && matches[selected]) choose(matches[selected]); return }
      }
      if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); onSubmit() }
    }} />
  </div>
}
