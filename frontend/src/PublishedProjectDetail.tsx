import { useEffect, useRef, useState } from 'react'
import { Bookmark, Check, ChevronDown, ChevronRight, ExternalLink, Eye, Link, Shuffle, X } from 'lucide-react'
import AuthPage from './AuthPage'
import { restoreSession, type AuthUser } from './auth'
import { copyLink } from './clipboard'
import './published-project-detail.css'

type PublishedProject = {
  id: string; title: string; kind: string; mode: string; author: string; avatar: string;
  public_url: string; created_at: string; updated_at: string; discover_views: number; discover_clones: number;
}

type Props = {
  projectId: string; user: AuthUser | null; saved: boolean; onSave: () => void;
  onAuthenticated: (user: AuthUser) => void; onClone: (id: string, title: string) => Promise<void>;
  onHome: () => void; onDiscover: () => void; onPricing: () => void;
}

export default function PublishedProjectDetail({ projectId, user, saved, onSave, onAuthenticated, onClone, onHome, onDiscover, onPricing }: Props) {
  const [project, setProject] = useState<PublishedProject | null>(null)
  const [loadError, setLoadError] = useState('')
  const [copyMessage, setCopyMessage] = useState('')
  const [copied, setCopied] = useState(false)
  const [cloneOpen, setCloneOpen] = useState(false)
  const [cloneTitle, setCloneTitle] = useState('')
  const [cloneBusy, setCloneBusy] = useState(false)
  const [cloneError, setCloneError] = useState('')
  const [loginOpen, setLoginOpen] = useState(false)
  const [authChecking, setAuthChecking] = useState(false)
  const [authError, setAuthError] = useState('')
  const titleInput = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const controller = new AbortController()
    void fetch(`/api/discover/projects/${projectId}`, { signal: controller.signal }).then(async response => {
      if (!response.ok) throw new Error(response.status === 404 ? '项目未公开发布或已下架' : '项目加载失败，请刷新重试')
      return response.json() as Promise<PublishedProject>
    }).then(data => { setProject(data); setCloneTitle(`${data.title} · 副本`.slice(0, 100)) })
      .catch(error => { if (!controller.signal.aborted) setLoadError((error as Error).message) })
    return () => controller.abort()
  }, [projectId])

  useEffect(() => {
    if (!project) return
    const previous = document.title
    document.title = `${project.title} · Atoms`
    const key = `atoms:discovery-view:${project.id}`
    let counted = false
    try { counted = sessionStorage.getItem(key) === '1'; sessionStorage.setItem(key, '1') } catch { /* Storage can be disabled. */ }
    if (!counted) void fetch(`/api/discover/projects/${project.id}/view`, { method: 'POST' })
      .then(response => response.ok ? response.json() as Promise<{ discover_views: number }> : null)
      .then(data => { if (data) setProject(current => current && current.id === project.id ? { ...current, discover_views: data.discover_views } : current) })
      .catch(() => { try { sessionStorage.removeItem(key) } catch { /* Ignore storage failures. */ } })
    return () => { document.title = previous }
  }, [project?.id])

  useEffect(() => {
    if (!cloneOpen && !loginOpen) return
    titleInput.current?.focus()
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !cloneBusy) { setCloneOpen(false); setLoginOpen(false) }
    }
    window.addEventListener('keydown', close)
    return () => window.removeEventListener('keydown', close)
  }, [cloneOpen, loginOpen, cloneBusy])

  useEffect(() => {
    if (!user) { setCloneOpen(false); return }
    if (user && loginOpen) { setLoginOpen(false); setCloneOpen(true) }
  }, [user, loginOpen])

  const beginClone = async () => {
    setCloneError('')
    setAuthError('')
    if (user) { setCloneOpen(true); return }
    setAuthChecking(true)
    try {
      const account = await restoreSession()
      if (account) { onAuthenticated(account); setCloneOpen(true) }
      else setLoginOpen(true)
    } catch (error) { setAuthError((error as Error).message || '暂时无法确认登录状态，请重试') }
    finally { setAuthChecking(false) }
  }
  const copy = async () => {
    try { await copyLink(window.location.href); setCopied(true); setCopyMessage('链接已复制') }
    catch (error) { setCopied(false); setCopyMessage((error as Error).message) }
  }

  return <div className="published-discovery-page">
    <header className="public-header">
      <button className="public-brand" onClick={onHome}><span className="atoms-logo"><i /><i /><i /></span><strong>Atoms</strong></button>
      <nav aria-label="公开项目导航"><button onClick={onDiscover}>资源 <ChevronDown size={14} /></button><button onClick={onDiscover}>社区</button><button onClick={onPricing}>定价</button></nav>
      <button className="enter-atoms" onClick={onHome}>进入 Atoms</button>
    </header>
    <main className="public-main">
      <div className="breadcrumbs"><button onClick={onDiscover}>发现</button><ChevronRight size={14} /><button onClick={onDiscover}>其他</button></div>
      {loadError ? <div className="published-discovery-empty" role="alert"><h1>{loadError}</h1><button onClick={onDiscover}>返回发现</button></div> : !project ? <div className="published-discovery-empty" role="status">正在加载项目…</div> : <>
        <div className="public-title-row">
          <div><h1>{project.title}</h1><p className="published-discovery-stats"><span title="浏览次数"><Eye size={15} />{project.discover_views}</span><span title="克隆次数"><Shuffle size={15} />{project.discover_clones}</span></p></div>
          <div className="public-actions">
            <a className="icon-button" title="在浏览器中打开" aria-label="在浏览器中打开" href={project.public_url} target="_blank" rel="noopener noreferrer"><ExternalLink size={16} /></a>
            <button className="icon-button" title="复制链接" aria-label="复制链接" onClick={() => void copy()}>{copied ? <Check size={16} /> : <Link size={16} />}</button>
            <button className={`public-save ${saved ? 'is-saved' : ''}`} title={saved ? '取消保存' : '保存'} aria-label={saved ? '取消保存' : '保存'} aria-pressed={saved} onClick={onSave}><Bookmark size={16} fill={saved ? 'currentColor' : 'none'} /></button>
            <button className="remix-button" disabled={authChecking} onClick={() => void beginClone()}>{authChecking ? '正在确认登录…' : '克隆'}</button>
          </div>
        </div>
        {authError && <p className="published-copy-message failed" role="alert">{authError}</p>}
        {copyMessage && <div className={`published-copy-message ${copied ? '' : 'failed'}`} role="status">{copyMessage}{!copied && <input aria-label="手动复制项目详情链接" readOnly value={window.location.href} onFocus={event => event.target.select()} />}</div>}
        <div className="public-preview published-discovery-preview"><iframe title={`${project.title} 项目预览`} src={`${project.public_url}?__atoms_frame=1`} sandbox="allow-scripts allow-forms allow-modals" /></div>
        <div className="published-discovery-info">
          <section><h2>创作者</h2><div className="published-discovery-author">{project.avatar ? <img src={project.avatar} alt={`${project.author} 头像`} /> : <span>{project.author.slice(0, 1).toUpperCase()}</span>}<strong>{project.author}</strong></div></section>
          <section><h2>关于</h2><p>{project.mode === 'Goal' ? '目标模式' : '团队模式'}</p><small>{new Date(project.updated_at).toLocaleDateString('zh-CN')} · {project.kind} 项目</small></section>
        </div>
      </>}
    </main>
    <footer className="published-discovery-footer"><strong>Atoms</strong><span>把想法变成可运行的作品</span><button onClick={onDiscover}>发现更多项目 <ChevronRight size={14} /></button></footer>
    {cloneOpen && project && <div className="published-clone-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !cloneBusy) setCloneOpen(false) }}><section className="published-clone-dialog" role="dialog" aria-modal="true" aria-labelledby="public-clone-title">
      <header><h2 id="public-clone-title">从当前版本重新制作</h2><button aria-label="关闭克隆弹窗" disabled={cloneBusy} onClick={() => setCloneOpen(false)}><X size={18} /></button></header>
      <p>获取相同版本并在新项目中继续编辑。</p>
      <form onSubmit={event => { event.preventDefault(); if (cloneBusy || !cloneTitle.trim()) return; setCloneBusy(true); setCloneError(''); void onClone(project.id, cloneTitle.trim()).catch(error => setCloneError((error as Error).message)).finally(() => setCloneBusy(false)) }}>
        <label>项目名称<input ref={titleInput} maxLength={100} value={cloneTitle} disabled={cloneBusy} onChange={event => setCloneTitle(event.target.value)} required /></label>
        {cloneError && <p className="published-clone-error" role="alert">{cloneError}</p>}
        <footer><button type="button" disabled={cloneBusy} onClick={() => setCloneOpen(false)}>取消</button><button className="remix-button" disabled={cloneBusy || !cloneTitle.trim()}>{cloneBusy ? '正在克隆…' : '克隆'}</button></footer>
      </form>
    </section></div>}
    {loginOpen && <div className="published-clone-backdrop"><section className="published-login-dialog" role="dialog" aria-modal="true" aria-label="登录后克隆项目"><button className="published-login-close" aria-label="关闭登录" onClick={() => setLoginOpen(false)}><X size={18} /></button><AuthPage onAuthenticated={account => { onAuthenticated(account); setLoginOpen(false); setCloneOpen(true) }} /></section></div>}
  </div>
}
