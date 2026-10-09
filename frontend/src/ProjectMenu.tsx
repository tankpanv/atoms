import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, ArrowUpRight, ChevronDown, ChevronRight, Copy, Gift, HelpCircle, Info, Moon, Pencil, Plug, Settings, Shuffle, Star, Sun, X } from 'lucide-react'
import { AccountAvatar } from './AccountMenu'
import { accountApi, type Account } from './account'
import { formatCredits } from './numberFormat'
import './project-menu.css'
import ProjectDomains from './ProjectDomains'

export type MenuProject = { publish_slug?: string; id: string; title: string; favorite?: boolean; visibility?: string; remove_badge?: boolean; published: boolean; created_at: string; updated_at: string; messages?: unknown[]; status: string; model: string }
export type ProjectAction = 'rename' | 'clone' | 'details' | 'credits' | 'help'
export function ProjectMenu({ project, account, onHome, onSettings, onAction, onFavorite, onTheme }: { project: MenuProject; account: Account | null; onHome: () => void; onSettings: (page: string) => void; onAction: (action: ProjectAction) => void; onFavorite: () => void; onTheme: (theme: string) => void }) {
  const [open, setOpen] = useState(false)
  const [appearance, setAppearance] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const close = () => { setOpen(false); setAppearance(false) }
    const outside = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) close()
    }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') close() }
    document.addEventListener('pointerdown', outside, true)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('pointerdown', outside, true)
      document.removeEventListener('keydown', escape)
    }
  }, [open])
  const act = (fn: () => void) => { setOpen(false); setAppearance(false); fn() }
  return <div className={`project-name-anchor ${open ? 'menu-open' : ''}`} ref={ref}><button className="build-title" aria-label="项目菜单" aria-expanded={open} onClick={() => { setOpen(!open); setAppearance(false) }}>{project.title}<ChevronDown size={15} /></button>{open && <><div className="project-menu-dismiss" aria-hidden="true" onPointerDown={() => { setOpen(false); setAppearance(false) }} /><div className="project-name-menu" role="menu" aria-label="项目操作">
    <button role="menuitem" onClick={() => act(onHome)}><ArrowLeft size={19} />前往仪表板</button><hr />
    {account && <><div className="project-workspace"><AccountAvatar account={account} /><strong>{account.workspace_name}</strong><span className="project-plan">{account.plan}</span></div><div className="project-credit-card"><div><button onClick={() => act(() => onSettings('plan'))}>剩余积分 <ArrowUpRight size={17} /></button><button className="project-plan" onClick={() => act(() => onSettings('plan'))}>升级</button></div><div><progress max={Math.max(Number(account.plan_credits) + 25, Number(account.credits), 1)} value={Math.max(0,Number(account.available_credits))} /><strong>{formatCredits(account.available_credits)} 剩余</strong></div></div></>}
    <button role="menuitem" onClick={() => act(() => onAction('credits'))}><Gift size={20} />获取免费积分</button><hr />
    <button role="menuitem" onClick={() => act(() => onSettings('projectGeneral'))}><Settings size={20} />项目设置</button>
    <button role="menuitem" onClick={() => act(() => onSettings('connectors'))}><Plug size={20} />连接器</button>
    <button role="menuitem" onClick={() => act(() => onAction('clone'))}><Shuffle size={20} />克隆此项目</button>
    <button role="menuitem" onClick={() => act(() => onAction('rename'))}><Pencil size={20} />重命名项目</button>
    <button role="menuitem" onClick={() => act(onFavorite)}><Star size={20} fill={project.favorite ? 'currentColor' : 'none'} />{project.favorite ? '取消收藏项目' : '收藏项目'}</button>
    <button role="menuitem" onClick={() => act(() => onAction('details'))}><Info size={20} />详情</button><hr />
    <div className="project-appearance"><button role="menuitem" aria-expanded={appearance} onClick={() => setAppearance(!appearance)} onMouseEnter={() => setAppearance(true)}><Moon size={20} />外观<ChevronRight size={18} /></button>{appearance && <div className="project-theme-options" role="menu" aria-label="外观">{[['light','浅色'],['dark','暗色'],['system','系统']].map(([value,label]) => <button role="menuitemradio" aria-checked={account?.preferences.theme === value} key={value} onClick={() => act(() => onTheme(value))}>{value === 'light' ? <Sun size={17} /> : <Moon size={17} />}{label}{account?.preferences.theme === value && <span>✓</span>}</button>)}</div>}</div>
    <button role="menuitem" onClick={() => act(() => onAction('help'))}><HelpCircle size={20} />帮助中心<ArrowUpRight size={18} /></button>
  </div></>}</div>
}

export function ProjectDialog({ action, project, account, onClose, onRename, onClone, onPrivacy, onRedeem, onChooseClone }: { action: ProjectAction; project: MenuProject; account: Account | null; onClose: () => void; onRename: (name: string) => Promise<void>; onClone: (name: string, database: boolean) => Promise<void>; onPrivacy: (value: string) => Promise<void>; onRedeem: () => void; onChooseClone: () => void }) {
  const [name, setName] = useState(action === 'clone' ? `${project.title} · 副本`.slice(0,100) : project.title)
  const [database, setDatabase] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)
  const [referral, setReferral] = useState<{ code: string; rewarded: number } | null>(null)
  useEffect(() => { if (action === 'credits') void accountApi<{ code: string; rewarded: number }>('/referrals').then(setReferral).catch(e => setError(e.message)) }, [action])
  const run = async (operation: () => Promise<void>) => { setBusy(true); setError(''); try { await operation() } catch (e) { setError((e as Error).message) } finally { setBusy(false) } }
  useEffect(() => { const escape = (event: KeyboardEvent) => { if (event.key === 'Escape' && !busy) onClose() }; document.addEventListener('keydown', escape); return () => document.removeEventListener('keydown', escape) }, [busy, onClose])
  const heading = { rename:'重命名项目', clone:'从当前版本重新制作', details:'Project 详细信息', credits:'获取免费积分', help:'帮助中心' }[action]
  return <div className="account-dialog-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose() }}><section className="project-dialog" role="dialog" aria-modal="true" aria-label={heading}><button className="account-dialog-close" disabled={busy} aria-label="关闭项目弹窗" onClick={onClose}><X /></button><h2>{heading}</h2>
    {(action === 'rename' || action === 'clone') && <form onSubmit={event => { event.preventDefault(); void run(async () => { if (action === 'rename') await onRename(name.trim()); else await onClone(name.trim(),database); onClose() }) }}><p>{action === 'rename' ? '更新此项目在你的工作区中的显示方式。' : '获取当前源码，在独立项目中继续编辑。'}</p><label className="project-field">{action === 'rename' ? '应用名称' : '项目名称'}<small>{name.length}/100</small><input autoFocus maxLength={100} aria-label="项目名称" value={name} onChange={e => setName(e.target.value)} /></label>{action === 'clone' ? <label className="project-copy-data"><input type="checkbox" checked={database} onChange={e => setDatabase(e.target.checked)} />复制数据库数据<small>数据库结构始终复制；账号凭据与运行环境独立。</small></label> : <p className="project-muted">最多 100 个字符。允许使用空格和特殊字符。</p>}{error && <p role="alert" className="account-error">{error}</p>}<footer><button type="button" className="account-connect" disabled={busy} onClick={onClose}>取消</button><button className="account-primary" disabled={busy || !name.trim()}>{busy ? '处理中…' : action === 'rename' ? '保存' : '克隆'}</button></footer></form>}
    {action === 'details' && <><div className="project-detail-row"><strong>隐私</strong><select aria-label="项目隐私" value={project.visibility || 'public'} disabled={busy} onChange={e => void run(() => onPrivacy(e.target.value))}><option value="public">公开</option><option value="private">私有</option></select></div><p className="project-muted">{project.visibility === 'private' ? '仅登录的项目所有者可以访问；公开发布链接已停止对外访问。' : '发布后可通过公开链接访问。'}</p><dl className="project-details"><dt>创建于</dt><dd>{new Date(project.created_at).toLocaleString('zh-CN')}</dd><dt>最后更新</dt><dd>{new Date(project.updated_at).toLocaleString('zh-CN')}</dd><dt>消息数量</dt><dd>{project.messages?.length ?? 0}</dd><dt>模型</dt><dd>{project.model}</dd><dt>项目 ID</dt><dd>{project.id}</dd></dl><button className="account-connect" onClick={onChooseClone}><Shuffle size={16} />克隆此项目</button> <button className="account-connect" onClick={() => void run(async () => { await navigator.clipboard.writeText(`${location.origin}/zh/chat/${project.id}`); setCopied(true) })}><Copy size={16} />{copied ? '已复制' : '复制链接'}</button>{error && <p role="alert" className="account-error">{error}</p>}</>}
    {action === 'credits' && <><h3>分享并赚取积分</h3><p>分享邀请链接，朋友注册并发送第一条消息可获得 10 积分；朋友首次发布项目后，你可获得 10 积分，每月最多 100 积分。</p><p>本月已获得 {referral?.rewarded ?? 0} 次邀请奖励（最多 10 次）</p><button className="account-primary" disabled={!referral} onClick={() => void run(async () => { await navigator.clipboard.writeText(`${location.origin}/zh/dashboard?invite=${referral!.code}`); setCopied(true) })}>{copied ? '已复制邀请链接' : '复制邀请链接'}</button>{error && <p role="alert" className="account-error">{error}</p>}<p>每天自动领取 15 积分，每月最多 25 积分。登录时自动到账，可在积分账单查看真实记录。</p><div className="project-credit-card">本月已领取：{formatCredits(account?.free_granted)} / 25<br />当前可用：{formatCredits(account?.available_credits)} 积分</div><button className="account-primary" onClick={onRedeem}>兑换积分码</button></>}
    {action === 'help' && <div className="project-help"><h3>构建与预览</h3><p>在对话框描述需求，选择模型与专家后发送。构建完成后，在应用查看器启动并预览应用。</p><h3>发布与分享</h3><p>点击右上角发布按钮，资源上传到项目专属存储后可复制公开链接。设为私有会关闭公开访问。</p><h3>克隆与连接器</h3><p>克隆复制源码与独立数据库结构，可选择复制数据。进入连接器管理 GitHub 与 PostgreSQL，继续描述需求即可编辑新项目。</p><h3>积分</h3><p>套餐与积分页可查看充值、支付与模型使用扣费记录；模拟支付完成后积分真实到账。</p></div>}
  </section></div>
}

export function ProjectSettings({ project, account, page, onNavigate, onClose, onAction, onDelete, onUnpublish, onBadge, onSlug, publicUrl }: { project: MenuProject; account: Account | null; page: string; onNavigate: (page: string) => void; onClose: () => void; onAction: (action: ProjectAction) => void; onDelete: () => void; onUnpublish: () => Promise<void>; onBadge: (value: boolean) => Promise<void>; onSlug: (value: string) => Promise<void>; publicUrl: string }) {
  const [editingUrl,setEditingUrl] = useState(false)
  const [slug,setSlug] = useState(project.publish_slug || project.id.replaceAll('-','').slice(0,12))
  const [busy,setBusy] = useState(false)
  const [error,setError] = useState('')
  const [copied,setCopied] = useState(false)
  const run = async (operation: () => Promise<void>) => { setBusy(true); setError(''); try { await operation() } catch (e) { setError((e as Error).message) } finally { setBusy(false) } }
  return <div className="account-settings" role="dialog" aria-label="项目设置"><aside className="account-settings-sidebar"><button className="account-back" onClick={onClose}><ArrowLeft size={18} />返回</button><p className="project-muted">项目</p>{[['projectGeneral','常规'],['domain','域名']].map(([value,label]) => <button key={value} className={`account-settings-nav-item ${page === value ? 'active' : ''}`} onClick={() => onNavigate(value)}>{label}</button>)}</aside><main className="account-settings-main"><div className="account-settings-content"><header><h1>{page === 'domain' ? '域名' : '项目设置'}</h1><p>{page === 'domain' ? '管理项目的发布网址' : '管理你的项目详情、可见性和偏好设置'}</p></header>{error && <p role="alert" className="account-error">{error}</p>}
    {page === 'projectGeneral' ? <><section className="account-card"><h2>概览</h2><dl className="project-overview"><dt>项目名称</dt><dd>{project.title}</dd><dt>所有者</dt><dd>{account?.display_name}</dd><dt>消息数量</dt><dd>{project.messages?.length ?? 0}</dd><dt>创建于</dt><dd>{new Date(project.created_at).toLocaleString('zh-CN')}</dd></dl></section><section className="account-card">{[['重命名项目','更新你的项目标题','rename'],['克隆项目','在新项目中复制此 app','clone'],['项目隐私','管理项目公开访问权限','details']].map(([title,description,action]) => <div className="account-row" key={action}><div><strong>{title}</strong><p>{description}</p></div><button className="account-connect" onClick={() => onAction(action as ProjectAction)}>{title.slice(0,2)}</button></div>)}<div className="account-row"><div><strong>移除 Atoms™ 徽章</strong><p>隐藏已发布网页的平台标识</p></div><button className={`account-switch ${project.remove_badge ? 'on' : ''}`} role="switch" aria-label="移除 Atoms 徽章" aria-checked={!!project.remove_badge} disabled={busy} onClick={() => void run(() => onBadge(!project.remove_badge))}><span /></button></div><div className="account-row"><div><strong>取消发布项目</strong><p>已发布网址将不再对任何人可访问</p></div><button className="account-connect" disabled={!project.published || busy} onClick={() => void run(onUnpublish)}>取消发布</button></div></section><section className="account-card project-danger"><div className="account-row"><div><strong>删除项目</strong><p>永久删除项目及其所有内容，包括文件、存储与数据库。</p></div><button className="account-connect" onClick={onDelete}>删除</button></div></section></> : <><section className="account-card"><h2>已连接的网址</h2><div className="account-row"><div><strong>{publicUrl}</strong><p>{project.published ? project.visibility === 'private' ? '私有 · 已关闭公开访问' : '在线' : '尚未发布'}</p></div><button className="account-connect" onClick={() => void run(async () => { await navigator.clipboard.writeText(publicUrl); setCopied(true) })}>{copied ? '已复制' : '复制链接'}</button><button className="account-connect" onClick={() => setEditingUrl(!editingUrl)}>编辑</button></div>{editingUrl && <form onSubmit={event => { event.preventDefault(); void run(async () => { await onSlug(slug); setEditingUrl(false) }) }}><label className="project-field">网址名称<input aria-label="网址名称" value={slug} onChange={e => setSlug(e.target.value.toLowerCase())} minLength={3} maxLength={63} pattern="[a-z0-9](?:[a-z0-9-]*[a-z0-9])?" /></label><p className="project-muted">3–63 个小写字母、数字或连字符；保存后使用 /api/sites/名称 访问。</p><button className="account-primary" disabled={busy}>保存网址</button></form>}{project.published && <button className="account-connect" disabled={busy} onClick={() => void run(onUnpublish)}>取消发布</button>}</section><ProjectDomains projectId={project.id} /></>}
  </div></main></div>
}
