import { useEffect, useRef, useState } from 'react'
import discoverData from './discover-data.json'
import AuthPage from './AuthPage'
import ToolsSelector, { savedBuildTools, type BuildTool } from './ToolsSelector'
import BuildTierSelector, { savedBuildTier, type BuildTier } from './BuildTierSelector'
import ChatWorkspace, { type MessageOptions } from './ChatWorkspace'
import PendingAttachments from './PendingAttachments'
import AccountMenu, { AccountAvatar } from './AccountMenu'
import AccountSettings, { RedeemDialog } from './AccountSettings'
import { accountApi, saveAccount, type Account } from './account'
import { formatCredits } from './numberFormat'
import { copyLink } from './clipboard'
import PublishDialog, { type PublishedRelease, type PublishVersion } from './PublishDialog'
import PublishedProjectDetail from './PublishedProjectDetail'
import { ProjectMenu, ProjectDialog, ProjectSettings, type ProjectAction } from './ProjectMenu'
import { RecentProject, DeleteProjectDialog } from './ProjectActions'
import ExpertsPage, { ExpertDetails, ExpertPicker, type Expert } from './ExpertLibrary'
import { authFetch, logoutSession, restoreSession, type AuthUser } from './auth'
import { documentAccept, mediaAccept, mediaPayload, readDocumentFiles, readMediaFiles, type DocumentAttachment, type MediaAttachment, type UploadAttachment } from './media'
import {
  ArrowUp, Bell, Check, ChevronDown, ChevronRight, Code2, Compass,
  Download, ExternalLink, Gift, Globe2, Home, Menu,
  MoreHorizontal, PanelLeftClose, PanelLeftOpen, Paperclip, Plus, Search, Settings2,
  Share2, Sparkles, Square, WandSparkles, X, Zap, type LucideIcon,
} from 'lucide-react'

type Project = { enabled_tools: BuildTool[]; build_tier: BuildTier; publish_slug?: string; cover_image_url?: string; public_url?: string; owned?: boolean; favorite?: boolean; visibility?: string; remove_badge?: boolean; expert_ids: string[]; id: string; title: string; prompt: string; kind: 'Web' | 'App'; mode: 'Build' | 'Goal'; model: string; status: string; preview_html: string; published: boolean; created_at: string; updated_at: string }
type AiModel = { id: string; provider: string; name: string; context: number; input_price: number; output_price: number; available: boolean | null; image_input: boolean | null }
type ModelCatalog = { default_model: string; models: AiModel[]; prices_unit: string }
type Message = { id: string; project_id: string; role: string; agent: string | null; content: string; created_at: string }
type ProjectDetail = Project & { messages: Message[] }
type Section = 'home' | 'resources' | 'projects' | 'discover' | 'templates' | 'experts'

const api = async <T,>(path: string, options?: RequestInit): Promise<T> => {
  const response = await authFetch(path, options)
  if (!response.ok) { const text = await response.text(); let message = text; try { const body = JSON.parse(text); message = typeof body.detail === 'string' ? body.detail : text } catch { /* keep plain text */ } throw new Error(message || `请求失败 (${response.status})`) }
  return response.status === 204 ? undefined as T : response.json()
}

const agentNames = ['Mike', 'Adrian', 'Sarah', 'Emma', 'Bob', 'Alex', 'David', 'Iris']
const agentRoles = ['Team Leader', 'Ads Specialist', 'SEO Specialist', 'Product Manager', 'Architect', 'Engineer', 'Data Analyst', 'Deep Researcher']
const agentImages = ['/agents/0.webp', '/agents/1.png', '/agents/2.webp', '/agents/3.webp', '/agents/4.webp', '/agents/5.webp', '/agents/6.webp', '/agents/7.webp']
type Discovery = (typeof discoverData)[number]
const initialSection = (): Section => window.location.pathname.includes('/experts') ? 'experts' : window.location.pathname.includes('/my-projects') ? 'projects' : window.location.pathname.includes('/discover') ? 'resources' : 'home'

function IconButton({ icon: Icon, label, onClick, className = '' }: { icon: LucideIcon; label: string; onClick?: () => void; className?: string }) {
  return <button type="button" title={label} aria-label={label} className={`icon-button ${className}`} onClick={onClick}><Icon size={18} strokeWidth={1.8} /></button>
}

function AgentAvatars() {
  return <div className="agents" aria-label="AI 智能体团队">{agentNames.map((name, index) => <div key={name} className="agent" title={`${name} · ${agentRoles[index]}`}><span className="avatar"><img src={agentImages[index]} alt={`${name} - ${agentRoles[index]}`} /></span></div>)}</div>
}

function ModelSelector({ models, value, onChange }: { models: AiModel[]; value: string; onChange: (model: string) => void }) {
  const [showModels, setShowModels] = useState(false)
  const [query, setQuery] = useState('')
  const selectorRef = useRef<HTMLDivElement>(null)
  const selected = models.find(item => item.id === value)
  const providers = [...new Set(models.map(item => item.provider))]
  const visibleModels = models.filter(item => `${item.provider} ${item.name}`.toLowerCase().includes(query.trim().toLowerCase()))
  useEffect(() => {
    const closeOnOutside = (event: PointerEvent) => { if (!selectorRef.current?.contains(event.target as Node)) { setShowModels(false) } }
    document.addEventListener('pointerdown', closeOnOutside)
    return () => document.removeEventListener('pointerdown', closeOnOutside)
  }, [])
  return <div className="model-selector" ref={selectorRef}>
    <button type="button" className="model-trigger" aria-label="选择 AI 模型" aria-haspopup="listbox" aria-expanded={showModels} disabled={!models.length} onClick={() => { setShowModels(open => !open); setQuery('') }}>
      <span>{selected ? `${selected.provider} · ${selected.name}` : models.length ? '选择模型' : '暂无可用模型'}</span><ChevronDown size={14} />
    </button>
    {showModels && <div className="model-options-popover">
      <label className="model-search"><Search size={14} /><input autoFocus value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索模型" /></label>
      <div className="model-options-scroll" role="listbox" aria-label="可用模型">
        {providers.map(provider => {
          const items = visibleModels.filter(item => item.provider === provider)
          return items.length ? <section className="model-option-group" key={provider}><div className="model-option-heading">{provider}</div>{items.map(item => <button type="button" role="option" aria-selected={item.id === value} key={item.id} onClick={() => { onChange(item.id); setShowModels(false) }}><span>{item.name}</span>{item.id === value && <Check size={14} />}</button>)}</section> : null
        })}
        {!visibleModels.length && <div className="model-empty">没有匹配的模型</div>}
      </div>
    </div>}
  </div>
}

function PromptBox({ onSubmit, busy, models, selectedModel, onModelChange, initialText = '', compact = false, toolsControl, tierControl, expertControl, onChooseExperts }: { toolsControl: React.ReactNode; tierControl: React.ReactNode; expertControl: React.ReactNode; onChooseExperts: () => void; onSubmit: (prompt: string, kind: 'Web' | 'App', mode: 'Build' | 'Goal', attachments: UploadAttachment[]) => Promise<void>; busy: boolean; models: AiModel[]; selectedModel: string; onModelChange: (model: string) => void; initialText?: string; compact?: boolean }) {
  const [value, setValue] = useState(initialText)
  const [kind, setKind] = useState<'Web' | 'App'>('Web')
  const [mode, setMode] = useState<'Build' | 'Goal'>('Build')
  const [menu, setMenu] = useState<'add' | 'mode' | null>(null)
  useEffect(() => {
    if (!menu) return
    const outside = (event: PointerEvent) => {
      const target = event.target
      if (!(target instanceof Element) || !target.closest(`[data-prompt-menu="${menu}"]`)) setMenu(null)
    }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setMenu(null) }
    document.addEventListener('pointerdown', outside, true)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('pointerdown', outside, true)
      document.removeEventListener('keydown', escape)
    }
  }, [menu])
  const [documents, setDocuments] = useState<DocumentAttachment[]>([])
  const [media, setMedia] = useState<MediaAttachment[]>([])
  const fileRef = useRef<HTMLInputElement>(null)
  const mediaRef = useRef<HTMLInputElement>(null)
  useEffect(() => setValue(initialText), [initialText])
  const send = async () => {
    if ((!value.trim() && !media.length && !documents.length) || busy) return
    try { await onSubmit(value.trim() || '请参考附件构建项目', kind, mode, [...media, ...documents]); setValue(''); setDocuments([]); setMedia([]) } catch { /* the page shows the request error and keeps the prompt */ }
  }
  const chooseFiles = async (selected: FileList | null) => {
    if (!selected) return
    try { const next = await readDocumentFiles(Array.from(selected), documents, media); setDocuments(previous => [...previous, ...next]); setMenu(null) }
    catch (error) { alert((error as Error).message) }
  }
  const addMedia = async (selected: File[]) => {
    try { const next = await readMediaFiles(selected, media); setMedia(previous => [...previous, ...next]); setMenu(null) }
    catch (error) { alert((error as Error).message) }
  }
  return <div className={`prompt-shell ${compact ? 'compact' : ''}`}>
    <div className="prompt-main">
      <PendingAttachments className="home-pending-attachments" items={[
        ...documents.map((attachment, index) => ({ key: `document-${index}-${attachment.name}`, attachment, onRemove: () => setDocuments(items => items.filter((_, itemIndex) => itemIndex !== index)) })),
        ...media.map((attachment, index) => ({ key: `image-${index}-${attachment.name}`, attachment, onRemove: () => setMedia(items => items.filter((_, itemIndex) => itemIndex !== index)) })),
      ]} />
      <textarea value={value} onChange={event => setValue(event.target.value)} onPaste={event => { const selected = Array.from(event.clipboardData.files).filter(file => file.type.startsWith('image/')); if (selected.length) { event.preventDefault(); void addMedia(selected) } }} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void send() } }} placeholder={compact ? '向 Alex 描述你希望修改的地方…' : '请Alex构建'} aria-label="描述你的想法" />
      <div className="prompt-controls">
        <div className="prompt-left"><div className="relative" data-prompt-menu="add"><IconButton icon={Plus} label="添加工具或附件" onClick={() => setMenu(menu === 'add' ? null : 'add')} className="circle-control" />{menu === 'add' && <div className="popover add-menu"><button onClick={() => { setMenu(null); onChooseExperts() }}><Sparkles size={17} /> 专家 <ChevronRight size={15} /></button><button onClick={() => { setMenu(null); mediaRef.current?.click() }}><Paperclip size={17} /> 上传图片</button><button onClick={() => { setMenu(null); fileRef.current?.click() }}><Paperclip size={17} /> 上传文档</button><button onClick={() => { setKind('Web'); setMenu(null) }}><Globe2 size={17} /> 网页项目 {kind === 'Web' && <Check size={15} />}</button><button onClick={() => { setKind('App'); setMenu(null) }}><Square size={17} /> 应用项目 {kind === 'App' && <Check size={15} />}</button><button onClick={() => { setValue(text => `${text}请先研究市场和用户需求，再给出产品方案。`); setMenu(null) }}><Search size={17} /> 深度研究</button><button onClick={() => { setValue(text => `${text}请提供三种不同设计方案供比较。`); setMenu(null) }}><WandSparkles size={17} /> 竞赛模式</button><button onClick={() => { document.dispatchEvent(new Event('open-connectors')); setMenu(null) }}><Zap size={17} /> 连接工具</button></div>}</div><input ref={fileRef} type="file" accept={documentAccept} multiple hidden onChange={event => { void chooseFiles(event.target.files); event.target.value = '' }} /><input ref={mediaRef} type="file" accept={mediaAccept} multiple hidden onChange={event => { void addMedia(Array.from(event.target.files || [])); event.target.value = '' }} /><ModelSelector models={models} value={selectedModel} onChange={onModelChange} /><div className="prompt-inline-experts">{expertControl}</div>{toolsControl}</div>
        <div className="prompt-right">{tierControl}<div className="relative" data-prompt-menu="mode"><button className="mode-button" onClick={() => setMenu(menu === 'mode' ? null : 'mode')}>{mode === 'Build' ? '构建' : '目标'} <ChevronDown size={13} /></button>{menu === 'mode' && <div className="popover mode-menu"><button onClick={() => { setMode('Build'); setMenu(null) }}><Code2 size={17} /><span><b>构建</b><small>直接构建产品</small></span></button><button onClick={() => { setMode('Goal'); setMenu(null) }}><Sparkles size={17} /><span><b>目标</b><small>从目标开始规划</small></span></button></div>}</div><button className="send-button" disabled={(!value.trim() && !media.length && !documents.length) || busy} onClick={() => void send()} aria-label="发送需求">{busy ? <span className="spinner" /> : <ArrowUp size={20} />}</button></div>
      </div>
    </div>
    {!compact && <button className="connect-strip" onClick={() => document.dispatchEvent(new Event('open-connectors'))}><span><Zap size={15} /> 将你的工具连接到 Atoms</span><span className="connector-icons"><i>▣</i><i>✚</i><i>◉</i><i>▲</i><i>▥</i></span><X size={14} /></button>}
  </div>
}

function App() {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [authReady, setAuthReady] = useState(false)
  const [authError, setAuthError] = useState('')
  const [path, setPath] = useState(window.location.pathname + window.location.search)
  const [projects, setProjects] = useState<Project[]>([])
  const [publicProjects, setPublicProjects] = useState<Project[]>([])
  const [detail, setDetail] = useState<ProjectDetail | null>(null)
  const [section, setSection] = useState<Section>(initialSection)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [theme, setTheme] = useState(() => localStorage.getItem('atoms-theme') || 'light')
  const [modal, setModal] = useState<'settings' | 'credits' | 'connectors' | 'share' | 'publish' | null>(null)
  const [shareCopyError, setShareCopyError] = useState('')
  useEffect(() => { setShareCopyError('') }, [modal])
  const [publishBusy, setPublishBusy] = useState(false)
  const [publishSuccess, setPublishSuccess] = useState(false)
  const [publishError, setPublishError] = useState('')
  const [publishPhase, setPublishPhase] = useState('')
  const [publishedReleases, setPublishedReleases] = useState<PublishedRelease[]>([])
  const [publishVersions, setPublishVersions] = useState<{ projectId: string; versions: PublishVersion[] } | null>(null)
  const [publishVersionsLoading, setPublishVersionsLoading] = useState(false)
  const [prefill, setPrefill] = useState('')
  const [profileOpen, setProfileOpen] = useState(false)
  const [workspaceOpen, setWorkspaceOpen] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null)
  const [deleteBusy, setDeleteBusy] = useState(false)
  const [deleteError, setDeleteError] = useState('')
  const [projectAction, setProjectAction] = useState<ProjectAction | null>(null)
  const [renameTarget, setRenameTarget] = useState<Project | null>(null)
  const [projectMenu, setProjectMenu] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [recentMenuId, setRecentMenuId] = useState<string | null>(null)
  const [discoverTab, setDiscoverTab] = useState<'discover' | 'projects' | 'templates'>('discover')
  const [resourceMode, setResourceMode] = useState<'discover' | 'templates'>('discover')
  const [resourceCategory, setResourceCategory] = useState('全部')
  const [favoriteOnly, setFavoriteOnly] = useState(false)
  const [favorites, setFavorites] = useState<string[]>(() => JSON.parse(localStorage.getItem('atoms-favorites') || '[]'))
  const [account, setAccount] = useState<Account | null>(null)
  const [redeemOpen, setRedeemOpen] = useState(false)
  const [profileTab, setProfileTab] = useState('公开项目')
  const [experts, setExperts] = useState<Expert[]>([])
  const [expertsLoading, setExpertsLoading] = useState(false)
  const [expertsError, setExpertsError] = useState('')
  const [homeExpertIds, setHomeExpertIds] = useState<string[]>([])
  const [expertPickerOpen, setExpertPickerOpen] = useState(false)
  const [expertDetails, setExpertDetails] = useState<Expert | null>(null)
  const [expertsSaving, setExpertsSaving] = useState(false)
  const [models, setModels] = useState<AiModel[]>([])
  const [selectedModel, setSelectedModel] = useState(() => localStorage.getItem('atoms-model') || '')
  const [homeBuildTier, setHomeBuildTier] = useState<BuildTier>(savedBuildTier)
  const [projectBuildTiers, setProjectBuildTiers] = useState<Record<string, BuildTier>>({})
  const activeBuildTier = detail ? projectBuildTiers[detail.id] || detail.build_tier || 'normal' : homeBuildTier
  const changeBuildTier = (tier: BuildTier) => {
    if (detail) setProjectBuildTiers(current => ({ ...current, [detail.id]: tier }))
    else { setHomeBuildTier(tier); localStorage.setItem('atoms-build-tier', tier) }
  }
  const [homeTools, setHomeTools] = useState<BuildTool[]>(savedBuildTools)
  const [projectTools, setProjectTools] = useState<Record<string, BuildTool[]>>({})
  const [toolsSaving, setToolsSaving] = useState(false)
  const activeTools = detail ? projectTools[detail.id] ?? detail.enabled_tools ?? [] : homeTools
  const changeTools = async (tools: BuildTool[]) => {
    if (!detail) { setHomeTools(tools); localStorage.setItem('atoms-build-tools', JSON.stringify(tools)); return }
    const id = detail.id
    const previous = activeTools
    setProjectTools(current => ({ ...current, [id]: tools }))
    setToolsSaving(true)
    try { await api(`/projects/${id}/tools`, { method: 'PATCH', body: JSON.stringify({ enabled_tools: tools }) }) }
    catch (e) { setProjectTools(current => ({ ...current, [id]: previous })); setError(e instanceof Error ? e.message : String(e)) }
    finally { setToolsSaving(false) }
  }
  const toolsControl = <ToolsSelector value={activeTools} onChange={tools => void changeTools(tools)} disabled={busy || toolsSaving} />
  const tierControl = <BuildTierSelector value={activeBuildTier} onChange={changeBuildTier} disabled={busy} />

  const loadExperts = async () => { setExpertsLoading(true); setExpertsError(''); try { setExperts(await api<Expert[]>('/experts')) } catch (e) { setExpertsError((e as Error).message) } finally { setExpertsLoading(false) } }
  useEffect(() => { if (user) void loadExperts(); else { setExperts([]); setHomeExpertIds([]); setExpertDetails(null) } }, [user?.id])
  useEffect(() => { setExpertPickerOpen(false); if (user && path.split('?')[0] === '/zh/experts') void loadExperts() }, [path])

  const settingsPage = new URLSearchParams(path.split('?')[1] || '').get('settings')
  const openSettings = (page: string) => { setModal(null); setProfileOpen(false); route(`${window.location.pathname}?settings=${page}`) }
  const closeSettings = () => route(window.location.pathname)
  const updateAccount = (value: Account) => { setAccount(value); setTheme(value.preferences.theme); if (value.preferences.default_model !== account?.preferences.default_model) setSelectedModel(value.preferences.default_model) }
  const route = (next: string) => { window.history.pushState({}, '', next); setPath(next) }
  const refresh = async () => { try { const next = await api<Project[]>('/projects'); setProjects(next); setFavorites(current => [...current.filter(id => !/^[a-f0-9-]{36}$/.test(id)), ...next.filter(p => p.favorite).map(p => p.id)]) } catch (e) { setError((e as Error).message) } }
  const recoverAuth = () => {
    setAuthReady(false); setAuthError('')
    void restoreSession().then(setUser).catch((error: Error) => setAuthError(error.message || '暂时无法确认登录状态，请重试')).finally(() => setAuthReady(true))
  }
  useEffect(() => {
    recoverAuth()
    const popstate = () => setPath(window.location.pathname + window.location.search)
    const expired = () => { setUser(null); setProjects([]); setDetail(null) }
    const restored = (event: Event) => { setUser((event as CustomEvent<AuthUser>).detail); setAuthError('') }
    window.addEventListener('popstate', popstate); window.addEventListener('auth-expired', expired); window.addEventListener('auth-restored', restored)
    return () => { window.removeEventListener('popstate', popstate); window.removeEventListener('auth-expired', expired); window.removeEventListener('auth-restored', restored) }
  }, [])
  useEffect(() => { if (!user) return; void refresh(); void api<Project[]>('/discover/projects').then(setPublicProjects).catch(() => setPublicProjects([])); api<ModelCatalog>('/models').then(data => { setModels(data.models); setSelectedModel(current => current && data.models.some(item => item.id === current && item.available !== false) ? current : data.default_model) }).catch(e => setError(e.message)) }, [user?.id])
  useEffect(() => { if (!user) { setAccount(null); return }; void accountApi<Account>().then(data => { setAccount(data); setTheme(data.preferences.theme); setSelectedModel(data.preferences.default_model) }).catch(e => setError(e.message)) }, [user?.id])
  useEffect(() => { if (modal === 'settings' || modal === 'credits' || modal === 'connectors') openSettings(modal === 'settings' ? 'profile' : modal === 'credits' ? 'plan' : 'connectors') }, [modal])
  useEffect(() => { if (!user) return; const refreshBalance = () => { if (!document.hidden) void accountApi<Account>().then(setAccount).catch(() => {}) }; const timer = setInterval(refreshBalance, 5000); window.addEventListener('billing-changed', refreshBalance); return () => { clearInterval(timer); window.removeEventListener('billing-changed', refreshBalance) } }, [user?.id])
  useEffect(() => { if (selectedModel) localStorage.setItem('atoms-model', selectedModel) }, [selectedModel])
  useEffect(() => { const system = matchMedia('(prefers-color-scheme: dark)'); const apply = () => { document.documentElement.dataset.theme = theme === 'system' ? (system.matches ? 'dark' : 'light') : theme; document.documentElement.lang = account?.preferences.language === 'en' ? 'en' : 'zh-CN' }; apply(); system.addEventListener('change', apply); localStorage.setItem('atoms-theme', theme); return () => system.removeEventListener('change', apply) }, [theme, account?.preferences.language])
  useEffect(() => { localStorage.setItem('atoms-favorites', JSON.stringify(favorites)) }, [favorites])
  useEffect(() => { if (!/\/(?:project|chat)\//.test(path)) setSection(initialSection()) }, [path])
  useEffect(() => { const handler = () => setModal('connectors'); document.addEventListener('open-connectors', handler); return () => document.removeEventListener('open-connectors', handler) }, [])
  useEffect(() => { if (!user) return; const id = path.match(/\/(?:project|chat)\/([a-f\d-]+)/)?.[1]; if (id) { api<ProjectDetail>(`/projects/${id}`).then(setDetail).catch(e => setError(e.message)) } else setDetail(null) }, [path, user?.id])
  useEffect(() => { if (detail?.model) setSelectedModel(detail.model) }, [detail?.id])

  const selectSection = (next: Section) => { setSection(next); if (next === 'templates') setResourceMode('templates'); else if (next === 'resources' || next === 'discover') setResourceMode('discover'); route(next === 'experts' ? '/zh/experts' : next === 'projects' ? '/zh/my-projects' : next === 'home' ? '/zh/dashboard' : '/zh/discover'); setProfileOpen(false) }
  const openProject = (id: string) => { setProjectMenu(null); setDraft(''); route(`/zh/chat/${id.replaceAll('-', '')}`) }
  const clonePublicProject = async (id: string, title: string) => {
    const next = await api<Project>(`/projects/${id}/clone`, { method: 'POST', body: JSON.stringify({ title, copy_database: false }) })
    await refresh()
    openProject(next.id)
  }
  const createProject = async (prompt: string, kind: 'Web' | 'App', mode: 'Build' | 'Goal', attachments: UploadAttachment[]) => {
    setBusy(true); setError('')
    try { const project = await api<Project>('/projects', { method: 'POST', body: JSON.stringify({ prompt, kind, mode, enabled_tools: homeTools, build_tier: homeBuildTier, model: selectedModel, attachments: mediaPayload(attachments), expert_ids: homeExpertIds }) }); await refresh(); openProject(project.id) }
    catch (e) { setError((e as Error).message); throw e } finally { setBusy(false) }
  }
  const sendMessage = async (attachments: UploadAttachment[], options?: MessageOptions) => {
    const content = options?.content || draft.trim()
    if (expertsSaving || !detail || (!content && !attachments.length && !options?.fileReferences?.length) || busy) return false
    setBusy(true); setError('')
    try { const updated = await api<ProjectDetail>(`/projects/${detail.id}/messages`, { method: 'POST', body: JSON.stringify({ content: content || '请参考引用文件和附件修改项目', enabled_tools: activeTools, build_tier: activeBuildTier, model: selectedModel, attachments: mediaPayload(attachments), element_references: options?.elementReferences || [], file_references: options?.fileReferences || [], expert_ids: detail.expert_ids || [] }) }); setDetail(updated); setDraft(''); await refresh(); return true }
    catch (e) { setError((e as Error).message); return false } finally { setBusy(false) }
  }
  const changeExpertIds = async (ids: string[]) => {
    if (!detail) { setHomeExpertIds(ids); return }
    if (expertsSaving) return
    const projectId = detail.id
    setExpertsSaving(true)
    try {
      const result = await api<{ expert_ids: string[] }>(`/projects/${projectId}/experts`, { method: 'PATCH', body: JSON.stringify({ expert_ids: ids }) })
      setDetail(current => current?.id === projectId ? { ...current, expert_ids: result.expert_ids } : current)
    } catch (e) { setError((e as Error).message) }
    finally { setExpertsSaving(false) }
  }
  const chooseExpert = (expert: Expert, prompt?: string) => {
    const ids = detail?.expert_ids || homeExpertIds
    if (!ids.includes(expert.id) && ids.length >= 3) { setError('一次最多选择 3 位专家，请先移除一位'); return }
    void changeExpertIds(ids.includes(expert.id) ? ids : [...ids, expert.id])
    setExpertDetails(null); setExpertPickerOpen(false)
    if (detail) { if (prompt) setDraft(prompt) }
    else { if (prompt) setPrefill(prompt); selectSection('home') }
  }
  const saveExpert = async (expert: Expert) => {
    try {
      const result = await api<{ saved: boolean }>(`/experts/${expert.id}/saved`, { method: 'PUT', body: JSON.stringify({ saved: !expert.saved }) })
      setExperts(items => items.map(item => item.id === expert.id ? { ...item, saved: result.saved } : item))
      setExpertDetails(current => current?.id === expert.id ? { ...current, saved: result.saved } : current)
    } catch (e) { setError((e as Error).message) }
  }
  const expertControl = <ExpertPicker experts={experts} value={detail?.expert_ids || homeExpertIds} onChange={ids => void changeExpertIds(ids)} open={expertPickerOpen} setOpen={setExpertPickerOpen} onDetails={setExpertDetails} onBrowse={() => selectSection('experts')} disabled={expertsSaving} />
  const removeProject = (id: string) => { const project = projects.find(item => item.id === id) || (detail?.id === id ? detail : null); if (project) { setDeleteError(''); setDeleteTarget(project) } }
  const confirmDeleteProject = async () => {
    if (!deleteTarget || deleteBusy) return
    setDeleteBusy(true); setDeleteError('')
    try {
      await api(`/projects/${deleteTarget.id}`, { method: 'DELETE' })
      setProjects(items => items.filter(item => item.id !== deleteTarget.id))
      setFavorites(items => items.filter(id => id !== deleteTarget.id))
      if (detail?.id === deleteTarget.id) selectSection('home')
      setDeleteTarget(null)
      void refresh().catch(e => setError(e.message))
    } catch (e) { setDeleteError((e as Error).message) }
    finally { setDeleteBusy(false) }
  }
  const renameProject = async (project: Project) => { setRenameTarget(project); setProjectAction('rename') }
  const patchProject = async (project: Project, patch: Partial<Project>) => {
    const next = await api<Project>(`/projects/${project.id}`, { method: 'PATCH', body: JSON.stringify(patch) })
    if (detail?.id === project.id) setDetail(current => current ? { ...current, ...next } : current)
    await refresh()
  }
  const favoriteProject = (project: Project) => { void patchProject(project, { favorite: !project.favorite }).catch(e => setError(e.message)) }

  const publishUrl = detail ? `${window.location.origin}${detail.publish_slug ? `/api/sites/${detail.publish_slug}` : `/api/public/${detail.id}`}` : ''
  const loadPublishedReleases = async (id: string) => {
    const result = await api<{ releases: PublishedRelease[] }>(`/projects/${id}/releases`)
    setPublishedReleases(result.releases)
  }
  const waitForPublication = async (id: string, releaseId: string, cancelled: () => boolean = () => false) => {
    for (;;) {
      if (cancelled()) return
      const result = await api<{ publication: PublishedRelease | null; published: boolean; active_release_id: string | null }>(`/projects/${id}/publication`)
      const release = result.publication
      if (!release || release.id !== releaseId) throw new Error('发布状态已变化，请查看发布版本')
      setPublishPhase(release.phase)
      if (release.status === 'failed') throw new Error(release.error || '发布失败，旧版本继续运行')
      if (release.status === 'active' && result.active_release_id === releaseId && result.published) break
      if (release.status === 'retired') throw new Error('此发布已停止或被替换')
      await new Promise(resolve => window.setTimeout(resolve, 1500))
    }
    const updated = await api<ProjectDetail>(`/projects/${id}`)
    setDetail(current => current?.id === id ? updated : current)
    await loadPublishedReleases(id)
  }
  useEffect(() => {
    if (modal === 'publish' && detail) void loadPublishedReleases(detail.id).catch(() => {})
  }, [modal, detail?.id])
  useEffect(() => {
    if (modal !== 'publish' || !detail) return
    const id = detail.id
    let cancelled = false
    setPublishVersionsLoading(true)
    void api<PublishVersion[]>(`/projects/${id}/versions`).then(versions => {
      if (!cancelled) setPublishVersions({ projectId: id, versions })
    }).catch((error: Error) => { if (!cancelled) { setPublishVersions(null); setPublishError(error.message) } })
      .finally(() => { if (!cancelled) setPublishVersionsLoading(false) })
    return () => { cancelled = true }
  }, [modal, detail?.id, detail?.status])
  useEffect(() => {
    if (!detail) return
    const id = detail.id
    let cancelled = false
    void api<{ publication: PublishedRelease | null }>(`/projects/${id}/publication`).then(async result => {
      if (cancelled || !result.publication || !['queued', 'packaging', 'building', 'starting'].includes(result.publication.status)) return
      setPublishBusy(true); setPublishSuccess(false); setPublishError(''); setModal('publish')
      try { await waitForPublication(id, result.publication.id, () => cancelled); if (!cancelled) setPublishSuccess(true) }
      catch (e) { if (!cancelled) setPublishError((e as Error).message) }
      finally { if (!cancelled) setPublishBusy(false) }
    }).catch(() => {})
    return () => { cancelled = true; setPublishBusy(false) }
  }, [detail?.id])
  const publishProject = async (version?: number) => {
    if (!detail || publishBusy) return
    setPublishBusy(true); setPublishError(''); setPublishSuccess(false); setModal('publish')
    try {
      const updated = await api<Project & { publication: PublishedRelease }>(`/projects/${detail.id}`, { method: 'PATCH', body: JSON.stringify({ published: true, ...(version ? { publish_version: version } : {}) }) })
      await waitForPublication(detail.id, updated.publication.id)
      setPublishSuccess(true)
      await refresh()
    } catch (e) { setPublishError((e as Error).message) }
    finally { setPublishBusy(false) }
  }
  const rollbackPublishedRelease = async (releaseId: string) => {
    if (!detail || publishBusy) return
    setPublishBusy(true); setPublishError(''); setPublishSuccess(false); setPublishPhase('正在恢复已验证的发布版本')
    try {
      await api(`/projects/${detail.id}/releases/${releaseId}/activate`, { method: 'POST' })
      const updated = await api<ProjectDetail>(`/projects/${detail.id}`)
      setDetail(current => current?.id === detail.id ? updated : current)
      await loadPublishedReleases(detail.id)
      setPublishSuccess(true)
      await refresh()
    } catch (e) { setPublishError((e as Error).message) }
    finally { setPublishBusy(false) }
  }
  const unpublishProject = async () => {
    if (!detail || publishBusy) return
    setPublishBusy(true); setPublishError('')
    try {
      const updated = await api<Project>(`/projects/${detail.id}`, { method: 'PATCH', body: JSON.stringify({ published: false }) })
      setDetail(current => current?.id === detail.id ? { ...current, published: updated.published } : current)
      setPublishSuccess(false)
      await refresh()
    } catch (e) { setPublishError((e as Error).message) }
    finally { setPublishBusy(false) }
  }
  const downloadProject = async (project: Project) => { try { const response = await authFetch(`/projects/${project.id}/archive`); if (!response.ok) throw new Error('下载失败'); const url = URL.createObjectURL(await response.blob()); const a = document.createElement('a'); a.href = url; a.download = `${project.title.replace(/[^\p{L}\p{N}-]+/gu, '-') || 'app'}.zip`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000) } catch (e) { setError((e as Error).message) } }
  const navItems: { key: Section; label: string; icon: LucideIcon }[] = [{ key: 'home', label: '首页', icon: Home }, { key: 'resources', label: '资源', icon: Compass }, { key: 'experts', label: '专家', icon: Sparkles }, { key: 'projects', label: '我的项目', icon: Square }]
  const discoveryPatterns: Record<string, RegExp> = {
    'E-commerce': /商店|商城|服饰|电商|商品/,
    '网站': /网站|网页|页面|主页|旅行|生日/,
    '游戏': /游戏|2048|贪吃蛇|消消乐/,
    '生产力': /工具|管理|任务|计划|日历/,
    '原型设计': /设计|原型|UI|方案/,
    '数据分析': /数据|分析|图|金融/,
  }
  const publicDiscovery = discoverData.find(item => item.href === path)
  const publishedDiscoveryId = path.split('?')[0].match(/^\/zh\/discover\/others\/([a-f\d-]{32,36})$/)?.[1]
  const templateProjects = publicProjects
    .filter(project => project.public_url && (project.cover_image_url || project.preview_html?.trim()))
    .filter((project, index, items) => items.findIndex(item => item.title === project.title) === index)
    .slice(0, 8)
  const templateCards = templateProjects.length ? templateProjects.map(project => <ProjectCard key={project.id} project={project} href={`/zh/discover/others/${project.id.replaceAll('-', '')}`} open={() => {}} />) : <div className="empty-card">暂无可用模板，公开发布项目后会自动展示</div>
  const visiblePublicProjects = publicProjects.filter(project => !favoriteOnly || favorites.includes(`/zh/discover/others/${project.id.replaceAll('-', '')}`))
    .filter(project => !discoveryPatterns[resourceCategory] || discoveryPatterns[resourceCategory].test(project.title))

  if (!authReady) return <main className="auth-screen"><div className="auth-loading">正在恢复会话…</div></main>
  if (authError) return <main className="auth-screen"><div className="auth-card" role="alert"><p>{authError}</p><button className="auth-submit" onClick={recoverAuth}>重试</button></div></main>
  if (publishedDiscoveryId && !publicDiscovery) {
    const href = `/zh/discover/others/${publishedDiscoveryId.replaceAll('-', '')}`
    return <PublishedProjectDetail key={publishedDiscoveryId} projectId={publishedDiscoveryId} user={user} onAuthenticated={setUser}
      saved={favorites.includes(href)} onSave={() => setFavorites(items => items.includes(href) ? items.filter(id => id !== href) : [...items, href])}
      onClone={clonePublicProject} onHome={() => selectSection('home')} onDiscover={() => selectSection('resources')}
      onPricing={() => { selectSection('home'); openSettings('plan') }} />
  }
  if (!user) return <AuthPage onAuthenticated={setUser} />

  return <div className={`app-layout ${sidebarOpen ? '' : 'sidebar-collapsed'} ${publicDiscovery ? 'public-detail-layout' : ''} ${detail ? 'project-layout' : ''}`}>
    <aside className="sidebar" inert={!!settingsPage || redeemOpen}>
      <div className="brand-row"><button className="brand" onClick={() => selectSection('home')}><span className="atoms-logo"><i /><i /><i /></span><b>Atoms</b></button><IconButton icon={PanelLeftClose} label="收起侧栏" onClick={() => setSidebarOpen(false)} /></div>
      <div className="relative"><button className="workspace-button" onClick={() => setWorkspaceOpen(!workspaceOpen)}><span className="workspace-initial">{account ? <AccountAvatar account={account} workspace /> : user.email.charAt(0).toUpperCase()}</span><span className="workspace-name">{account?.workspace_name || `${user.email.split('@')[0]}'s Atoms`}</span><ChevronDown size={15} /></button>{workspaceOpen && <div className="popover workspace-pop"><div className="menu-label">工作区</div><button onClick={() => setWorkspaceOpen(false)}><span className="workspace-initial">{user.email.charAt(0).toUpperCase()}</span> {account?.workspace_name || `${user.email.split('@')[0]}'s Atoms`} <Check size={15} /></button></div>}</div>
      <nav className="sidebar-nav">{navItems.map(item => <button key={item.key} className={(section === item.key && !detail) ? 'active' : ''} onClick={() => selectSection(item.key)}><item.icon size={17} strokeWidth={1.7} />{item.label}</button>)}</nav>
      <div className="sidebar-projects">{projects.length ? <><div className="sidebar-caption">最近</div>{projects.map(project => <RecentProject key={project.id} project={project} active={detail?.id === project.id} open={recentMenuId === project.id} onMenuChange={setRecentMenuId} onOpen={() => openProject(project.id)} onDelete={() => removeProject(project.id)} />)}</> : <p>还没有项目<br />点击“首页”开始。</p>}</div>
      <div className="sidebar-bottom"><button className="sidebar-card" onClick={() => openSettings('plan')}><span className="card-symbol"><Gift size={18} /></span><span><b>剩余积分</b><small>{account ? `${formatCredits(account.available_credits)} 剩余` : '正在加载…'}</small></span><ChevronRight size={16} /></button><div className="sidebar-footer">{account ? <AccountMenu account={account} open={profileOpen} onToggle={() => setProfileOpen(!profileOpen)} onClose={() => setProfileOpen(false)} onSettings={openSettings} onProfile={() => route('/zh/profile')} onRedeem={() => setRedeemOpen(true)} onTheme={value => { void saveAccount({ preferences: { theme: value } }).then(updateAccount).catch(e => setError(e.message)) }} onLogout={() => { void logoutSession().then(() => { setUser(null); setProjects([]); setDetail(null); route('/zh/dashboard') }).catch((error: Error) => setError(error.message)) }} /> : <button className="profile-avatar" aria-label="加载个人菜单">{user.email.charAt(0).toUpperCase()}</button>}<span /><IconButton icon={Settings2} label="设置" onClick={() => openSettings('profile')} /><IconButton icon={Bell} label="通知" onClick={() => openSettings('plan')} /></div></div>
    </aside>
    <div className="main-column" inert={!!settingsPage || redeemOpen}><header className="topbar">{!sidebarOpen && <IconButton icon={PanelLeftOpen} label="展开侧栏" onClick={() => setSidebarOpen(true)} />}<div className="topbar-spacer" />{detail && <div className="project-top-title">{detail.title}</div>}<div className="topbar-spacer" />{detail || account?.preferences.show_credits ? <button className="credits-pill" onClick={() => setModal('credits')}><span>◇</span> {formatCredits(account?.available_credits) || '0'}</button> : section === 'home' && !account?.preferences.show_credits ? null : <button className="personal-home" onClick={() => route('/zh/profile')}>我的个人主页</button>}<IconButton icon={Menu} label="更多" onClick={() => setModal('settings')} className="mobile-only" /></header>
      {error && <div className="error-banner"><span>{error}</span><button onClick={() => setError('')}><X size={15} /></button></div>}
      {detail ? <ChatWorkspace projectMenu={<ProjectMenu project={detail} account={account} onHome={() => selectSection('home')} onSettings={openSettings} onAction={action => { setRenameTarget(null); setProjectAction(action) }} onFavorite={() => favoriteProject(detail)} onTheme={value => { void saveAccount({ preferences: { theme: value } }).then(updateAccount).catch(e => setError(e.message)) }} />} expertControl={expertControl} toolsControl={toolsControl} tierControl={tierControl} onChooseExperts={() => setExpertPickerOpen(true)} experts={experts} key={detail.id} project={detail} setProject={project => {
        setDetail(current => current?.id === project.id ? { ...current, ...project } as ProjectDetail : current)
        setProjects(current => current.map(item => item.id === project.id ? { ...item, ...project } : item))
      }} modelControl={<ModelSelector models={models} value={selectedModel} onChange={setSelectedModel} />} draft={draft} setDraft={setDraft} sendMessage={sendMessage} busy={busy || expertsSaving} onHome={() => selectSection('home')} onRename={() => void renameProject(detail)} onDelete={() => void removeProject(detail.id)} publishBusy={publishBusy} onPublish={() => { setPublishSuccess(false); setPublishError(''); setModal('publish') }} onUnpublish={() => void unpublishProject()} onShare={() => setModal('share')} /> : section === 'experts' ? <ExpertsPage experts={experts} loading={expertsLoading} error={expertsError} onRetry={() => void loadExperts()} onDetails={setExpertDetails} onChoose={chooseExpert} onSave={expert => void saveExpert(expert)} /> : path.split('?')[0] === '/zh/profile' && account ? <main className="personal-profile"><div className="personal-profile-cover"><AccountAvatar account={account} /><button onClick={() => openSettings('profile')}>编辑个人资料</button></div><h1>{account.display_name}</h1><div className="personal-profile-stats"><span>{favorites.length}<small>保存</small></span><span>0<small>查看次数</small></span></div><div className="sub-tabs">{['公开项目', '已保存'].map(tab => <button className={profileTab === tab ? 'active' : ''} key={tab} onClick={() => setProfileTab(tab)}>{tab}</button>)}</div>{profileTab === '公开项目' ? <><h2>其他项目</h2><div className="card-grid">{projects.map(project => <ProfileProjectCard key={project.id} project={project} open={() => openProject(project.id)} />)}</div>{!projects.length && <div className="profile-empty">还没有项目</div>}</> : <div className="card-grid">{projects.filter(p => favorites.includes(p.id)).map(project => <ProfileProjectCard key={project.id} project={project} open={() => openProject(project.id)} />)}{publicProjects.filter(p => favorites.includes(`/zh/discover/others/${p.id.replaceAll('-', '')}`)).map(project => <ProjectCard key={project.id} project={project} href={`/zh/discover/others/${project.id.replaceAll('-', '')}`} open={() => {}} />)}{discoverData.filter(p => favorites.includes(p.href)).map(item => <DiscoveryCard key={item.href} item={item} open={() => route(item.href)} />)}{!favorites.length && <div className="profile-empty">还没有保存的项目</div>}</div>}</main> : publicDiscovery ? <DiscoveryDetail item={publicDiscovery} saved={favorites.includes(publicDiscovery.href)} onSave={() => setFavorites(items => items.includes(publicDiscovery.href) ? items.filter(id => id !== publicDiscovery.href) : [...items, publicDiscovery.href])} onRemix={() => { setPrefill(`参考“${publicDiscovery.title}”，创建一个类似的可运行网页应用。`); selectSection('home') }} onHome={() => selectSection('resources')} /> : section === 'home' ? (
        <main className="home-page">
          <div className="hero-zone">
            <button className="notice" onClick={() => setModal('credits')}>Notice <span>·</span> Atoms Update: Visual Editor and Cloud Connector String <X size={14} /></button>
            <AgentAvatars />
            <h1>你想创造什么，{account?.display_name || user.email.split('@')[0]}？</h1>
            <PromptBox expertControl={expertControl} toolsControl={toolsControl} tierControl={tierControl} onChooseExperts={() => setExpertPickerOpen(true)} onSubmit={createProject} busy={busy} models={models} selectedModel={selectedModel} onModelChange={setSelectedModel} initialText={prefill} />
          </div>
          <div className="discovery-panel">
            <div className="discovery-heading">
              <div className="discovery-tabs">
                <button className={discoverTab === 'discover' ? 'active' : ''} onClick={() => setDiscoverTab('discover')}>发现</button>
                <button className={discoverTab === 'projects' ? 'active' : ''} onClick={() => setDiscoverTab('projects')}>我的项目</button>
                <button className={discoverTab === 'templates' ? 'active' : ''} onClick={() => setDiscoverTab('templates')}>模板</button>
              </div>
              <button className="view-all" onClick={() => selectSection(discoverTab === 'projects' ? 'projects' : discoverTab === 'templates' ? 'templates' : 'resources')}>查看全部 <ChevronRight size={16} /></button>
            </div>
            {discoverTab === 'projects' ? <div className="card-grid">{projects.length ? projects.map(project => <ProjectCard key={project.id} project={project} open={() => openProject(project.id)} />) : <div className="empty-card">还没有项目</div>}</div>
              : discoverTab === 'templates' ? <div className="card-grid">{templateCards}</div>
              : <div className="card-grid">{publicProjects.length ? publicProjects.slice(0, 18).map(project => <ProjectCard key={project.id} project={project} href={`/zh/discover/others/${project.id.replaceAll('-', '')}`} open={() => {}} />) : <div className="empty-card">暂无公开项目</div>}</div>}
          </div>
        </main>
      ) : section === 'projects' ? (
        <main className="my-projects-page">
          <h1>我的项目</h1>
          <div className="sub-tabs"><button className={!favoriteOnly ? 'active' : ''} onClick={() => setFavoriteOnly(false)}>全部</button><button className={favoriteOnly ? 'active' : ''} onClick={() => setFavoriteOnly(true)}>已收藏</button></div>
          <div className="my-projects-grid">{projects.filter(project => !favoriteOnly || favorites.includes(project.id)).length ? projects.filter(project => !favoriteOnly || favorites.includes(project.id)).map(project => <div className={`project-card ${projectMenu === project.id ? 'menu-open' : ''}`} key={project.id}>
            <button className="project-thumb" onClick={() => openProject(project.id)}><ProjectThumbnail project={project} /></button>
            <div className="project-card-footer"><button onClick={() => openProject(project.id)}><strong>{project.title}</strong><small>{new Date(project.updated_at).toLocaleDateString('zh-CN')} · {project.published ? '已发布' : '草稿'}</small></button><button className="favorite-button" onClick={() => favoriteProject(project)}>{favorites.includes(project.id) ? '★' : '☆'}</button><div className="relative"><IconButton icon={MoreHorizontal} label="项目操作" onClick={() => setProjectMenu(projectMenu === project.id ? null : project.id)} />{projectMenu === project.id && <div className="popover project-actions"><button onClick={() => { void renameProject(project); setProjectMenu(null) }}>重命名</button><button onClick={() => { void downloadProject(project); setProjectMenu(null) }}>下载</button><button onClick={() => { void removeProject(project.id); setProjectMenu(null) }}>删除</button></div>}</div></div>
          </div>) : <div className="my-empty"><span className="empty-mascot">◕</span><p>还没有项目</p></div>}</div>
        </main>
      ) : (
        <main className="resources-page">
          <h1>资源</h1>
          <div className="resource-switch"><button className={resourceMode === 'discover' ? 'active' : ''} onClick={() => setResourceMode('discover')}>发现</button><button className={resourceMode === 'templates' ? 'active' : ''} onClick={() => setResourceMode('templates')}>模板</button></div>
          {resourceMode === 'discover' ? <><div className="resource-filters">{['全部','Claude Fable 5','E-commerce','网站','游戏','生产力','原型设计','数据分析'].map(name => <button key={name} className={resourceCategory === name ? 'active' : ''} onClick={() => setResourceCategory(name)}>{name}</button>)}<ChevronRight size={16} /><button className={favoriteOnly ? 'active' : ''} onClick={() => setFavoriteOnly(!favoriteOnly)}>♧ 已保存</button><button className={resourceCategory === '最新' ? 'active' : ''} onClick={() => setResourceCategory('最新')}>◷ 最新</button></div><div className="card-grid">{visiblePublicProjects.length ? visiblePublicProjects.map(project => <ProjectCard key={project.id} project={project} href={`/zh/discover/others/${project.id.replaceAll('-', '')}`} open={() => {}} />) : <div className="empty-card">暂无相关作品</div>}</div></> : <div className="card-grid">{templateCards}</div>}
        </main>
      )}
    </div>
    {deleteTarget && <DeleteProjectDialog title={deleteTarget.title} busy={deleteBusy} error={deleteError} onCancel={() => { if (!deleteBusy) setDeleteTarget(null) }} onConfirm={() => void confirmDeleteProject()} />}
    {modal === 'publish' && detail ? <PublishDialog key={detail.id} url={publishUrl} published={detail.published} busy={publishBusy} success={publishSuccess && !publishBusy} canPublish={!['deleting', 'cloning'].includes(detail.status)} versions={publishVersions?.projectId === detail.id ? publishVersions.versions : []} versionsLoading={publishVersionsLoading} error={publishError} phase={publishPhase} releases={publishedReleases} onRollback={id => void rollbackPublishedRelease(id)} onPublish={version => void publishProject(version)} onUnpublish={() => void unpublishProject()} onShare={() => setModal('share')} onClose={() => { if (!publishBusy) setModal(null) }} /> : modal && <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) setModal(null) }}><div className="modal"><div className="modal-top"><h2>{modal === 'settings' ? '设置' : modal === 'credits' ? '积分与计划' : modal === 'share' ? '分享项目' : '连接你的工具'}</h2><IconButton icon={X} label="关闭" onClick={() => setModal(null)} /></div>{modal === 'share' ? <div className="modal-body"><p>{detail?.published ? '项目已发布。复制链接即可分享当前版本。' : '发布项目后即可获得公开访问链接。'}</p>{detail?.published ? <><input className="share-link" readOnly value={publishUrl} /><button className="primary-button" onClick={() => { void copyLink(publishUrl).then(() => setShareCopyError('')).catch((error: Error) => setShareCopyError(error.message)) }}>复制链接</button>{shareCopyError && <p className="account-error" role="alert">{shareCopyError}</p>}</> : <button className="primary-button" onClick={() => void publishProject()}>发布项目</button>}<button className="secondary-button" onClick={() => { if (detail) downloadProject(detail) }}><Download size={17} /> 下载项目 ZIP</button></div> : null}</div></div>}
    {settingsPage && ['projectGeneral', 'domain'].includes(settingsPage) && detail && <ProjectSettings project={detail} account={account} page={settingsPage} onNavigate={openSettings} onClose={closeSettings} onAction={setProjectAction} onDelete={() => removeProject(detail.id)} onUnpublish={() => patchProject(detail, { published: false })} onBadge={value => patchProject(detail, { remove_badge: value })} onSlug={value => patchProject(detail, { publish_slug: value })} publicUrl={publishUrl} />}
    {projectAction && (renameTarget || detail) && <ProjectDialog key={`${projectAction}-${(renameTarget || detail)!.id}`} action={projectAction} project={(renameTarget || detail)!} account={account} onClose={() => { setProjectAction(null); setRenameTarget(null) }} onRename={name => patchProject((renameTarget || detail)!, { title: name })} onClone={async (title, copy_database) => { const next = await api<Project>(`/projects/${(renameTarget || detail)!.id}/clone`, { method: 'POST', body: JSON.stringify({ title, copy_database }) }); closeSettings(); await refresh(); openProject(next.id) }} onPrivacy={value => patchProject((renameTarget || detail)!, { visibility: value })} onRedeem={() => { setProjectAction(null); setRedeemOpen(true) }} onChooseClone={() => setProjectAction('clone')} />}
    {settingsPage && !['projectGeneral', 'domain'].includes(settingsPage) && account && <AccountSettings account={account} page={settingsPage} models={models} onChange={updateAccount} onNavigate={openSettings} onClose={closeSettings} onProfile={() => route('/zh/profile')} onProject={id => { openProject(id); void refresh() }} />}
    {expertDetails && <ExpertDetails expert={expertDetails} onClose={() => setExpertDetails(null)} onChoose={chooseExpert} onSave={expert => void saveExpert(expert)} />}
    {redeemOpen && <RedeemDialog onClose={() => setRedeemOpen(false)} onChange={updateAccount} />}
  </div>
}

function ProjectThumbnail({ project }: { project: Project }) {
  const [url, setUrl] = useState('')
  useEffect(() => {
    if (project.cover_image_url || project.public_url || !project.preview_html) { setUrl(project.public_url || ''); return }
    let disposed = false
    void authFetch(`/projects/${project.id}/preview-token`, { method: 'POST' })
      .then(response => response.ok ? response.json() as Promise<{ url: string }> : Promise.reject(new Error('预览不可用')))
      .then(data => { if (!disposed) setUrl(data.url) })
      .catch(() => {})
    return () => { disposed = true }
  }, [project.id, project.public_url, project.cover_image_url, Boolean(project.preview_html)])
  if (project.cover_image_url) return <img className="project-thumbnail-image" src={`${project.cover_image_url}?v=${encodeURIComponent(project.updated_at)}`} alt={`${project.title} 首页预览`} loading="lazy" />
  return url ? <iframe title={project.title} src={`${url}${url.includes('?') ? '&' : '?'}__atoms_thumbnail=1`} tabIndex={-1} inert loading="lazy" sandbox="allow-scripts" /> : project.preview_html ? <span className="project-thumb-empty">正在加载预览…</span> : <span className="project-thumb-empty project-art-placeholder"><b>{project.kind === 'App' ? '▦' : '▤'}</b><strong>{project.title}</strong><small>{project.status === 'running' || project.status === 'queued' ? '正在构建…' : '成果文件项目'}</small></span>
}

function ProfileProjectCard({ project, open }: { project: Project; open: () => void }) {
  return <button className="profile-project-card" onClick={open}><div className="profile-project-thumbnail"><ProjectThumbnail project={project} />{project.published && <span className="profile-published"><Globe2 size={12} />已发布</span>}</div><div className="profile-project-info"><strong>{project.title}</strong><small>{new Date(project.updated_at).toLocaleDateString('zh-CN')}</small></div></button>
}

function ProjectCard({ project, open, href }: { project: Project; open: () => void; href?: string }) {
  const content = <><div className={`card-art project-art ${project.cover_image_url ? 'has-project-cover' : ''}`}><ProjectThumbnail project={project} /></div><div className="idea-info"><small>{project.kind} · {project.published ? '已发布' : '草稿'}</small><strong>{project.title}</strong></div></>
  return href ? <a className="idea-card project-card-discovery" href={href} target="_blank" rel="noopener noreferrer">{content}</a> : <button className="idea-card project-card-discovery" onClick={open}>{content}</button>
}

function DiscoveryCard({ item, open }: { item: Discovery; open: () => void }) {
  return <button className="discovery-card" onClick={open}>
    <img className="discovery-cover" src={item.thumbnail} alt="" loading="lazy" />
    <div className="discovery-info"><span className="discovery-avatar">{item.avatar ? <img src={item.avatar} alt="" loading="lazy" /> : item.author.charAt(0).toUpperCase()}</span><span className="discovery-meta"><strong>{item.title}</strong><small>{item.author} · ◉ {item.views}</small></span></div>
  </button>
}

function DiscoveryDetail({ item, saved, onSave, onRemix, onHome }: { item: Discovery; saved: boolean; onSave: () => void; onRemix: () => void; onHome: () => void }) {
  return <div className="public-detail">
    <header className="public-header"><button className="public-brand" onClick={onHome}><span className="atoms-logo"><i /><i /><i /></span><strong>Atoms</strong></button><nav><button onClick={onHome}>资源 <ChevronDown size={14} /></button><button onClick={onHome}>社区</button><button onClick={onHome}>定价</button></nav><button className="enter-atoms" onClick={onHome}>进入 Atoms</button></header>
    <main className="public-main"><div className="breadcrumbs"><button onClick={onHome}>发现</button><ChevronRight size={15} /><span>其他</span></div><div className="public-title-row"><div><h1>{item.title}</h1><p>◉ {item.views}　♧ 0</p></div><div className="public-actions"><IconButton icon={ExternalLink} label="查看原作品" onClick={() => window.open(`https://atoms.dev${item.href}`, '_blank', 'noopener,noreferrer')} /><IconButton icon={Share2} label="复制链接" onClick={() => void navigator.clipboard.writeText(window.location.href)} /><button className="public-save" onClick={onSave}>{saved ? '◆' : '♧'}</button><button className="remix-button" onClick={onRemix}>克隆</button></div></div><div className="public-preview"><img src={item.thumbnail} alt={`${item.title} 页面预览`} /></div></main>
  </div>
}

export default App
