import { useEffect, useState } from 'react'
import { ChevronLeft, ChevronRight, Download, RefreshCw } from 'lucide-react'
import { authFetch } from './auth'
import './ArtifactViewer.css'

export type ArtifactEntry = { path: string; name: string; size: number }
type Cell = string | { value: string; formula: boolean; number_format: string; expression?: string }
type Preview = { kind: 'pages' | 'table' | 'text' | 'html' | 'image' | 'archive'; name?: string; pages?: number; titles?: string[]; text?: string; sheets?: string[]; sheet?: number; total_rows?: number; total_columns?: number; rows?: Cell[][]; entries?: string[]; note?: string }
const columnName = (index: number) => { let text = ''; for (let n = index + 1; n; n = Math.floor((n - 1) / 26)) text = String.fromCharCode(65 + (n - 1) % 26) + text; return text }

export default function ArtifactViewer({ projectId, entries, onDownload }: { projectId: string; entries: ArtifactEntry[]; onDownload: (entry: ArtifactEntry) => void }) {
  const [selected, setSelected] = useState(entries[0]?.path || '')
  const [info, setInfo] = useState<Preview | null>(null)
  const [page, setPage] = useState(0)
  const [sheet, setSheet] = useState(0)
  const [rowOffset, setRowOffset] = useState(0)
  const [columnOffset, setColumnOffset] = useState(0)
  const [imageUrl, setImageUrl] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [mode, setMode] = useState<'default' | 'pages'>('default')
  const [revision, setRevision] = useState(0)
  const entry = entries.find(item => item.path === selected) || entries[0]
  useEffect(() => { setSelected(entries[0]?.path || '') }, [projectId])
  useEffect(() => { setPage(0); setSheet(0); setRowOffset(0); setColumnOffset(0); setInfo(null); setMode('default') }, [entry?.path])
  useEffect(() => {
    let disposed = false
    if (!entry) return
    setLoading(true); setError(''); setInfo(null)
    void (async () => {
      const response = await authFetch(`/projects/${projectId}/artifacts/preview?path=${encodeURIComponent(entry.path)}&mode=${mode}&sheet=${sheet}&offset=${rowOffset}&column_offset=${columnOffset}`)
      if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(data.detail || '预览读取失败') }
      const data = await response.json() as Preview
      if (!disposed) setInfo(data)
    })().catch(reason => { if (!disposed) setError((reason as Error).message) }).finally(() => { if (!disposed) setLoading(false) })
    return () => { disposed = true }
  }, [projectId, entry?.path, sheet, rowOffset, columnOffset, revision, mode])
  useEffect(() => {
    let disposed = false; let objectUrl = ''
    setImageUrl('')
    if (!entry || !info || !['pages', 'image'].includes(info.kind)) return
    setLoading(true)
    void (async () => {
      const endpoint = info.kind === 'pages' ? `page?path=${encodeURIComponent(entry.path)}&page=${page}` : `download?path=${encodeURIComponent(entry.path)}`
      const response = await authFetch(`/projects/${projectId}/artifacts/${endpoint}`)
      if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(data.detail || '页面读取失败') }
      const blob = await response.blob()
      const mime = info.kind === 'pages' ? 'image/png' : entry.path.endsWith('.svg') ? 'image/svg+xml' : `image/${entry.path.split('.').at(-1)?.replace('jpg', 'jpeg')}`
      objectUrl = URL.createObjectURL(new Blob([blob], { type: mime }))
      if (!disposed) setImageUrl(objectUrl)
    })().catch(reason => { if (!disposed) setError((reason as Error).message) }).finally(() => { if (!disposed) setLoading(false) })
    return () => { disposed = true; if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [projectId, entry?.path, info, page, revision])
  if (!entry) return <div className="artifact-empty">暂未生成成果文件</div>
  return <div className="artifact-viewer" data-testid="artifact-viewer">
    <div className="artifact-toolbar">
      <select aria-label="选择成果文件" value={entry.path} onChange={event => setSelected(event.target.value)}>{entries.map(item => <option key={item.path} value={item.path}>{item.name}</option>)}</select>
      {entry.path.endsWith('.xlsx') && <button onClick={() => { setMode(v => v === 'pages' ? 'default' : 'pages'); setPage(0) }}>{mode === 'pages' ? '表格数据' : '版式预览'}</button>}
      <button title="刷新预览" aria-label="刷新成果预览" onClick={() => setRevision(v => v + 1)}><RefreshCw size={16} /></button>
      <button className="artifact-download" onClick={() => onDownload(entry)}><Download size={16} />下载 {entry.name}</button>
    </div>
    {info?.kind === 'pages' && <div className="artifact-pagination"><button aria-label="上一页" disabled={page === 0} onClick={() => setPage(v => v - 1)}><ChevronLeft size={16} /></button><span>第 {page + 1} / {info.pages} 页</span><button aria-label="下一页" disabled={page + 1 >= (info.pages || 0)} onClick={() => setPage(v => v + 1)}><ChevronRight size={16} /></button><input aria-label="跳转页码" type="number" min={1} max={info.pages} value={page + 1} onChange={event => { const n = Number(event.target.value); if (Number.isInteger(n) && n >= 1 && n <= (info.pages || 0)) setPage(n - 1) }} /></div>}
    {info?.kind === 'table' && <div className="artifact-sheet-tabs">{info.sheets?.map((title, index) => <button key={index} className={index === sheet ? 'selected' : ''} onClick={() => { setSheet(index); setRowOffset(0); setColumnOffset(0) }}>{title}</button>)}</div>}
    {loading && <div className="artifact-loading" role="status">正在读取真实成果…</div>}
    {error && <div className="artifact-error" role="alert">{error}<button onClick={() => setRevision(v => v + 1)}>重试预览</button></div>}
    <div className="artifact-content">
      {imageUrl && <img data-testid="artifact-page" className="artifact-page" src={imageUrl} alt={info?.kind === 'pages' ? `${entry.name} 第${page + 1}页：${info.titles?.[page] || ''}` : entry.name} />}
      {info?.kind === 'html' && <iframe title={`${entry.name} 报告预览`} sandbox="" srcDoc={info.text} />}
      {info?.kind === 'text' && <article className="artifact-document">{info.text?.split('\n').map((line, index) => line.startsWith('# ') ? <h1 key={index}>{line.slice(2)}</h1> : line.startsWith('## ') ? <h2 key={index}>{line.slice(3)}</h2> : line.startsWith('### ') ? <h3 key={index}>{line.slice(4)}</h3> : <p key={index}>{line || '\u00a0'}</p>)}</article>}
      {info?.kind === 'archive' && <article className="artifact-document"><h2>文件包内容</h2>{info.entries?.map((name, index) => <p key={index}>{name}</p>)}</article>}
      {info?.kind === 'table' && <table className="artifact-table"><thead><tr><th></th>{Array.from({ length: Math.min(100, Math.max(0, (info.total_columns || 0) - columnOffset)) }, (_, index) => <th key={index}>{columnName(columnOffset + index)}</th>)}</tr></thead><tbody>{info.rows?.map((row, index) => <tr key={index}><th>{rowOffset + index + 1}</th>{row.map((cell, column) => <td key={column} className={typeof cell !== 'string' && cell.formula ? 'formula' : ''} title={typeof cell === 'string' ? cell : cell.expression || cell.value}>{typeof cell === 'string' ? cell : cell.value}</td>)}</tr>)}</tbody></table>}
    </div>
    {info?.kind === 'table' && <div className="artifact-table-footer"><span>{info.total_rows} 行 · {info.total_columns} 列 {info.note}</span><button disabled={rowOffset === 0} onClick={() => setRowOffset(v => Math.max(0, v - 100))}>上一批行</button><button disabled={rowOffset + 100 >= (info.total_rows || 0)} onClick={() => setRowOffset(v => v + 100)}>下一批行</button><button disabled={columnOffset === 0} onClick={() => setColumnOffset(v => Math.max(0, v - 100))}>前100列</button><button disabled={columnOffset + 100 >= (info.total_columns || 0)} onClick={() => setColumnOffset(v => v + 100)}>后100列</button></div>}
  </div>
}
