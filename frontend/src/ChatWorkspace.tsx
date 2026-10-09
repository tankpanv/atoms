import ArtifactViewer, { type ArtifactEntry } from './ArtifactViewer'
import { lazy, Suspense, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  ArrowUp, ChevronDown, ChevronLeft, ChevronRight, ChevronsLeft, CircleCheck,
  Code2, Download, ExternalLink, File, FileCode2, FilePlus2, Folder, FolderOpen,
  History, Home, Laptop, ListTodo, MoreHorizontal, NotebookPen, PanelLeftClose, PanelLeftOpen,
  MousePointer2, Pin, PinOff, Play, Plus, RefreshCw, Save, Search, Smartphone, Square,
  TerminalSquare, Trash2, Upload, X,
} from 'lucide-react'
import { formatNumber } from './numberFormat'
import { authFetch } from './auth'
import { documentAccept, mediaAccept, readDocumentFiles, readMediaFiles, type DocumentAttachment, type MediaAttachment, type UploadAttachment } from './media'
import './chat-workspace.css'
import FileMentionInput from './FileMentionInput'
import PendingAttachments from './PendingAttachments'
import VoiceInput from './VoiceInput'
import type { Expert } from './ExpertLibrary'

const ProjectCodeEditor = lazy(() => import('./ProjectCodeEditor'))

type SavedAttachment = { id: string; filename: string; mime_type: string; kind: 'image' | 'document'; size_bytes: number }
export type ElementReference = { value: string; domPath: string; code: string; text: string; src_path: string; component: string; url: string; parentCode: string; referenceKey: string; referenceNamespace: 'visual-editor-selection' }
export type MessageOptions = { content?: string; elementReferences?: ElementReference[]; fileReferences?: string[] }
type VisualTextChange = { reference: ElementReference; oldText: string; newText: string }
export type ChatProject = { expert_ids?: string[]; id: string; title: string; status: string; preview_html: string; published: boolean; model: string; messages: { id: string; role: string; agent: string | null; content: string; created_at: string; expert_ids?: string[]; attachments?: SavedAttachment[]; element_references?: ElementReference[]; file_references?: string[] }[] }
type Step = { id: number; kind: string; label: string; detail: string; created_at: string; tool_name?: string; tool_input?: string; tool_output?: string; token_usage?: { total_tokens?: number } }
type Job = { state?: string; id: string; prompt: string; status: string; error: string; stop_requested: boolean; created_at: string; updated_at: string; steps: Step[] }
type DeliveryBudget = { phase: string; complexity: string; task_tokens: number; token_limit: number; task_iterations: number; iteration_limit: number; task_calls: number; repair_tokens: number; repair_calls: number; repair_iterations: number; repair_token_limit?: number; repair_iteration_limit?: number; configured_iteration_limit?: number; total_tokens: number }
type SessionInfo = { application_type?: string; delivery_budget?: DeliveryBudget; demo?: { ready: boolean; kind: string; report: string; deliverables?: {path:string}[] }; available: boolean; completed?: boolean; compactions?: number; prunes?: number; pruned_tokens?: number; local_read_hits?: number; estimated_context_tokens?: number; known_files?: number }
type PreviewResult = { url: string; mode: 'live' | 'static' | 'none'; output: string }
type RestoredPreview = { id: string; runtime?: PreviewResult; error?: string }
type Version = { version: number; summary: string; created_at: string }
type Command = { id: number; command: string; exit_code: number; output: string; created_at: string }
type DirectoryEntry = { name: string; path: string; workspace_id?: string; kind: 'file' | 'directory'; size: number | null; modified_at: string }
type FilePreview = { name: string; mime: string; text?: string; url?: string }
type SearchMatch = { path: string; line: number; text: string }
type ConsoleEntry = { id: string | number; level: string; message: string; time: string }
type WorkspaceTool = 'terminal' | 'planner' | 'browser' | 'notebook'
type Tab = 'preview' | 'editor' | 'terminal' | 'planner' | 'notebook' | 'files'
const workspaceTools: { id: WorkspaceTool; label: string }[] = [
  { id: 'terminal', label: '终端' }, { id: 'planner', label: '计划器' },
  { id: 'browser', label: '浏览器' }, { id: 'notebook', label: '笔记本' },
]
const toolIcons = { terminal: TerminalSquare, planner: ListTodo, browser: Laptop, notebook: NotebookPen }

const encodedPath = (path: string) => path.split('/').map(encodeURIComponent).join('/')
const displaySize = (bytes: number | null) => bytes === null ? '—' : bytes < 1024 ? `${bytes} B` : bytes < 1_048_576 ? `${formatNumber(bytes / 1024)} KB` : `${formatNumber(bytes / 1_048_576)} MB`
const previewOrigin = import.meta.env.VITE_PREVIEW_ORIGIN || `${window.location.protocol}//${window.location.hostname}:${import.meta.env.VITE_PREVIEW_PORT || '8002'}`
const isolatedPreviewUrl = (path: string) => {
  if (!path) return ''
  const target = new URL(path, previewOrigin)
  if (target.origin === window.location.origin || target.origin !== new URL(previewOrigin).origin ||
      !/^\/api\/(runtime|preview)\/[^/]+\//.test(target.pathname)) {
    throw new Error('预览地址未使用独立的隔离域名或端口')
  }
  return target.href
}
const stateLabels: Record<string, string> = {
  UNDERSTAND: '理解需求', EXPLORE: '探索代码', PLAN: '制定计划', IMPLEMENT: '修改代码',
  STABILIZE: '准备演示', PREVIEW_READY: '演示版本就绪', TEST: '构建与测试', REVIEW: '复核改动', COMPLETE: '任务完成', FAILED: '任务失败', STOPPED: '任务已停止',
}
const redactModelNames = (value: string) => value
  .replace(/图片识别来源：[^。\n]+[。]?/gi, '图片识别完成。')
  .replace(/\b(?:openai|anthropic|deepseek|google|meta-llama|qwen|mistral|x-ai|cohere|perplexity|microsoft|amazon|nvidia|bytedance|minimax|moonshotai|z-ai|01-ai|ai21|together|fireworks|inflection|nousresearch|gryphe|undi95|huggingface|openrouter)\/[\w.-]+(?:\:[\w.-]+)?\b/gi, '')
const stepLabel = (step: Step) => redactModelNames(step.kind === 'state' ? stateLabels[step.label] || step.label : step.label)
const toolFailure = (step: Step): { detail?: string; recovery?: string; executed?: boolean } | undefined => {
  try {
    const value = JSON.parse(step.tool_output || '{}') as { ok?: boolean; detail?: string; recovery?: string; executed?: boolean }
    return value.ok === false ? value : undefined
  } catch { return undefined }
}
const stepDetail = (step: Step) => {
  const failure = toolFailure(step)
  if (failure) {
    const recovery = !['write_files', 'write_file'].includes(step.tool_name || '') && failure.recovery?.includes('write_files') ? '' : failure.recovery
    return `${failure.executed === false ? '未执行：' : ''}${failure.detail || '工具调用未成功'}${recovery ? `\n${recovery}` : ''}`
  }
  return redactModelNames(step.detail)
}
const filePathForStep = (step: Step) => {
  if (step.kind !== 'tool' || !['read_file', 'write_file', 'replace_in_file', 'apply_patch'].includes(step.tool_name || '') || step.tool_output?.startsWith('工具错误:') || toolFailure(step)) return ''
  try { return (JSON.parse(step.tool_input || '{}') as { path?: string }).path || '' } catch { return '' }
}
const editedFile = (step: Step) => {
  if (step.kind !== 'tool' || !['write_file', 'replace_in_file', 'apply_patch'].includes(step.tool_name || '') || step.tool_output?.startsWith('工具错误:') || toolFailure(step)) return ''
  try {
    const input = JSON.parse(step.tool_input || '{}') as { path?: string }
    if (input.path) return input.path
  } catch {}
  return step.detail.split('\n')[0]
}
const editedFiles = (job?: Job) => [...new Set((job?.steps || []).map(editedFile).filter(Boolean))]

function jobDuration(job: Job) {
  const elapsed = Date.parse(job.updated_at) - Date.parse(job.created_at)
  if (!Number.isFinite(elapsed) || elapsed < 0) return '耗时未知'
  const totalSeconds = Math.round(elapsed / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor(totalSeconds % 3600 / 60)
  const seconds = totalSeconds % 60
  return `耗时 ${hours ? `${hours} 小时 ` : ''}${hours || minutes ? `${minutes} 分 ` : ''}${seconds} 秒`
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await authFetch(path, options)
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { detail?: string }
    throw new Error(body.detail || `请求失败 (${response.status})`)
  }
  return response.json() as Promise<T>
}

function MessageText({ content, files, onOpenFile }: { content: string; files: string[]; onOpenFile: (path: string) => void }) {
  const [showLongCode, setShowLongCode] = useState(false)
  const codeLike = content.length > 800 && (content.includes('```') || (content.match(/[{};]/g) || []).length > 24)
  const fileLinks = !!files.length && <div className="build-message-edited-files"><span>修改文件</span>{files.map(path => <button key={path} onClick={() => onOpenFile(path)} title={`在编辑器中打开 ${path}`}>{path}</button>)}</div>
  if (codeLike && !showLongCode) return <div className="build-message-code-fold"><div className="build-message-code-fold-info"><span>已折叠较长的代码内容（{content.length.toLocaleString()} 字符）</span>{fileLinks}</div><button onClick={() => setShowLongCode(true)}>展开内容</button></div>
  const inline = (value: string) => value.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, index) =>
    part.startsWith('**') && part.endsWith('**') ? <strong key={index}>{part.slice(2, -2)}</strong> :
      part.startsWith('`') && part.endsWith('`') ? <code key={index}>{part.slice(1, -1)}</code> : part)
  const lines = content.split('\n')
  const blocks: ReactNode[] = []
  let lineIndex = 0
  while (lineIndex < lines.length) {
    const line = lines[lineIndex]
    if (!line.trim()) { lineIndex++; continue }
    if (/^演示交付 TODO(?:\s|（|\()/.test(line)) {
      const title = line
      lineIndex++
      const todoLines: string[] = []
      while (lineIndex < lines.length) todoLines.push(lines[lineIndex++])
      const todoBlocks: ReactNode[] = []
      let todoIndex = 0
      while (todoIndex < todoLines.length) {
        if (!todoLines[todoIndex].trim()) { todoIndex++; continue }
        if (/^\s*[-*] /.test(todoLines[todoIndex])) {
          const items: string[] = []
          while (todoIndex < todoLines.length && /^\s*[-*] /.test(todoLines[todoIndex])) items.push(todoLines[todoIndex++].replace(/^\s*[-*] /, ''))
          todoBlocks.push(<ul key={todoBlocks.length}>{items.map((item, index) => <li key={index}>{inline(item)}</li>)}</ul>)
        } else {
          const paragraph = [todoLines[todoIndex++]]
          while (todoIndex < todoLines.length && todoLines[todoIndex].trim() && !/^\s*[-*] /.test(todoLines[todoIndex])) paragraph.push(todoLines[todoIndex++])
          todoBlocks.push(<p key={todoBlocks.length}>{paragraph.map((text, index) => <span key={index}>{index > 0 && <br />}{inline(text)}</span>)}</p>)
        }
      }
      blocks.push(<details className="build-message-todo-fold" key={blocks.length}><summary>{title} <span>点击展开</span></summary><div>{todoBlocks}</div></details>)
      continue
    }
    if (line.startsWith('```')) {
      const code: string[] = []
      lineIndex++
      while (lineIndex < lines.length && !lines[lineIndex].startsWith('```')) code.push(lines[lineIndex++])
      lineIndex++
      blocks.push(<pre key={blocks.length}>{code.join('\n')}</pre>)
      continue
    }
    if (/^#{1,4} /.test(line)) { blocks.push(<h3 key={blocks.length}>{inline(line.replace(/^#{1,4} /, ''))}</h3>); lineIndex++; continue }
    if (/^\s*[-*] /.test(line)) {
      const items: string[] = []
      while (lineIndex < lines.length && /^\s*[-*] /.test(lines[lineIndex])) items.push(lines[lineIndex++].replace(/^\s*[-*] /, ''))
      blocks.push(<ul key={blocks.length}>{items.map((item, index) => <li key={index}>{inline(item)}</li>)}</ul>)
      continue
    }
    if (line.startsWith('|') && lines[lineIndex + 1]?.includes('---')) {
      const rows: string[][] = []
      while (lineIndex < lines.length && lines[lineIndex].startsWith('|')) rows.push(lines[lineIndex++].split('|').slice(1, -1).map(cell => cell.trim()))
      blocks.push(<div className="build-message-table" key={blocks.length}><table><thead><tr>{rows[0].map((cell, index) => <th key={index}>{inline(cell)}</th>)}</tr></thead><tbody>{rows.slice(2).map((row, index) => <tr key={index}>{row.map((cell, cellIndex) => <td key={cellIndex}>{inline(cell)}</td>)}</tr>)}</tbody></table></div>)
      continue
    }
    const paragraph = [line]
    lineIndex++
    while (lineIndex < lines.length && lines[lineIndex].trim() && !/^(#{1,4} |\s*[-*] |\|)/.test(lines[lineIndex])) paragraph.push(lines[lineIndex++])
    blocks.push(<p key={blocks.length}>{paragraph.map((text, index) => <span key={index}>{index > 0 && <br />}{inline(text)}</span>)}</p>)
  }
  return <>{codeLike && fileLinks}<div className="build-message-text">{blocks}</div>{codeLike && <button className="build-message-code-collapse" onClick={() => setShowLongCode(false)}>收起代码内容</button>}</>
}

function SavedMedia({ projectId, attachment }: { projectId: string; attachment: SavedAttachment }) {
  const [url, setUrl] = useState('')
  useEffect(() => {
    let active = true
    let objectUrl = ''
    void authFetch(`/projects/${projectId}/attachments/${attachment.id}`).then(response => response.ok ? response.blob() : Promise.reject(new Error('附件加载失败'))).then(blob => {
      objectUrl = URL.createObjectURL(blob)
      if (active) setUrl(objectUrl)
      else URL.revokeObjectURL(objectUrl)
    }).catch(() => {})
    return () => { active = false; if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [projectId, attachment.id])
  return <a className="build-message-media" href={url || undefined} target="_blank" rel="noreferrer" title={attachment.filename}>
    {url && attachment.kind === 'image' ? <img src={url} alt={attachment.filename} /> : <span>▣</span>}
    <small>{attachment.filename}</small>
  </a>
}

function FileTree({ files, activeFile, search, onSelect }: { files: string[]; activeFile: string; search: string; onSelect: (file: string) => void }) {
  const [closed, setClosed] = useState<Record<string, boolean>>({})
  const listRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const ancestors = activeFile.split('/').slice(0, -1).map((_, index, parts) => parts.slice(0, index + 1).join('/'))
    setClosed(current => ({ ...current, ...Object.fromEntries(ancestors.map(path => [path, false])) }))
    const frame = requestAnimationFrame(() => listRef.current?.querySelector('.build-tree-row.active')?.scrollIntoView({ block: 'nearest' }))
    return () => cancelAnimationFrame(frame)
  }, [activeFile])
  const matching = files.filter(file => file.toLowerCase().includes(search.toLowerCase()))
  const entries = [...new Set(matching.flatMap(file => file.split('/').map((_, index) => file.split('/').slice(0, index + 1).join('/'))))]
  const isFolder = (entry: string) => matching.some(file => file.startsWith(`${entry}/`))
  const renderFolder = (parent: string, depth: number): ReactNode[] => entries
    .filter(entry => entry.lastIndexOf('/') < 0 ? parent === '' : entry.slice(0, entry.lastIndexOf('/')) === parent)
    .sort((left, right) => isFolder(left) === isFolder(right) ? left.localeCompare(right) : isFolder(left) ? -1 : 1)
    .flatMap(entry => isFolder(entry) ? [
      <button key={entry} className="build-tree-row folder" style={{ paddingLeft: 9 + depth * 19 }} onClick={() => setClosed(state => ({ ...state, [entry]: !state[entry] }))}>
        {closed[entry] ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
        {closed[entry] ? <Folder size={15} /> : <FolderOpen size={15} />}
        <span>{entry.split('/').at(-1)}</span>
      </button>,
      ...(closed[entry] ? [] : renderFolder(entry, depth + 1)),
    ] : [<button key={entry} className={`build-tree-row file ${entry === activeFile ? 'active' : ''}`} style={{ paddingLeft: 29 + depth * 19 }} title={entry} onClick={() => onSelect(entry)}><FileCode2 size={14} /><span>{entry.split('/').at(-1)}</span></button>])
  return <div className="build-file-list" ref={listRef}>
    {renderFolder('', 0)}
    {!matching.length && <p className="build-file-empty">没有匹配的文件</p>}
  </div>
}

export default function ChatWorkspace({ projectMenu, toolsControl, tierControl, expertControl, onChooseExperts, experts, project, setProject, modelControl, draft, setDraft, sendMessage, busy, publishBusy, onHome, onRename, onPublish, onUnpublish, onShare }: {
  projectMenu: ReactNode; toolsControl: ReactNode; tierControl: ReactNode; expertControl: ReactNode; onChooseExperts: () => void; experts: Expert[];
  project: ChatProject; setProject: (project: ChatProject) => void; modelControl: ReactNode; draft: string; setDraft: (value: string) => void; sendMessage: (attachments: UploadAttachment[], options?: MessageOptions) => Promise<boolean>; busy: boolean;
  publishBusy: boolean;
  onHome: () => void; onRename: () => void; onDelete: () => void; onPublish: () => void; onUnpublish: () => void; onShare: () => void;
}) {
  const [jobs, setJobs] = useState<Job[]>([])
  const [sessionInfo, setSessionInfo] = useState<SessionInfo>({ available: false })
  const [sessionProjectId, setSessionProjectId] = useState<string | null>(null)
  const [artifactEntries, setArtifactEntries] = useState<ArtifactEntry[]>([])
  const [showArtifacts, setShowArtifacts] = useState(false)
  const [versions, setVersions] = useState<Version[]>([])
  const [commands, setCommands] = useState<Command[]>([])
  const [directoryEntries, setDirectoryEntries] = useState<DirectoryEntry[]>([])
  const [currentDirectory, setCurrentDirectory] = useState('')
  const [directoryWorkspaceId, setDirectoryWorkspaceId] = useState<string | null>(null)
  const [directoryWorkspaceTitle, setDirectoryWorkspaceTitle] = useState('')
  const [filePreview, setFilePreview] = useState<FilePreview | null>(null)
  const [pendingDelete, setPendingDelete] = useState<{ kind: 'project' | 'entry'; name: string; workspaceId?: string; path?: string } | null>(null)
  const [designMode, setDesignMode] = useState<'none' | 'select' | 'text'>('none')
  const [designToolsCollapsed, setDesignToolsCollapsed] = useState(false)
  const [elementReferences, setElementReferences] = useState<ElementReference[]>([])
  const [fileReferences, setFileReferences] = useState<string[]>([])
  useEffect(() => { setFileReferences([]) }, [project.id])
  const [visualTextChanges, setVisualTextChanges] = useState<VisualTextChange[]>([])
  const [pinnedTools, setPinnedTools] = useState<WorkspaceTool[]>([])
  const [notebookText, setNotebookText] = useState('')
  const [terminalInput, setTerminalInput] = useState('')
  const [media, setMedia] = useState<MediaAttachment[]>([])
  const [documents, setDocuments] = useState<DocumentAttachment[]>([])
  const mediaRef = useRef<HTMLInputElement>(null)
  const documentRef = useRef<HTMLInputElement>(null)
  const projectFileUploadRef = useRef<HTMLInputElement>(null)
  const projectFolderUploadRef = useRef<HTMLInputElement>(null)
  const addMedia = async (selected: File[]) => {
    try { const next = await readMediaFiles(selected, media); setMedia(previous => [...previous, ...next]); setMenu(null) }
    catch (error) { setNotice((error as Error).message) }
  }
  const addDocuments = async (selected: File[]) => {
    try { const next = await readDocumentFiles(selected, documents, media); setDocuments(previous => [...previous, ...next]); setMenu(null) }
    catch (error) { setNotice((error as Error).message) }
  }
  const submitMessage = async () => {
    if (visualTextChanges.length) { setNotice('请先保存或丢弃预览中的文本更改'); return }
    if (await sendMessage([...media, ...documents], { elementReferences, fileReferences })) { setMedia([]); setDocuments([]); setElementReferences([]); setFileReferences([]); designCommand({ action: 'clear' }) }
  }
  const designCommand = (command: Record<string, unknown>) => previewRef.current?.contentWindow?.postMessage({ type: 'atoms-design-command', ...command }, '*')
  const activateDesign = (mode: 'none' | 'select' | 'text') => {
    if (visualTextChanges.length && mode !== 'text') { setNotice('请先保存或丢弃文本更改'); return }
    designCommand({ action: 'mode', mode })
    setDesignMode(mode)
  }
  const saveVisualText = async () => {
    if (!visualTextChanges.length || busy || isBuilding) return
    const content = '仅应用下列更改：\n\n' + visualTextChanges.map(change => `将引用 ${change.reference.value} 元素（${change.reference.domPath}）的文本从 ${JSON.stringify(change.oldText)} 更改为 ${JSON.stringify(change.newText)}。`).join('\n') + (draft.trim() ? `\n\n${draft.trim()}` : '')
    if (await sendMessage([], { content, elementReferences: visualTextChanges.map(change => change.reference) })) {
      designCommand({ action: 'clear' })
      setVisualTextChanges([]); setElementReferences([]); setDesignMode('none')
    }
  }
  const [files, setFiles] = useState<string[]>([])
  const [activeFile, setActiveFile] = useState('')
  const [fileText, setFileText] = useState('')
  const [savedText, setSavedText] = useState('')
  const [fileReadable, setFileReadable] = useState(false)
  const [tab, setTab] = useState<Tab>(project.status === 'running' || project.status === 'queued' ? 'editor' : 'preview')
  const [mobile, setMobile] = useState(false)
  const [chatOpen, setChatOpen] = useState(true)
  const [treeOpen, setTreeOpen] = useState(true)
  const [following, setFollowing] = useState(true)
  const pendingEditedFileRef = useRef<string | null>(null)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [menu, setMenu] = useState<'project' | 'more' | 'add' | null>(null)
  const moreMenuCloseTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const addMenuCloseTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [activityOpen, setActivityOpen] = useState<Record<string, boolean>>({})
  const [working, setWorking] = useState(false)
  const [notice, setNotice] = useState('')
  useEffect(() => {
    if (!notice) return
    const timer = setTimeout(() => setNotice(''), 10_000)
    return () => clearTimeout(timer)
  }, [notice])
  const [frameKey, setFrameKey] = useState(0)
  const [previewRevision, setPreviewRevision] = useState(0)
  const [restoredPreview, setRestoredPreview] = useState<RestoredPreview | null>(null)
  const syncRevision = useRef(0)
  const workspaceMutating = useRef(false)
  const refreshWorkspace = useRef<() => void>(() => {})
  const [previewUrl, setPreviewUrl] = useState('')
  const [previewMode, setPreviewMode] = useState<'live' | 'static' | 'none'>('none')
  const [previewStarting, setPreviewStarting] = useState(false)
  const [runtimeOutput, setRuntimeOutput] = useState('')
  const [previewError, setPreviewError] = useState('')
  const [previewErrorDismissed, setPreviewErrorDismissed] = useState(false)
  const [consoleOpen, setConsoleOpen] = useState(false)
  const [consoleFilter, setConsoleFilter] = useState<'all' | 'error' | 'info'>('all')
  const [consoleSearch, setConsoleSearch] = useState('')
  const [consoleEntries, setConsoleEntries] = useState<ConsoleEntry[]>([])
  const [search, setSearch] = useState('')
  const [searchMode, setSearchMode] = useState<'files' | 'content'>('files')
  const [searchResults, setSearchResults] = useState<SearchMatch[]>([])
  const [openFiles, setOpenFiles] = useState<string[]>([])
  const bottomRef = useRef<HTMLDivElement>(null)
  const previewRef = useRef<HTMLIFrameElement>(null)
  const consoleSequence = useRef(0)
  const uploadRef = useRef<HTMLInputElement>(null)
  const draftsRef = useRef<Record<string, string>>({})
  const id = project.id
  const isBuilding = project.status === 'running' || project.status === 'queued'
  const sessionReady = sessionProjectId === id
  const restoredWebPreview = restoredPreview?.id === id && !isBuilding && !!restoredPreview.runtime?.url
  const previewKind = restoredWebPreview ? 'web' : sessionReady ? sessionInfo.application_type || sessionInfo.demo?.kind || '' : ''
  const syncState = useRef({ busy, isBuilding })
  syncState.current = { busy, isBuilding }
  const nonWebPreview = ['cli', 'library', 'artifact'].includes(previewKind)
  const canLaunchPreview = sessionReady && !nonWebPreview && !isBuilding
  const previewBlocked = !canLaunchPreview
  const previewEligibility = useRef({ id, canLaunchPreview })
  previewEligibility.current = { id, canLaunchPreview }
  const runtimeController = useRef<AbortController | null>(null)
  const previewStartupRetry = useRef(false)
  const previewRetryTimer = useRef<number | undefined>(undefined)
  const latestJob = jobs.at(-1)
  const latestEditStep = latestJob?.steps.slice().reverse().find(step => editedFile(step))
  const latestEditPath = latestEditStep ? editedFile(latestEditStep) : ''

  useEffect(() => {
    let disposed = false
    setArtifactEntries([])
    if (sessionReady && !isBuilding && (nonWebPreview || sessionInfo.demo?.deliverables?.length)) {
      void request<{ artifacts: { path: string; name: string; size: number }[] }>(`/projects/${id}/artifacts`)
        .then(data => { if (!disposed) setArtifactEntries(data.artifacts) })
        .catch(error => { if (!disposed) setNotice((error as Error).message) })
    }
    return () => { disposed = true }
  }, [id, isBuilding, sessionReady, nonWebPreview, sessionInfo.demo?.ready, sessionInfo.demo?.deliverables?.map(item => item.path).join('|')])

  useEffect(() => {
    if (isBuilding && !project.preview_html && following) setTab('editor')
  }, [isBuilding, project.preview_html, following])

  useEffect(() => {
    try {
      const savedPins = JSON.parse(localStorage.getItem(`atoms:workspace-pins:${id}`) || '[]') as WorkspaceTool[]
      setPinnedTools(savedPins.filter(tool => workspaceTools.some(item => item.id === tool)))
      setNotebookText(localStorage.getItem(`atoms:workspace-notebook:${id}`) || '')
    } catch { setPinnedTools([]); setNotebookText('') }
  }, [id])

  useEffect(() => { setCurrentDirectory(''); setDirectoryEntries([]); setDirectoryWorkspaceId(null); setDirectoryWorkspaceTitle('') }, [id])

  const openWorkspaceTool = (tool: WorkspaceTool) => {
    setTab(tool === 'browser' ? 'preview' : tool)
    if (tool === 'browser') setFollowing(false)
    setMenu(null)
  }
  const directoryUrl = (path = currentDirectory, workspaceId = directoryWorkspaceId) => {
    const params = new URLSearchParams()
    if (workspaceId) params.set('workspace_id', workspaceId)
    if (path) params.set('path', path)
    return `/projects/${id}/directory${params.size ? `?${params}` : ''}`
  }
  const togglePinnedTool = (tool: WorkspaceTool) => {
    setPinnedTools(current => {
      const next = current.includes(tool) ? current.filter(item => item !== tool) : [...current, tool]
      localStorage.setItem(`atoms:workspace-pins:${id}`, JSON.stringify(next))
      return next
    })
  }
  const updateNotebook = (value: string) => {
    setNotebookText(value)
    localStorage.setItem(`atoms:workspace-notebook:${id}`, value)
  }

  useEffect(() => {
    let disposed = false
    const open = async () => {
      try {
        await request(`/projects/${id}/open`, { method: 'POST' })
      } catch (error) { if (!disposed) setNotice((error as Error).message) }
    }
    void open()
    const heartbeat = window.setInterval(() => {
      void request(`/projects/${id}/heartbeat`, { method: 'POST' }).catch(() => {})
    }, 15000)
    return () => { disposed = true; window.clearInterval(heartbeat) }
  }, [id])

  useEffect(() => {
    let disposed = false
    let controller: AbortController | null = null
    let refreshController: AbortController | null = null
    let refreshing = false
    let dirty = false
    let refreshTimer: number | undefined
    let reconnectTimer: number | undefined
    let lastPoll = 0
    const schedule = (delay = 250) => {
      dirty = true
      if (disposed || refreshing || refreshTimer !== undefined) return
      refreshTimer = window.setTimeout(() => { refreshTimer = undefined; void refresh() }, delay)
    }
    const refresh = async () => {
      if (disposed) return
      if (refreshing) { dirty = true; return }
      if (workspaceMutating.current || syncState.current.busy || document.hidden) { dirty = true; return }
      refreshing = true; dirty = false; lastPoll = Date.now()
      const revision = syncRevision.current
      const current = () => !disposed && revision === syncRevision.current && !workspaceMutating.current && !syncState.current.busy
      const abort = new AbortController()
      refreshController = abort
      const timeout = window.setTimeout(() => abort.abort(), 10000)
      try {
        // Session metadata is optional: a locked/corrupt checkpoint must never
        // prevent completed jobs and their final messages from reaching the UI.
        await Promise.allSettled([
          request<ChatProject>(`/projects/${id}`, { signal: abort.signal }).then(value => { if (current()) setProject(value) }),
          request<Job[]>(`/projects/${id}/jobs`, { signal: abort.signal }).then(value => { if (current()) setJobs(value) }),
          request<SessionInfo>(`/projects/${id}/session`, { signal: abort.signal }).then(value => {
            if (current()) { setSessionInfo(value); setSessionProjectId(id) }
          }).catch(() => { if (current()) setSessionProjectId(id) }),
        ])
      } finally {
        window.clearTimeout(timeout)
        if (refreshController === abort) refreshController = null
        refreshing = false
        if (!current()) dirty = true
        if (dirty && !disposed && !document.hidden && !workspaceMutating.current && !syncState.current.busy) schedule()
      }
    }
    refreshWorkspace.current = () => schedule(0)
    const listen = async () => {
      while (!disposed) {
        controller = new AbortController()
        try {
          const response = await authFetch(`/projects/${id}/events`, {
            signal: controller.signal, headers: { Accept: 'text/event-stream' }, cache: 'no-store',
          })
          if (!response.ok || !response.body) throw new Error('实时连接不可用')
          // Reconcile after every reconnect, including a connection whose
          // initial event was buffered/dropped by an intermediate proxy.
          schedule(0)
          const reader = response.body.getReader()
          const decoder = new TextDecoder()
          let pending = ''
          try {
            while (!disposed) {
              const { value, done } = await reader.read()
              if (done) break
              pending = (pending + decoder.decode(value, { stream: true })).replace(/\r\n/g, '\n')
              let boundary = pending.indexOf('\n\n')
              while (boundary >= 0) {
                const event = pending.slice(0, boundary)
                pending = pending.slice(boundary + 2)
                if (/^event:\s*change\s*$/m.test(event)) schedule()
                boundary = pending.indexOf('\n\n')
              }
            }
          } finally { await reader.cancel().catch(() => {}) }
        } catch { /* Polling remains active if streaming is unavailable. */ }
        if (!disposed) await new Promise<void>(resolve => { reconnectTimer = window.setTimeout(resolve, 2000) })
      }
    }
    const reconcile = () => { if (!document.hidden) schedule(0) }
    document.addEventListener('visibilitychange', reconcile)
    window.addEventListener('focus', reconcile)
    schedule(0)
    void listen()
    const timer = window.setInterval(() => {
      const interval = syncState.current.isBuilding ? 3000 : 15000
      if (!document.hidden && Date.now() - lastPoll >= interval) schedule(0)
    }, 1000)
    return () => {
      disposed = true; controller?.abort(); refreshController?.abort()
      window.clearInterval(timer); window.clearTimeout(refreshTimer); window.clearTimeout(reconnectTimer)
      document.removeEventListener('visibilitychange', reconcile)
      window.removeEventListener('focus', reconcile)
      refreshWorkspace.current = () => {}
    }
  }, [id])

  useEffect(() => { syncRevision.current += 1; if (!busy) refreshWorkspace.current() }, [busy])

  useEffect(() => {
    if (tab !== 'editor') return
    let disposed = false
    const refreshFiles = async () => request<{ files: string[] }>(`/projects/${id}/files`).then(data => {
      if (!disposed) {
        setFiles(data.files)
        const preferred = following && latestEditPath && data.files.includes(latestEditPath) ? latestEditPath : ''
        if (preferred && preferred !== activeFile) {
          setActiveFile(preferred)
          setOpenFiles(current => current.includes(preferred) ? current : [...current, preferred])
        } else if (data.files.length && !data.files.includes(activeFile)) {
          const initial = data.files.find(file => file.endsWith('App.tsx')) || data.files[0]
          setActiveFile(initial)
          setOpenFiles(current => current.includes(initial) ? current : [...current, initial])
        }
      }
    }).catch(error => { if (!disposed) setNotice(error.message) })
    void refreshFiles()
    // Build steps can create files without changing the project status. Keep
    // the tree and the selected editor file current while the agent is working.
    const timer = isBuilding ? window.setInterval(() => { if (document.visibilityState === 'visible') void refreshFiles() }, 2000) : undefined
    return () => { disposed = true; if (timer) window.clearInterval(timer) }
  }, [id, tab, project.status, isBuilding, following, latestEditPath, latestEditStep?.id, activeFile])

  useEffect(() => {
    if (tab !== 'terminal') return
    void request<Command[]>(`/projects/${id}/commands`).then(setCommands).catch(error => setNotice(error.message))
  }, [id, tab])

  useEffect(() => {
    if (tab !== 'files') return
    let disposed = false
    void request<{ entries: DirectoryEntry[] }>(directoryUrl()).then(data => {
      if (!disposed) setDirectoryEntries(data.entries)
    }).catch(error => { if (!disposed) setNotice(error.message) })
    return () => { disposed = true }
  }, [id, tab, currentDirectory, directoryWorkspaceId])

  useEffect(() => {
    if (tab !== 'editor' || searchMode !== 'content' || !search.trim()) { setSearchResults([]); return }
    let disposed = false
    const timer = window.setTimeout(() => {
      void request<{ matches: SearchMatch[] }>(`/projects/${id}/search?q=${encodeURIComponent(search.trim())}`)
        .then(data => { if (!disposed) setSearchResults(data.matches) })
        .catch(error => { if (!disposed) setNotice(error.message) })
    }, 250)
    return () => { disposed = true; window.clearTimeout(timer) }
  }, [id, tab, search, searchMode, project.status])

  const launchPreview = async (restart = false, command?: string) => {
    const eligible = () => previewEligibility.current.id === id && previewEligibility.current.canLaunchPreview && !workspaceMutating.current
    if (!eligible()) return
    runtimeController.current?.abort()
    const controller = new AbortController()
    runtimeController.current = controller
    setPreviewStarting(true); setPreviewError(''); setPreviewErrorDismissed(false)
    try {
      const result = await request<{ url: string; mode: 'live' | 'static' | 'none'; output: string }>(`/projects/${id}/runtime`, {
        method: 'POST', signal: controller.signal,
        body: JSON.stringify({ restart, ...(command === undefined ? {} : { command }) }),
      })
      if (controller.signal.aborted || !eligible()) return
      setPreviewUrl(isolatedPreviewUrl(result.url)); setPreviewMode(result.mode); setRuntimeOutput(result.output)
      if (restart) setFrameKey(key => key + 1)
    } catch (error) {
      if (!controller.signal.aborted && eligible()) {
        // Runtime startup can race the worker becoming ready immediately after
        // a build completes. Give it one bounded retry before showing an error;
        // the backend also falls back to the latest static build artifact.
        if (!restart && !previewStartupRetry.current) {
          previewStartupRetry.current = true
          previewRetryTimer.current = window.setTimeout(() => {
            if (eligible()) void launchPreview(true)
          }, 800)
        } else {
          setPreviewError((error as Error).message)
        }
      }
    } finally {
      if (runtimeController.current === controller) { runtimeController.current = null; setPreviewStarting(false) }
    }
  }

  useEffect(() => {
    runtimeController.current?.abort()
    window.clearTimeout(previewRetryTimer.current)
    previewStartupRetry.current = false
    setPreviewUrl(''); setPreviewMode('none'); setPreviewError(''); setPreviewErrorDismissed(false); setPreviewStarting(false)
    if (workspaceMutating.current) return
    if (restoredPreview?.id === id && !isBuilding) {
      if (restoredPreview.runtime) {
        setPreviewUrl(isolatedPreviewUrl(restoredPreview.runtime.url))
        setPreviewMode(restoredPreview.runtime.mode)
        setRuntimeOutput(restoredPreview.runtime.output)
      }
      if (restoredPreview.error) setPreviewError(restoredPreview.error)
    } else {
      if (restoredPreview) setRestoredPreview(null)
      if (canLaunchPreview && project.status !== 'error') void launchPreview()
    }
    return () => { runtimeController.current?.abort(); window.clearTimeout(previewRetryTimer.current) }
  }, [id, canLaunchPreview, project.status, previewKind, previewRevision, restoredPreview])

  useEffect(() => {
    if (!canLaunchPreview || previewMode !== 'live') return
    let disposed = false
    const timer = window.setInterval(() => {
      void request<{ status: { running: boolean; url: string; output: string } | null }>(`/projects/${id}/runtime`)
        .then(data => {
          if (disposed) return
          setRuntimeOutput(data.status?.output || '')
          if (!data.status?.running || isolatedPreviewUrl(data.status.url) !== previewUrl) void launchPreview()
        }).catch(() => {})
    }, 6000)
    return () => { disposed = true; window.clearInterval(timer) }
  }, [id, canLaunchPreview, previewMode, previewUrl])

  const configurePreview = async () => {
    if (previewBlocked) return
    const current = await request<{ command: string }>(`/projects/${id}/runtime`).catch(() => ({ command: '' }))
    const command = window.prompt('开发服务启动命令（留空自动检测 package.json 的 dev/start 脚本；服务需监听 $PORT）', current.command)
    if (command !== null) void launchPreview(true, command.trim())
  }

  const stopPreview = async () => {
    try {
      await request(`/projects/${id}/runtime/stop`, { method: 'POST' })
      setPreviewUrl(''); setPreviewMode('none'); setRuntimeOutput(''); setNotice('开发服务已停止')
    } catch (error) { setNotice((error as Error).message) }
  }

  useEffect(() => {
    const onPreviewMessage = (event: MessageEvent) => {
      if (event.source !== previewRef.current?.contentWindow) return
      if (event.data?.type === 'atoms-design-error') setNotice(String(event.data.message).slice(0, 200))
      if (event.data?.type === 'atoms-design-state') {
        const validReference = (reference: ElementReference) => reference && typeof reference.domPath === 'string' && typeof reference.code === 'string' && typeof reference.value === 'string'
        if (['none', 'select', 'text'].includes(event.data.mode)) setDesignMode(event.data.mode)
        if (Array.isArray(event.data.references)) setElementReferences(event.data.references.filter(validReference).slice(0, 10))
        if (Array.isArray(event.data.changes)) setVisualTextChanges(event.data.changes.filter((change: VisualTextChange) => validReference(change.reference) && typeof change.oldText === 'string' && typeof change.newText === 'string').slice(0, 10))
        if (event.data.mode !== 'none') setChatOpen(true)
      }
      if (event.data?.type === 'atoms-preview-error') {
        setPreviewError(String(event.data.message || '预览运行失败'))
        setPreviewErrorDismissed(false)
      }
      if (event.data?.type === 'atoms-preview-console') {
        const level = String(event.data.level || 'log')
        const message = String(event.data.message || '').slice(0, 2000)
        if (level === 'info' && message.startsWith('[vite]')) return
        setConsoleEntries(previous => [...previous.slice(-299), {
          id: ++consoleSequence.current,
          level,
          message,
          time: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
        }])
      }
    }
    window.addEventListener('message', onPreviewMessage)
    return () => window.removeEventListener('message', onPreviewMessage)
  }, [])

  useEffect(() => { setPreviewError(''); setPreviewErrorDismissed(false); setConsoleEntries([]) }, [previewUrl, frameKey])
  useEffect(() => { setDesignMode('none'); setElementReferences([]); setVisualTextChanges([]) }, [id, previewUrl, frameKey])
  useEffect(() => { if (isBuilding) { setDesignMode('none'); setElementReferences([]); setVisualTextChanges([]) } }, [isBuilding])
  useEffect(() => { if (project.preview_html) setFrameKey(key => key + 1) }, [project.preview_html])

  useEffect(() => {
    if (!historyOpen) return
    void request<Version[]>(`/projects/${id}/versions`).then(setVersions).catch(error => setNotice(error.message))
  }, [id, historyOpen, project.status])

  useEffect(() => {
    if (!activeFile) return
    let disposed = false
    void request<{ content: string }>(`/projects/${id}/files/${encodedPath(activeFile)}`).then(data => {
      if (!disposed) { setFileText(draftsRef.current[activeFile] ?? data.content); setSavedText(data.content); setFileReadable(true) }
    }).catch(error => { if (!disposed) { setFileReadable(false); setNotice(error.message) } })
    return () => { disposed = true }
  }, [id, activeFile, files.join('|'), project.status, latestEditStep?.id])

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }) }, [jobs.length, latestJob?.steps.length, project.messages.length])
  useEffect(() => {
    if (following && latestJob?.status === 'done' && (project.preview_html || artifactEntries.length)) setTab('preview')
  }, [following, latestJob?.status, project.preview_html, artifactEntries.length])

  const selectFile = (file: string) => {
    if (activeFile && fileText !== savedText) draftsRef.current[activeFile] = fileText
    if (!file) { setActiveFile(''); setFileText(''); setSavedText(''); setFileReadable(false); return }
    if (file === activeFile) return
    setFileText(''); setSavedText(''); setFileReadable(false)
    setActiveFile(file)
    setOpenFiles(current => current.includes(file) ? current : [...current, file])
  }
  const openEditedFile = (file: string) => {
    pendingEditedFileRef.current = tab === 'editor' ? null : file
    setFollowing(false)
    setTab('editor')
    setTreeOpen(true)
    setFiles(current => current.includes(file) ? current : [...current, file].sort())
    selectFile(file)
  }
  useEffect(() => {
    if (tab !== 'editor' || !latestEditPath) return
    const file = pendingEditedFileRef.current || latestEditPath
    pendingEditedFileRef.current = null
    selectFile(file)
    setOpenFiles(current => current.includes(file) ? current : [...current, file])
    setFiles(current => current.includes(file) ? current : [...current, file].sort())
    setTreeOpen(true)
    setSearch('')
    setSearchMode('files')
  }, [id, tab, latestEditStep?.id])
  const saveFile = async () => {
    if (working || !fileReadable) return
    setWorking(true); setNotice('')
    try {
      await request(`/projects/${id}/files/${encodedPath(activeFile)}`, { method: 'PUT', body: JSON.stringify({ content: fileText }) })
      delete draftsRef.current[activeFile]
      setSavedText(fileText); setNotice(`${activeFile} 已保存`)
    } catch (error) { setNotice((error as Error).message) } finally { setWorking(false) }
  }
  const createFile = async () => {
    const path = window.prompt('新文件路径', 'src/NewFile.tsx')?.trim()
    if (!path) return
    if (files.includes(path)) { setNotice('文件已存在'); return }
    try {
      await request(`/projects/${id}/files/${encodedPath(path)}`, { method: 'PUT', body: JSON.stringify({ content: '' }) })
      setFiles(current => [...new Set([...current, path])].sort())
      selectFile(path); setNotice(`已创建 ${path}`)
    } catch (error) { setNotice((error as Error).message) }
  }
  const renameFile = async () => {
    if (!activeFile) return
    if (fileText !== savedText) { setNotice('请先保存当前文件'); return }
    const target = window.prompt('重命名文件', activeFile)?.trim()
    if (!target || target === activeFile) return
    try {
      await request(`/projects/${id}/files/move`, { method: 'POST', body: JSON.stringify({ source: activeFile, target }) })
      setFiles(current => current.map(file => file === activeFile ? target : file).sort())
      setOpenFiles(current => current.map(file => file === activeFile ? target : file))
      if (draftsRef.current[activeFile] !== undefined) draftsRef.current[target] = draftsRef.current[activeFile]
      delete draftsRef.current[activeFile]
      setActiveFile(target); setNotice(`已重命名为 ${target}`)
    } catch (error) { setNotice((error as Error).message) }
  }
  const deleteFile = async () => {
    if (!activeFile || !window.confirm(`删除 ${activeFile}？`)) return
    try {
      await request(`/projects/${id}/files/${encodedPath(activeFile)}`, { method: 'DELETE' })
      const remaining = files.filter(file => file !== activeFile)
      setFiles(remaining); setOpenFiles(current => current.filter(file => file !== activeFile))
      delete draftsRef.current[activeFile]
      setFileReadable(false); setFileText(''); setSavedText('')
      setActiveFile(remaining[0] || ''); setNotice('文件已删除')
    } catch (error) { setNotice((error as Error).message) }
  }
  const uploadFiles = async (selected: FileList | null) => {
    if (!selected) return
    try {
      const uploaded: string[] = []
      for (const file of Array.from(selected)) {
        if (file.size > 5_000_000) throw new Error(`${file.name} 超过 5 MB`)
        const response = await authFetch(`/projects/${id}/assets/${encodeURIComponent(file.name)}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/octet-stream' }, body: file,
        })
        if (!response.ok) {
          const body = await response.json().catch(() => ({})) as { detail?: string }
          throw new Error(body.detail || `${file.name} 上传失败`)
        }
        uploaded.push((await response.json() as { path: string }).path)
      }
      const listing = await request<{ files: string[] }>(`/projects/${id}/files`)
      setFiles(listing.files); setNotice(`已上传 ${uploaded.join('、')}；构建后即可在预览中使用`)
    } catch (error) { setNotice((error as Error).message) }
  }
  const uploadWorkspaceFiles = async (selected: FileList | null) => {
    if (!selected?.length) return
    const targetWorkspaceId = directoryWorkspaceId || id
    const targetWorkspaceTitle = directoryWorkspaceTitle || project.title
    try {
      const uploaded: string[] = []
      for (const file of Array.from(selected)) {
        const relativePath = file.webkitRelativePath || file.name
        const path = [currentDirectory, relativePath].filter(Boolean).join('/')
        const response = await authFetch(`/projects/${id}/directory/upload/${targetWorkspaceId}/${encodedPath(path)}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/octet-stream' }, body: file,
        })
        if (!response.ok) {
          const body = await response.json().catch(() => ({})) as { detail?: string }
          throw new Error(body.detail || `${file.name} 上传失败`)
        }
        uploaded.push(path)
      }
      const data = await request<{ entries: DirectoryEntry[] }>(directoryUrl())
      setDirectoryEntries(data.entries)
      setNotice(`已上传 ${uploaded.length} 个文件到 ${targetWorkspaceTitle}${currentDirectory ? `/${currentDirectory}` : ''}`)
    } catch (error) { setNotice((error as Error).message) }
  }
  const openDirectoryFile = async (entry: DirectoryEntry) => {
    if (!directoryWorkspaceId) return
    try {
      const response = await authFetch(`/projects/${id}/directory/file/${directoryWorkspaceId}?path=${encodeURIComponent(entry.path)}`)
      if (!response.ok) {
        const body = await response.json().catch(() => ({})) as { detail?: string }
        throw new Error(body.detail || '读取文件失败')
      }
      const blob = await response.blob()
      const textFile = /^(text\/|application\/(json|javascript|xml|x-yaml))/.test(blob.type) || /\.(txt|md|json|ya?ml|toml|tsx?|jsx?|css|html|svg|xml|sh|py|go|rs|java|c|h|sql|log)$/i.test(entry.name)
      if (textFile) setFilePreview({ name: entry.name, mime: blob.type, text: await blob.text() })
      else setFilePreview({ name: entry.name, mime: blob.type, url: URL.createObjectURL(blob) })
    } catch (error) { setNotice((error as Error).message) }
  }
  const closeFilePreview = () => {
    if (filePreview?.url) URL.revokeObjectURL(filePreview.url)
    setFilePreview(null)
  }
  const downloadDirectoryEntry = async (entry: DirectoryEntry) => {
    if (!directoryWorkspaceId) return
    try {
      const response = await authFetch(`/projects/${id}/directory/download/${directoryWorkspaceId}?path=${encodeURIComponent(entry.path)}`)
      if (!response.ok) {
        const body = await response.json().catch(() => ({})) as { detail?: string }
        throw new Error(body.detail || '下载失败')
      }
      const url = URL.createObjectURL(await response.blob())
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = entry.kind === 'directory' ? `${entry.name}.zip` : entry.name
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (error) { setNotice((error as Error).message) }
  }
  const confirmDirectoryDelete = async () => {
    if (!pendingDelete) return
    try {
      if (pendingDelete.kind === 'project' && pendingDelete.workspaceId) {
        const response = await authFetch(`/projects/${pendingDelete.workspaceId}`, { method: 'DELETE' })
        if (!response.ok) {
          const body = await response.json().catch(() => ({})) as { detail?: string }
          throw new Error(body.detail || '删除项目失败')
        }
        setDirectoryWorkspaceId(null); setDirectoryWorkspaceTitle(''); setCurrentDirectory('')
      } else if (pendingDelete.workspaceId && pendingDelete.path) {
        const response = await authFetch(`/projects/${id}/directory/${pendingDelete.workspaceId}/${encodedPath(pendingDelete.path)}`, { method: 'DELETE' })
        if (!response.ok) {
          const body = await response.json().catch(() => ({})) as { detail?: string }
          throw new Error(body.detail || '删除失败')
        }
      }
      const data = await request<{ entries: DirectoryEntry[] }>(pendingDelete.kind === 'project' ? `/projects/${id}/directory` : directoryUrl())
      setDirectoryEntries(data.entries)
      setNotice(`${pendingDelete.name} 已删除`)
    } catch (error) { setNotice((error as Error).message) }
    setPendingDelete(null)
  }
  const runTerminal = async () => {
    if (!terminalInput.trim() || working || isBuilding) return
    setWorking(true)
    try {
      const entry = await request<Command>(`/projects/${id}/commands`, { method: 'POST', body: JSON.stringify({ command: terminalInput.trim() }) })
      setCommands(current => [...current, entry]); setTerminalInput('')
      setNotice(entry.exit_code === 0 ? '命令执行完成' : `命令退出码 ${entry.exit_code}`)
    } catch (error) { setNotice((error as Error).message) } finally { setWorking(false) }
  }
  const build = async () => {
    if (working || workspaceMutating.current) return
    workspaceMutating.current = true; syncRevision.current += 1
    setWorking(true); setNotice('正在构建…')
    try {
      const result = await request<{ version: number }>(`/projects/${id}/build`, { method: 'POST' })
      setNotice(`构建成功 · 版本 ${result.version}`); setTab('preview'); setFrameKey(key => key + 1)
      setProject(await request<ChatProject>(`/projects/${id}`))
      setRestoredPreview(null); setPreviewRevision(value => value + 1)
    } catch (error) { setNotice((error as Error).message) }
    finally {
      workspaceMutating.current = false; syncRevision.current += 1
      setWorking(false); refreshWorkspace.current()
    }
  }
  const restore = async (version: number) => {
    if (working || workspaceMutating.current || !window.confirm(`还原版本 ${version}？当前代码将被该版本替换。`)) return
    workspaceMutating.current = true; syncRevision.current += 1
    runtimeController.current?.abort(); window.clearTimeout(previewRetryTimer.current)
    setWorking(true); setNotice('正在还原版本并恢复预览…')
    setPreviewUrl(''); setPreviewMode('none'); setPreviewError(''); setPreviewStarting(true)
    try {
      const result = await request<ChatProject & { restored_preview?: PreviewResult; restore_preview_error?: string }>(`/projects/${id}/restore/${version}`, { method: 'POST' })
      syncRevision.current += 1
      workspaceMutating.current = false
      setProject(result)
      setSessionProjectId(id)
      setRestoredPreview({ id, runtime: result.restored_preview, error: result.restore_preview_error })
      draftsRef.current = {}; setFileText(''); setSavedText(''); setFiles([])
      setNotice(result.restore_preview_error ? `版本 ${version} 的代码已还原，但预览启动失败：${result.restore_preview_error}` : `已还原版本 ${version}`)
      setHistoryOpen(false); setTab('preview'); setFrameKey(key => key + 1)
    } catch (error) { setNotice((error as Error).message); setPreviewError((error as Error).message) }
    finally {
      workspaceMutating.current = false; syncRevision.current += 1
      setWorking(false); setPreviewStarting(false); refreshWorkspace.current()
    }
  }

  const downloadSource = async () => {
    try {
      const response = await authFetch(`/projects/${id}/archive`)
      if (!response.ok) throw new Error('下载失败')
      const url = URL.createObjectURL(await response.blob())
      const link = document.createElement('a'); link.href = url; link.download = `atoms-${id.slice(0, 8)}.zip`; link.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (error) { setNotice((error as Error).message) }
  }
  const openPreview = () => {
    if (previewUrl) window.open(previewUrl, '_blank', 'noopener,noreferrer')
  }
  const downloadArtifact = async (entry: { path: string; name: string }) => {
    try {
      const response = await authFetch(`/projects/${id}/artifacts/download?path=${encodeURIComponent(entry.path)}`)
      if (!response.ok) throw new Error('成果下载失败，请重试')
      const url = URL.createObjectURL(await response.blob())
      const anchor = document.createElement('a'); anchor.href = url; anchor.download = entry.name; anchor.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (error) { setNotice((error as Error).message) }
  }
  const stopJob = async (jobId: string) => {
    try {
      await request(`/projects/${id}/jobs/${jobId}/stop`, { method: 'POST' })
      setNotice('已请求停止任务')
    } catch (error) { setNotice((error as Error).message) }
  }
  const terminalSteps = useMemo(() => jobs.flatMap(job => job.steps), [jobs])
  const executionEntries = useMemo(() => jobs.flatMap(job => {
    if (job.status === 'running' || job.status === 'queued') return []
    const validationSteps = job.steps.map((step, index) => ({ step, index })).filter(({ step }) =>
      step.kind === 'result' && (/^(构建通过|构建失败|未配置构建命令)$/.test(step.label) || /^测试 (通过|失败)/.test(step.label))
    )
    const lastBuildIndex = validationSteps.filter(({ step }) => /^(构建通过|构建失败|未配置构建命令)$/.test(step.label)).at(-1)?.index ?? -1
    const finalSteps = validationSteps.filter(({ step, index }) => index >= lastBuildIndex && (lastBuildIndex >= 0 || /^测试 (通过|失败)/.test(step.label)))
    return finalSteps.map(({ step }) => ({
      id: `job-${job.id}-${step.id}`,
      level: /失败/.test(step.label) ? 'error' : 'info',
      message: `${step.label}${step.detail ? `\n${step.detail}` : ''}`,
      time: new Date(step.created_at).toLocaleTimeString('zh-CN', { hour12: false }),
    }))
  }), [jobs])
  const allConsoleEntries = useMemo(() => [...executionEntries, ...consoleEntries], [executionEntries, consoleEntries])
  const visibleConsoleEntries = useMemo(() => allConsoleEntries.filter(entry =>
    (consoleFilter === 'all' || (consoleFilter === 'error' ? entry.level === 'error' : entry.level !== 'error')) &&
    JSON.stringify(entry).normalize('NFKC').toLowerCase().includes(consoleSearch.trim().normalize('NFKC').toLowerCase())
  ), [allConsoleEntries, consoleFilter, consoleSearch])
  const consoleErrorCount = allConsoleEntries.filter(entry => entry.level === 'error').length

  let jobIndex = 0
  return <div className={`build-page ${chatOpen ? '' : 'chat-hidden'}`}>
    <header className="build-header">
      <div className="build-header-left">
        <button className="build-home-button" title="回到主页" onClick={onHome}><Home size={16} /><span>回到主页</span></button>
        {projectMenu}
        <div className="build-header-spacer" />
        <button className="build-top-icon" title="历史记录" onClick={() => setHistoryOpen(true)}><History size={17} /></button>
        <button className="build-top-icon" title={chatOpen ? '关闭聊天框' : '打开聊天框'} onClick={() => setChatOpen(!chatOpen)}>{chatOpen ? <ChevronsLeft size={18} /> : <PanelLeftOpen size={18} />}</button>
      </div>
      <div className="build-header-right">
        <div className="build-tool-tabs">
          <button className={tab === 'preview' ? 'active' : ''} title="应用查看器" onClick={() => { setTab('preview'); setFollowing(false) }}><Laptop size={16} /><span>应用查看器</span></button>
          <button className={tab === 'editor' ? 'active' : ''} title="编辑器" onClick={() => { setTab('editor'); setFollowing(false) }}><Code2 size={16} /><span>编辑器</span></button>
          <button className={consoleOpen && tab === 'preview' ? 'active' : ''} title="控制台" onClick={() => { setTab('preview'); setConsoleOpen(open => !open); setFollowing(false) }}><TerminalSquare size={16} /><span>控制台</span></button>
          <button className={tab === 'files' ? 'active' : ''} title="项目文件" onClick={() => setTab('files')}><Folder size={16} /><span>文件</span></button>
          <button title="历史记录" onClick={() => setHistoryOpen(true)}><History size={16} /></button>
          {pinnedTools.map(tool => { const Icon = toolIcons[tool]; return <button key={tool} title={workspaceTools.find(item => item.id === tool)?.label} className={tab === (tool === 'browser' ? 'preview' : tool) ? 'active' : ''} onClick={() => openWorkspaceTool(tool)}><Icon size={16} /></button> })}
          <div className="build-menu-anchor" onMouseEnter={() => { if (moreMenuCloseTimer.current) clearTimeout(moreMenuCloseTimer.current); moreMenuCloseTimer.current = null; setMenu('more') }} onMouseLeave={() => { moreMenuCloseTimer.current = setTimeout(() => { setMenu(current => current === 'more' ? null : current); moreMenuCloseTimer.current = null }, 300) }}><button title="更多" onClick={() => setMenu(menu === 'more' ? null : 'more')}><MoreHorizontal size={18} /></button>
            {menu === 'more' && <div className="build-menu right build-tool-menu"><div className="build-tool-menu-title">工作区工具</div>{workspaceTools.map(tool => { const Icon = toolIcons[tool.id]; return <div className="build-tool-menu-row" key={tool.id}><button className="build-tool-open" onClick={() => openWorkspaceTool(tool.id)}><Icon size={15} /><span>{tool.label}</span>{tab === (tool.id === 'browser' ? 'preview' : tool.id) && <i />}</button><button className="build-tool-pin" title={pinnedTools.includes(tool.id) ? '取消置顶' : '置顶工具'} aria-label={pinnedTools.includes(tool.id) ? `取消置顶${tool.label}` : `置顶${tool.label}`} onClick={() => togglePinnedTool(tool.id)}>{pinnedTools.includes(tool.id) ? <PinOff size={14} /> : <Pin size={14} />}</button></div>})}<div className="build-tool-menu-divider" /><button onClick={() => { void downloadSource(); setMenu(null) }}>下载项目</button><button onClick={() => { setHistoryOpen(true); setMenu(null) }}>版本历史</button><button onClick={() => { onRename(); setMenu(null) }}>重命名</button>{project.published && <button onClick={() => { onUnpublish(); setMenu(null) }}>取消发布</button>}</div>}
          </div>
        </div>
        <div className="build-actions">{isBuilding && <button className={`build-follow ${following ? 'on' : ''}`} onClick={() => setFollowing(!following)}><span>•</span>{following ? '正在跟随智能体' : '跟随智能体'}</button>}<button className="build-share" disabled={!project.preview_html} onClick={onShare}>分享</button><button className="build-publish" disabled={publishBusy} onClick={onPublish}>{publishBusy ? '发布中…' : project.published ? '更新' : '发布'}</button></div>
      </div>
    </header>
    <div className="build-content">
      {chatOpen && <section className="build-chat">
        <div className="build-feed">
          {project.messages.map(message => {
            const job = message.role === 'user' ? jobs[jobIndex++] : undefined
            const expanded = job ? activityOpen[job.id] ?? (job.status !== 'done') : false
            return <div key={message.id}>
              {!!message.file_references?.length && <div className="build-file-reference-chips">{message.file_references.map(path => <span key={path}><button type="button" title={`打开 ${path}`} onClick={() => openEditedFile(path)}><File size={13} />{path}</button></span>)}</div>}
              {message.role === 'user' ? <div className="build-user-row"><div className="build-user-bubble">{!!message.element_references?.length && <div className="build-element-references">{message.element_references.map(reference => <span key={reference.domPath} title={`${reference.domPath}\n${reference.text}`}><MousePointer2 size={13} />{reference.value}</span>)}</div>}{!!message.expert_ids?.length && <div className="build-message-experts">{message.expert_ids.map(id => <span key={id}>使用 {experts.find(expert => expert.id === id)?.name || id}</span>)}</div>}{message.content}{!!message.attachments?.length && <div className="build-message-media-list">{message.attachments.map(attachment => <SavedMedia key={attachment.id} projectId={project.id} attachment={attachment} />)}</div>}</div></div> :
                <div className="build-agent-message"><img src="/agents/5.webp" alt="" /><div className="build-agent-content"><div className="build-agent-meta"><span>{message.agent || 'Alex'}</span><span>工程师</span><time>{new Date(message.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time></div><MessageText content={message.content} files={editedFiles(jobs[jobIndex - 1])} onOpenFile={openEditedFile} />{jobs[jobIndex - 1]?.status === 'done' && <button className="build-version-card" onClick={() => setHistoryOpen(true)}><strong>{jobs[jobIndex - 1].steps.find(step => step.kind === 'version')?.label || '版本已完成'}</strong><span>查看历史 <ChevronRight size={14} /></span></button>}</div></div>}
              {job && <><div className="build-agent-message build-workflow-agent"><img src="/agents/5.webp" alt="" /><div className="build-agent-content"><div className="build-agent-meta"><span>Alex</span><span>工程师</span><time>{new Date(job.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time></div></div></div><div className="build-workflow">
                <button className="build-workflow-head" onClick={() => setActivityOpen(state => ({ ...state, [job.id]: !expanded }))}><CircleCheck size={18} className={job.status === 'done' ? 'done' : ''} /><span>{job.status === 'done' ? `本轮已结束 · ${jobDuration(job)}` : job.status === 'running' ? '工作流程' : job.status === 'queued' ? '正在排队' : job.status === 'stopped' ? '已停止' : '构建遇到问题'}</span>{expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}</button>
                {expanded && <div className="build-workflow-steps">{job.steps.filter(step => step.kind !== 'usage').map(step => { const filePath = filePathForStep(step); return <div className="build-workflow-step" key={step.id}><span className="build-step-dot" /><div><span>{stepLabel(step)}</span>{stepDetail(step) && <div className={step.kind === 'tool' ? 'build-tool-call' : 'build-step-detail'}>{step.kind === 'tool' && <File size={14} />}{filePath && <button className="build-step-file-link" onClick={() => openEditedFile(filePath)} title={`在编辑器中打开 ${filePath}`}><File size={14} />{filePath}</button>}<span>{stepDetail(step).slice(0, 260)}{stepDetail(step).length > 260 ? '…' : ''}</span></div>}</div></div>})}</div>}
                {(job.status === 'running' || job.status === 'queued') && <div className="build-agent-working"><span className="build-working-mark">✦</span>Alex 正在工作 <button disabled={job.stop_requested} onClick={() => void stopJob(job.id)}><Square size={12} /> 停止</button></div>}
                {job.status === 'error' && <div className="build-error">{job.error}</div>}
              </div></>}
            </div>
          })}
          <div ref={bottomRef} />
        </div>
        <div className="build-compose">
          {!!elementReferences.length && <div className="build-element-references"><small>设计</small>{elementReferences.map(reference => <span key={reference.domPath} title={`${reference.domPath}\n${reference.text}`}><MousePointer2 size={13} />{reference.value}<button title="移除元素引用" onClick={() => { designCommand({ action: 'remove', domPath: reference.domPath }); setElementReferences(current => current.filter(item => item.domPath !== reference.domPath)) }}><X size={12} /></button></span>)}</div>}
          <PendingAttachments className="build-compose-media" items={[
            ...media.map((attachment, index) => ({ key: `image-${index}-${attachment.name}`, attachment, onRemove: () => setMedia(items => items.filter((_, itemIndex) => itemIndex !== index)) })),
            ...documents.map((attachment, index) => ({ key: `document-${index}-${attachment.name}`, attachment, onRemove: () => setDocuments(items => items.filter((_, itemIndex) => itemIndex !== index)) })),
          ]} />
          <FileMentionInput projectId={id} draft={draft} setDraft={setDraft} references={fileReferences} setReferences={setFileReferences} onSubmit={() => { if (!busy && !isBuilding) void submitMessage() }} onError={setNotice} onPaste={event => { const selected = Array.from(event.clipboardData.files).filter(file => file.type.startsWith('image/')); if (selected.length) { event.preventDefault(); void addMedia(selected) } }} placeholder={elementReferences.length ? '描述需要修改所选元素的内容…' : '输入 @ 引用项目文件，粘贴或添加图片。'} />
          <div className="build-expert-row">{expertControl}{tierControl}{toolsControl}</div>
          <div className="build-compose-bottom">
            <div className="build-menu-anchor" onMouseEnter={() => { if (addMenuCloseTimer.current) clearTimeout(addMenuCloseTimer.current); addMenuCloseTimer.current = null; setMenu('add') }} onMouseLeave={() => { addMenuCloseTimer.current = setTimeout(() => { setMenu(current => current === 'add' ? null : current); addMenuCloseTimer.current = null }, 300) }}><button className="build-round-button" title="添加" onClick={() => setMenu(menu === 'add' ? null : 'add')}><Plus size={18} /></button>{menu === 'add' && <div className="build-menu above"><div className="build-model-slot">{modelControl}</div><button onClick={() => { setMenu(null); onChooseExperts() }}>选择专家</button><button onClick={() => mediaRef.current?.click()}>上传图片</button><button onClick={() => documentRef.current?.click()}>上传文档</button><button onClick={() => { setTab('editor'); setMenu(null) }}>查看项目文件</button></div>}<input ref={mediaRef} type="file" accept={mediaAccept} multiple hidden onChange={event => { void addMedia(Array.from(event.target.files || [])); event.target.value = '' }} /><input ref={documentRef} type="file" accept={documentAccept} multiple hidden onChange={event => { void addDocuments(Array.from(event.target.files || [])); event.target.value = '' }} /></div>
            <div className="build-compose-spacer" /><VoiceInput key={id} projectId={id} draft={draft} setDraft={setDraft} onError={setNotice} />
            <button className="build-send" disabled={(!draft.trim() && !media.length && !documents.length && !fileReferences.length) || busy || isBuilding} onClick={() => void submitMessage()} title={isBuilding ? '构建完成后继续对话' : '发送消息'}>{isBuilding ? <Square size={15} fill="currentColor" /> : <ArrowUp size={19} />}</button>
          </div>
        </div>
      </section>}
      <section className="build-workspace">
        {tab === 'files' && <div className="build-project-files">
          <div className="build-project-files-head"><div className="build-breadcrumbs"><button onClick={() => { setDirectoryWorkspaceId(null); setDirectoryWorkspaceTitle(''); setCurrentDirectory('') }}>我的项目</button>{directoryWorkspaceId && <span><ChevronRight size={13} /><button onClick={() => setCurrentDirectory('')}>{directoryWorkspaceTitle}</button></span>}{currentDirectory.split('/').filter(Boolean).map((part, index, parts) => <span key={index}><ChevronRight size={13} /><button onClick={() => setCurrentDirectory(parts.slice(0, index + 1).join('/'))}>{part}</button></span>)}</div><div className="build-project-file-actions"><input ref={projectFileUploadRef} type="file" multiple hidden onChange={event => { void uploadWorkspaceFiles(event.target.files); event.target.value = '' }} /><input ref={projectFolderUploadRef} type="file" multiple hidden onChange={event => { void uploadWorkspaceFiles(event.target.files); event.target.value = '' }} /><button disabled={isBuilding || working} onClick={() => projectFileUploadRef.current?.click()}><Upload size={14} />上传文件</button><button disabled={isBuilding || working} onClick={() => { projectFolderUploadRef.current?.setAttribute('webkitdirectory', ''); projectFolderUploadRef.current?.click() }}><Folder size={14} />上传文件夹</button></div></div>
          <div className="build-project-files-table"><div className="build-project-files-row heading"><span>文件名</span><span>大小</span><span>最后更新</span></div>{(currentDirectory || directoryWorkspaceId) && <button className="build-project-files-row parent" onClick={() => { if (currentDirectory) setCurrentDirectory(currentDirectory.split('/').slice(0, -1).join('/')); else { setDirectoryWorkspaceId(null); setDirectoryWorkspaceTitle('') } }}><span><Folder size={15} />..</span><span>—</span><span>{currentDirectory ? '返回上级目录' : '返回我的项目'}</span></button>}{directoryEntries.map(entry => <div className="build-project-files-row" key={`${entry.workspace_id || ''}:${entry.path || entry.name}`}><div className="build-project-entry-main"><button className="build-project-file-name" onClick={() => { if (entry.kind === 'directory' && !directoryWorkspaceId && entry.workspace_id) { setDirectoryWorkspaceId(entry.workspace_id); setDirectoryWorkspaceTitle(entry.name); setCurrentDirectory('') } else if (entry.kind === 'directory') setCurrentDirectory(entry.path); else void openDirectoryFile(entry) }}><span>{entry.kind === 'directory' ? <Folder size={15} /> : <FileCode2 size={15} />}{entry.name}</span></button><div className="build-project-entry-actions"><button title={entry.kind === 'directory' && !directoryWorkspaceId ? '删除项目' : '删除'} aria-label={`删除 ${entry.name}`} onClick={() => setPendingDelete({ kind: entry.kind === 'directory' && !directoryWorkspaceId ? 'project' : 'entry', name: entry.name, workspaceId: entry.workspace_id || directoryWorkspaceId || undefined, path: entry.path || undefined })}><Trash2 size={15} /></button>{directoryWorkspaceId && <button title={entry.kind === 'directory' ? '下载文件夹 ZIP' : '下载文件'} aria-label={`下载 ${entry.name}`} onClick={() => void downloadDirectoryEntry(entry)}><Download size={15} /></button>}</div></div><span>{displaySize(entry.size)}</span><span>{new Date(entry.modified_at).toLocaleString('zh-CN')}</span></div>)}{!directoryEntries.length && <p className="build-project-files-empty">{directoryWorkspaceId ? '此目录为空，可上传文件或文件夹。' : '还没有可访问的项目工作区。'}</p>}</div><div className="build-project-files-foot">{directoryEntries.length} 个项目</div>
        </div>}
        {tab === 'preview' && <div className={`build-viewer ${consoleOpen ? 'console-open' : ''}`}>
          {nonWebPreview || !sessionReady ? <div className="build-viewer-bar"><span style={{padding:'8px 16px'}}>成果预览</span></div> : <div className="build-viewer-bar"><button title={mobile ? '桌面预览' : '手机预览'} onClick={() => setMobile(!mobile)}>{mobile ? <Smartphone size={15} /> : <Laptop size={15} />}</button><button title="刷新页面" disabled={previewBlocked} onClick={() => setFrameKey(key => key + 1)}><RefreshCw size={15} /></button><button title="重启开发服务" disabled={previewBlocked || previewStarting} onClick={() => void launchPreview(true)}><Play size={15} /></button><button title="停止开发服务" disabled={previewMode !== 'live'} onClick={() => void stopPreview()}><Square size={14} /></button><button title="配置启动命令" disabled={previewBlocked || previewStarting} onClick={() => void configurePreview()}><TerminalSquare size={15} /></button><button title="主页" disabled={previewBlocked} onClick={() => setFrameKey(key => key + 1)}><Home size={15} /></button><div className="build-address">{isBuilding ? '代码构建中' : previewMode === 'live' ? '实时开发服务' : previewMode === 'static' ? '构建产物' : '等待服务'} <ChevronDown size={13} /></div><button title="新标签页打开" disabled={!previewUrl || previewBlocked} onClick={openPreview}><ExternalLink size={15} /></button><button className="build-console-link" onClick={() => setConsoleOpen(open => !open)}><Code2 size={14} /> 控制台{consoleErrorCount > 0 ? ` · ${consoleErrorCount}` : ''}</button></div>}
          {artifactEntries.length > 0 && !nonWebPreview && <div style={{display:'flex',gap:8,padding:8}}><button onClick={() => setShowArtifacts(false)}>应用预览</button><button onClick={() => setShowArtifacts(true)}>成果文件</button></div>}<div className={`build-viewer-body ${mobile ? 'mobile' : ''}`}>{isBuilding ? <div className="build-preview-empty"><span>✳</span><strong>{sessionInfo.delivery_budget?.phase === 'stabilization' ? '正在修复启动与预览，准备演示' : '正在完成任务'}</strong><p>成果通过实际运行或文件预览检查后，会自动展示。</p></div> : artifactEntries.length > 0 && (showArtifacts || nonWebPreview) ? <ArtifactViewer projectId={id} entries={artifactEntries} onDownload={entry => void downloadArtifact(entry)} /> : !sessionReady || previewKind === 'artifact' ? <div className="build-preview-empty"><strong>{!sessionReady ? '正在读取成果信息…' : '暂未生成成果文件'}</strong></div> : previewUrl ? <iframe key={frameKey} ref={previewRef} title={`${project.title} 预览`} src={`${previewUrl}?v=${frameKey}`} sandbox="allow-scripts allow-forms allow-modals" /> : sessionInfo.demo?.ready && nonWebPreview ? <div className="build-preview-empty"><strong>{artifactEntries.length ? "生成成果已就绪" : "程序演示验证通过"}</strong>{artifactEntries.map(entry => <button key={entry.path} onClick={() => void downloadArtifact(entry)}>下载 {entry.name}</button>)}<pre style={{ whiteSpace: 'pre-wrap', textAlign: 'left', maxWidth: '90%', maxHeight: '70%', overflow: 'auto' }}>{sessionInfo.demo.report}</pre><p>可在终端再次运行演示命令。</p><button onClick={() => setTab('terminal')}>打开终端</button></div> : <div className="build-preview-empty"><span>✳</span><strong>{previewStarting ? '正在启动开发服务' : project.status === 'error' ? '构建遇到问题' : '暂无 Web 服务'}</strong><p>{previewStarting ? '请稍候，正在工作区运行启动命令。' : project.status === 'error' ? '请先查看终端中的任务错误，修复后再重试预览。' : '配置开发服务命令，或在终端运行非 Web 程序。'}</p></div>}{previewError && !previewErrorDismissed && !isBuilding && <div className="build-preview-error"><strong>预览运行失败</strong><span>{previewError.includes('WebGL') ? '当前浏览器无法创建 WebGL 上下文。' : previewError} 请查看终端日志，修复后重启或刷新预览。</span><button onClick={() => { setPreviewErrorDismissed(true); setTab('terminal') }}>查看终端</button><button onClick={() => void launchPreview(true)}>重启</button><button title="关闭提示" aria-label="关闭预览错误提示" onClick={() => setPreviewErrorDismissed(true)}><X size={14} /></button></div>}</div>
          {previewUrl && !isBuilding && (designToolsCollapsed ? <button className="build-design-expand" title="展开设计工具" aria-label="展开设计工具" onClick={() => setDesignToolsCollapsed(false)}><ChevronLeft size={17} /></button> : <div className="build-design-tools-wrap"><div className={`build-design-tools ${designMode !== 'none' ? 'editing' : ''}`}><button title="选择元素" className={designMode === 'select' ? 'active' : ''} onClick={() => activateDesign(designMode === 'select' ? 'none' : 'select')}><MousePointer2 size={16} /></button><button title="修改文本" className={designMode === 'text' ? 'active' : ''} onClick={() => activateDesign(designMode === 'text' ? 'none' : 'text')}>T</button>{designMode === 'none' ? <button title="主题" onClick={() => setNotice('请在代码编辑器中修改主题')}>◉</button> : <><span className="build-design-status">{visualTextChanges.length ? `${visualTextChanges.length} 个文本更改` : designMode === 'select' ? elementReferences.length ? `编辑 ${elementReferences.length} 个选中项，在聊天窗口中` : '点击选择，Ctrl/Cmd + 点击可多选' : '点击文本修改，或双击直接编辑'}</span>{visualTextChanges.length ? <><button className="build-design-action" onClick={() => designCommand({ action: 'discard' })}>丢弃</button><button className="build-design-action save" disabled={busy} onClick={() => void saveVisualText()}>保存</button></> : <button className="build-design-action" onClick={() => activateDesign('none')}>退出</button>}</>}</div><button className="build-design-collapse" title="隐藏设计工具" aria-label="隐藏设计工具" onClick={() => setDesignToolsCollapsed(true)}><ChevronRight size={17} /></button></div>)}
          {consoleOpen && <div className="build-preview-console">
            <div className="build-console-head"><strong>控制台</strong><div className="build-console-filters">
              <button className={consoleFilter === 'all' ? 'active' : ''} onClick={() => setConsoleFilter('all')}>{allConsoleEntries.length} 全部</button>
              <button className={consoleFilter === 'error' ? 'active' : ''} onClick={() => setConsoleFilter('error')}>{consoleErrorCount} 错误</button>
              <button className={consoleFilter === 'info' ? 'active' : ''} onClick={() => setConsoleFilter('info')}>{allConsoleEntries.length - consoleErrorCount} 信息</button>
            </div><span /><label><Search size={14} /><input type="search" value={consoleSearch} onChange={event => setConsoleSearch(event.target.value)} placeholder="搜索日志" aria-label="搜索控制台日志" />{consoleSearch && <button className="build-console-search-clear" title="清除搜索" aria-label="清除日志搜索" onClick={() => setConsoleSearch('')}><X size={13} /></button>}</label><button onClick={() => setConsoleEntries([])}>清除所有</button><button title="关闭控制台" onClick={() => setConsoleOpen(false)}><X size={15} /></button></div>
            <div className="build-console-output">{visibleConsoleEntries.length ? visibleConsoleEntries.map(entry => <div key={entry.id} className={`build-console-entry ${entry.level === 'error' ? 'error' : ''}`}><time>{entry.time}</time><span>{entry.message}</span></div>) : <p>{allConsoleEntries.length ? '此筛选下没有日志' : isBuilding ? '任务完成后显示最终构建与运行结果；Agent 全过程日志请查看终端。' : '暂无构建或浏览器运行结果。'}</p>}</div>
          </div>}
        </div>}
        {tab === 'editor' && <div className="build-editor">
          {treeOpen && <aside className="build-file-panel">
            <div className="build-file-search"><button title="收起文件树" onClick={() => setTreeOpen(false)}><PanelLeftClose size={16} /></button><label><Search size={14} /><input value={search} onChange={event => setSearch(event.target.value)} placeholder={searchMode === 'files' ? '搜索文件' : '搜索内容'} aria-label="搜索文件" /></label></div>
            <div className="build-file-actions"><button className={searchMode === 'files' ? 'active' : ''} onClick={() => setSearchMode('files')}>文件名</button><button className={searchMode === 'content' ? 'active' : ''} onClick={() => setSearchMode('content')}>内容</button><span /><button title="新建文件" disabled={isBuilding} onClick={() => void createFile()}><FilePlus2 size={15} /></button><button title="上传资源" disabled={isBuilding} onClick={() => uploadRef.current?.click()}><Upload size={15} /></button><input ref={uploadRef} type="file" hidden multiple onChange={event => { void uploadFiles(event.target.files); event.target.value = '' }} /></div>
            {searchMode === 'files' ? <FileTree files={files} activeFile={activeFile} search={search} onSelect={selectFile} /> : <div className="build-search-results">{search.trim() ? searchResults.length ? searchResults.map((match, index) => <button key={index} onClick={() => selectFile(match.path)}><strong>{match.path}:{match.line}</strong><span>{match.text.trim()}</span></button>) : <p>没有匹配内容</p> : <p>输入关键词搜索项目代码</p>}</div>}
            <button className="build-download" onClick={() => void downloadSource()}><Download size={15} /> 下载项目</button>
          </aside>}
          <div className="build-code-panel"><div className="build-editor-tabs">{!treeOpen && <button title="展开文件树" onClick={() => setTreeOpen(true)}><PanelLeftOpen size={16} /></button>}{openFiles.map(file => <button key={file} className={file === activeFile ? 'active' : ''} onClick={() => selectFile(file)}><FileCode2 size={14} />{file.split('/').at(-1)}{file === activeFile && fileText !== savedText ? ' •' : ''}<X size={13} onClick={event => { event.stopPropagation(); setOpenFiles(current => current.filter(item => item !== file)); if (file === activeFile) selectFile(openFiles.find(item => item !== file) || files.find(item => item !== file) || '') }} /></button>)}</div><div className="build-code-actions"><span>{activeFile || '选择文件'}</span><button disabled={working || isBuilding || !activeFile} onClick={() => void renameFile()}>重命名</button><button disabled={working || isBuilding || !activeFile} onClick={() => void deleteFile()}><Trash2 size={14} /> 删除</button><button disabled={working || !fileReadable || fileText === savedText || isBuilding || !activeFile} onClick={() => void saveFile()}><Save size={14} /> 保存</button><button disabled={working || isBuilding || fileText !== savedText} onClick={() => void build()}><Code2 size={14} /> 构建</button></div><div className="build-code-editor"><Suspense fallback={<div className="build-code-loading">正在加载代码编辑器…</div>}>{activeFile ? <ProjectCodeEditor projectId={id} file={activeFile} value={fileReadable ? fileText : undefined} readOnly={isBuilding || !fileReadable} onChange={content => { setFileText(content); draftsRef.current[activeFile] = content }} /> : <div className="build-code-loading">{activeFile ? '文件正在加载，或无法作为文本编辑。' : '选择文件'}</div>}</Suspense></div></div>
        </div>}
        {tab === 'planner' && <div className="build-tool-panel"><div className="build-tool-panel-head"><ListTodo size={17} /><strong>计划器</strong><span>Agent 任务阶段</span></div><div className="build-planner-content">{jobs.length ? [...jobs].reverse().map(job => <section key={job.id}><h3>{job.prompt}</h3>{job.steps.filter(step => step.kind === 'state' || step.kind === 'thinking' || step.kind === 'plan').map(step => <div className="build-planner-step" key={step.id}><CircleCheck size={15} className={step.label === 'COMPLETE' ? 'done' : ''} /><div><strong>{stepLabel(step)}</strong>{stepDetail(step) && <p>{stepDetail(step)}</p>}</div></div>)}</section>) : <p>发送需求后，这里会显示 Agent 的理解、实现、验证和复核阶段。</p>}</div></div>}
        {tab === 'notebook' && <div className="build-tool-panel"><div className="build-tool-panel-head"><NotebookPen size={17} /><strong>笔记本</strong><span>仅保存在此项目工作区</span></div><textarea className="build-notebook" value={notebookText} onChange={event => updateNotebook(event.target.value)} placeholder="记录项目约定、待办事项或开发备注…" /></div>}
        {tab === 'terminal' && <div className="build-terminal"><div className="build-terminal-tabs"><span>工作区终端 · Agent 全过程</span></div><div className="build-terminal-output">{runtimeOutput && <><h3>开发服务日志</h3><div><pre>{runtimeOutput}</pre></div></>}{terminalSteps.length > 0 && <><h3>智能体全过程日志</h3>{terminalSteps.map(step => <div key={step.id}><span>{stepLabel(step)}</span>{stepDetail(step) && <pre>{stepDetail(step)}</pre>}</div>)}</>}{commands.length > 0 && <><h3>手动命令</h3>{commands.map(entry => <div key={entry.id}><span>$ {entry.command}</span><small>退出码 {entry.exit_code}</small>{entry.output && <pre>{entry.output}</pre>}</div>)}</>}{!runtimeOutput && !terminalSteps.length && !commands.length && <p>在下方运行项目命令，或查看智能体的构建活动。</p>}</div><div className="build-terminal-compose"><span>$</span><input value={terminalInput} onChange={event => setTerminalInput(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') void runTerminal() }} placeholder="例如 npm test、npm run build 或 ls -la" aria-label="工作区命令" disabled={working || isBuilding} /><button disabled={!terminalInput.trim() || working || isBuilding} onClick={() => void runTerminal()}>运行</button></div></div>}
        {filePreview && <div className="build-file-preview-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) closeFilePreview() }}><div className="build-file-preview" role="dialog" aria-modal="true" aria-label={`查看 ${filePreview.name}`}><header><strong>{filePreview.name}</strong><button title="关闭" onClick={closeFilePreview}><X size={17} /></button></header>{filePreview.text !== undefined ? <pre>{filePreview.text}</pre> : filePreview.mime.startsWith('image/') ? <img src={filePreview.url} alt={filePreview.name} /> : filePreview.mime === 'application/pdf' ? <iframe src={filePreview.url} title={filePreview.name} /> : <div className="build-file-preview-download"><p>此文件类型不支持内嵌预览。</p><a href={filePreview.url} download={filePreview.name}>下载文件</a></div>}</div></div>}
        {pendingDelete && <div className="build-file-preview-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) setPendingDelete(null) }}><div className="build-confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="build-delete-title"><h2 id="build-delete-title">确认删除</h2><p>确定删除“{pendingDelete.name}”吗？{pendingDelete.kind === 'project' ? '项目及其工作区将被永久删除。' : '此操作无法撤销。'}</p><footer><button onClick={() => setPendingDelete(null)}>取消</button><button className="danger" onClick={() => void confirmDirectoryDelete()}>删除</button></footer></div></div>}
        {notice && <div className="build-notice"><span>{notice}</span><button onClick={() => setNotice('')} aria-label="关闭提示"><X size={15} /></button></div>}
      </section>
    </div>
    {historyOpen && <div className="build-history-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) setHistoryOpen(false) }}><aside className="build-history"><div className="build-history-head"><strong>历史记录</strong><button title="关闭历史记录" onClick={() => setHistoryOpen(false)}><X size={17} /></button></div><div className="build-history-tab">版本</div><div className="build-history-list">{versions.length ? versions.map(version => <div className="build-version" key={version.version}><div><strong>版本 {version.version}</strong><p>{version.summary.split('\n').find(line => line.trim())?.slice(0, 90) || project.title}</p><time>{new Date(version.created_at).toLocaleString('zh-CN')}</time></div><span>v{version.version}</span><button disabled={working || isBuilding} onClick={() => void restore(version.version)}>还原</button></div>) : <p>版本管理的探索之旅正等着你</p>}</div></aside></div>}
  </div>
}
