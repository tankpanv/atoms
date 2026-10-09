import { useEffect, useRef, useState, type ReactNode } from 'react'
import { ArrowLeft, Check, ChevronDown, CircleUserRound, Cloud, Download, ExternalLink, Gem, HardDrive, Heart, Info, Lock, Pencil, Plug, RefreshCw, Users, X } from 'lucide-react'
import { AccountAvatar } from './AccountMenu'
import { accountApi, avatarData, type Account, type AccountPatch, saveAccount } from './account'
import BillingWallet, { BillingHistory, PaymentDialog, type Order } from './BillingPanel'
import { requestKey } from './account'
import GitHubConnector from './GitHubConnector'
import DatabaseConnector from './DatabaseConnector'
import './account.css'
import { formatCredits, formatNumber } from './numberFormat'

type Model = { id: string; provider: string; name: string; available: boolean | null }
type Storage = { total_bytes: number; projects: { id: string; title: string; bytes: number; files: number; updated_at: string }[] }

function Choice({ value, options, onChange, label }: { value: string; options: { value: string; label: string }[]; onChange: (value: string) => void; label: string }) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => { const outside = (e: PointerEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false) }; document.addEventListener('pointerdown', outside); return () => document.removeEventListener('pointerdown', outside) }, [])
  return <div className="account-choice" ref={ref}><button type="button" aria-label={label} aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen(!open)}>{options.find(o => o.value === value)?.label || value}<ChevronDown size={16} /></button>{open && <div className="account-options" role="listbox" aria-label={label} onKeyDown={e => { if (e.key === 'Escape') setOpen(false) }}>{options.length > 10 && <input autoFocus placeholder="搜索模型" value={query} onChange={e => setQuery(e.target.value)} />}{options.filter(o => o.label.toLowerCase().includes(query.toLowerCase())).map(o => <button role="option" aria-selected={o.value === value} key={o.value} onClick={() => { onChange(o.value); setOpen(false) }}>{o.label}{o.value === value && <Check size={15} />}</button>)}</div>}</div>
}
function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (checked: boolean) => void; label: string }) { return <button className={`account-switch ${checked ? 'on' : ''}`} type="button" role="switch" aria-label={label} aria-checked={checked} onClick={() => onChange(!checked)}><span /></button> }
function Row({ title, description, children }: { title: string; description?: string; children: ReactNode }) { return <div className="account-row"><div><strong>{title}</strong>{description && <p>{description}</p>}</div><div className="account-row-control">{children}</div></div> }
const nav = [
  { key: 'profile', label: '你的账户', icon: CircleUserRound, group: '账户' },
  { key: 'globalControl', label: '偏好设置', icon: Heart },
  { key: 'workspaceGeneral', label: '', icon: Users, group: '工作区' },
  { key: 'plan', label: '套餐与积分', icon: Gem },
  { key: 'cloudAiBalance', label: '云与 AI 钱包', icon: Cloud },
  { key: 'connectors', label: '连接器', icon: Plug },
  { key: 'diskSpace', label: '磁盘空间', icon: HardDrive },
]
const headings: Record<string, [string, string]> = {
  profile: ['账户设置', '管理你的个人资料并个性化你的账户体验'],
  globalControl: ['账户偏好设置', '管理你的默认账户偏好和行为设置'],
  workspaceGeneral: ['工作区设置', '管理你的项目工作区信息'],
  plan: ['套餐与积分', '管理你的订阅套餐和积分余额'],
  cloudAiBalance: ['云与AI', '管理积分余额、充值与模型调用计费'],
  connectors: ['连接器', '选择要连接的连接器提供方'],
  diskSpace: ['磁盘空间', '查看存储使用情况并管理项目的磁盘空间'],
}
const connectors = [
  ['GitHub', '连接你的 GitHub 帐户以管理代码仓库并在项目上协作。'],
  ['Supabase', '连接 Supabase 以管理你的数据库和后端服务。'],
  ['Stripe', '连接 Stripe 以在你的应用中处理支付和账单。'],
  ['Google Analytics 4', '连接 GA4，以分析你的网站流量和用户行为。'],
  ['Google Search Console', '连接 GSC，以监控你的搜索表现和索引情况。'],
  ['Google Ads', 'Google Ads 是 Google 用于提升知名度、增加流量和促进转化的平台。'],
  ['Asana', '在 Asana 中访问项目、任务和团队工作流。'], ['Box', '在 Box 中访问文件、文件夹和企业内容。'],
  ['Dropbox', '在 Dropbox 中访问文件、文件夹和共享内容。'], ['Todoist', '在 Todoist 中访问任务、项目和团队待办事项。'],
  ['Linear', '通过 MCP 连接 Linear 中的事项、项目、团队和工作流数据。'],
]
export default function AccountSettings({ account, page: requestedPage, models, onChange, onNavigate, onClose, onProfile, onProject }: {
  account: Account; page: string; models: Model[]; onChange: (account: Account) => void; onNavigate: (page: string) => void;
  onClose: () => void; onProfile: () => void; onProject: (id: string) => void;
}) {
  const page = headings[requestedPage] ? requestedPage : 'profile'
  const [notice, setNotice] = useState('')
  const [countdown, setCountdown] = useState(10)
  const [saving, setSaving] = useState(false)
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(account.display_name)
  const [workspaceName, setWorkspaceName] = useState(account.workspace_name)
  const [description, setDescription] = useState(account.workspace_description)
  const [annual, setAnnual] = useState(false)
  const [planTab, setPlanTab] = useState('计划')
  const [proCredits, setProCredits] = useState('350')
  const [maxCredits, setMaxCredits] = useState('500')
  const [paymentOrder, setPaymentOrder] = useState<Order | null>(null)
  const [storage, setStorage] = useState<Storage | null>(null)
  const [storageError, setStorageError] = useState('')
  const avatarRef = useRef<HTMLInputElement>(null)
  const workspaceAvatarRef = useRef<HTMLInputElement>(null)
  const prefs = account.preferences
  useEffect(() => { if (!notice) return; setCountdown(10); const timer = setTimeout(() => setNotice(''), 10000); const interval = setInterval(() => setCountdown(value => Math.max(0, value - 1)), 1000); return () => { clearTimeout(timer); clearInterval(interval) } }, [notice])
  useEffect(() => { setNotice(''); setEditing(false); if (page === 'diskSpace') { setStorage(null); setStorageError(''); accountApi<Storage>('/storage').then(setStorage).catch(e => setStorageError(e.message)) } if (page === 'connectors') { const result = new URLSearchParams(location.search).get('github'); if (result) { const messages: Record<string,string> = { connected: 'GitHub 已连接', denied: '已取消 GitHub 授权', invalid: 'GitHub 回调无效', 'not-configured': 'GitHub OAuth 尚未配置', 'state-expired': '授权已过期，请重新连接', failed: 'GitHub 授权失败，请重试' }; setNotice(messages[result] || 'GitHub 授权结束'); const url = new URL(location.href); url.searchParams.delete('github'); history.replaceState(history.state, '', url) } } }, [page])
  useEffect(() => { const escape = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }; document.addEventListener('keydown', escape); return () => document.removeEventListener('keydown', escape) }, [onClose])
  const save = async (patch: AccountPatch) => { setSaving(true); try { onChange(await saveAccount(patch)); setNotice('已保存'); return true } catch (e) { setNotice((e as Error).message); return false } finally { setSaving(false) } }
  const preference = (key: keyof Account['preferences'], value: string | boolean) => void save({ preferences: { [key]: value } })
  const upload = async (file: File | undefined, workspace = false) => { if (!file) return; try { const data = await avatarData(file); await save(workspace ? { workspace_avatar: data } : { avatar: data }) } catch (e) { setNotice((e as Error).message) } }
  const billing = async (action: string, plan = 'Pro', credits = 350) => { if (action === 'portal') { setPlanTab('支付'); return }; setSaving(true); try { setPaymentOrder(await accountApi<Order>(`/billing/${action}`, { method: 'POST', body: JSON.stringify({ plan, credits, annual, idempotency_key: requestKey() }) })) } catch (e) { setNotice((e as Error).message) } finally { setSaving(false) } }
  return <div className="account-settings" role="dialog" aria-label="账户设置" aria-modal="true">
    <aside className="account-settings-sidebar"><button className="account-back" onClick={onClose}><ArrowLeft size={16} />返回</button><nav>{nav.map(item => <div key={item.key}>{item.group && <p>{item.group}</p>}<button className={page === item.key ? 'active' : ''} onClick={() => onNavigate(item.key)}>{item.key === 'workspaceGeneral' ? <AccountAvatar account={account} workspace /> : <item.icon size={17} />}<span>{item.label || account.workspace_name}</span></button></div>)}</nav></aside>
    <main className="account-settings-main"><div className={`account-settings-content ${['profile', 'globalControl', 'workspaceGeneral'].includes(page) ? 'narrow' : ''}`}><header><h1>{headings[page][0]}{page === 'plan' && <span className="account-plan-badge"><Gem size={13} />{account.plan}</span>}</h1><p>{headings[page][1]}</p></header>
      {page === 'profile' && <div className="account-card account-form-card">
        <Row title="语言" description="更改用户界面使用的语言"><Choice label="界面语言" value={prefs.language} options={[{ value: 'zh', label: '中文' }, { value: 'en', label: 'English' }]} onChange={value => preference('language', value)} /></Row>
        <Row title="个人主页" description="管理你的公开个人资料"><button className="account-text-link" onClick={onProfile}>在 Atoms 打开个人资料@{account.display_name}<ExternalLink size={15} /></button></Row>
        <Row title="头像" description="上传或更换你的头像"><button className="account-avatar-edit" aria-label="上传头像" onClick={() => avatarRef.current?.click()}><AccountAvatar account={account} /></button><input ref={avatarRef} type="file" hidden accept="image/png,image/jpeg,image/webp" onChange={e => { void upload(e.target.files?.[0]); e.target.value = '' }} /></Row>
        <Row title="用户名" description="选择你的名字在 Atoms 上的显示方式">{editing ? <form onSubmit={e => { e.preventDefault(); void save({ display_name: name }).then(ok => { if (ok) setEditing(false) }) }}><input aria-label="用户名" value={name} maxLength={80} onChange={e => setName(e.target.value)} autoFocus /><button className="account-primary" disabled={saving || !name.trim()}>保存</button><button type="button" onClick={() => setEditing(false)}>取消</button></form> : <button className="account-edit-name" onClick={() => { setName(account.display_name); setEditing(true) }}>{account.display_name}<Pencil size={15} /></button>}</Row>
        <Row title="邮箱地址" description="管理你的账户邮箱"><span>{account.email}</span></Row>
      </div>}
      {page === 'globalControl' && <div className="account-card account-form-card">
        <Row title="默认模型" description="选择在你的账户中默认使用的 AI 模型"><Choice label="默认模型" value={prefs.default_model} options={models.filter(m => m.available !== false).map(m => ({ value: m.id, label: `${m.provider} · ${m.name}` }))} onChange={value => preference('default_model', value)} /></Row>
        <Row title="权限" description="设置项目默认访问权限"><Choice label="项目访问权限" value={prefs.visibility} options={[{ value: 'public', label: '公开' }, { value: 'private', label: '私有' }]} onChange={value => preference('visibility', value)} /><small>{prefs.visibility === 'public' ? '项目可以通过链接和 Discover 访问' : '仅工作区成员可以访问项目'}</small></Row>
        <Row title="积分余额提醒" description="显示剩余积分"><Toggle label="显示剩余积分" checked={prefs.show_credits} onChange={value => preference('show_credits', value)} /></Row>
        <Row title="生成完成提示音" description="生成完成时播放提示音"><div className="account-radio-group">{[['first', '仅适用于首次构建'], ['always', '适用于每次构建'], ['off', '关闭']].map(([value, label]) => <label key={value}><input type="radio" name="completion-sound" checked={prefs.sound === value} onChange={() => preference('sound', value)} />{label}</label>)}</div></Row>
        <Row title="外部通知" description="通过电子邮件接收域名状态提醒。"><Toggle label="外部通知" checked={prefs.email_notifications} onChange={value => preference('email_notifications', value)} /></Row>
        <Row title="移除 Atoms™ 徽章 · 全局" description="设置新项目的默认标识可见性"><Toggle label="移除 Atoms 徽章" checked={prefs.remove_badge} onChange={value => preference('remove_badge', value)} /></Row>
      </div>}
      {page === 'workspaceGeneral' && <><div className="account-card account-form-card"><Row title="头像" description="为你的工作区设置头像。"><button className="account-avatar-edit" aria-label="上传工作区头像" onClick={() => workspaceAvatarRef.current?.click()}><AccountAvatar account={account} workspace /></button><input type="file" ref={workspaceAvatarRef} hidden accept="image/png,image/jpeg,image/webp" onChange={e => { void upload(e.target.files?.[0], true); e.target.value = '' }} /></Row><Row title="名称" description="你的完整工作区名称。"><input value={workspaceName} aria-label="工作区名称" maxLength={100} onChange={e => setWorkspaceName(e.target.value)} /><small>{workspaceName.length} / 100 字符</small></Row><Row title="描述" description="关于你的工作区的描述。"><textarea value={description} aria-label="工作区描述" maxLength={500} placeholder="描述" onChange={e => setDescription(e.target.value)} /><small>{description.length} / 500 字符</small></Row></div><div className="account-form-actions"><button onClick={() => { setWorkspaceName(account.workspace_name); setDescription(account.workspace_description) }}>取消</button><button className="account-primary" disabled={saving || !workspaceName.trim() || (workspaceName === account.workspace_name && description === account.workspace_description)} onClick={() => void save({ workspace_name: workspaceName, workspace_description: description })}>更新</button></div></>}
      {page === 'plan' && <><div className="account-card account-credit-card"><div><strong>剩余积分 <Info size={15} /></strong><span>{formatCredits(account.available_credits)} 可用</span></div><div className="account-credit-progress"><span style={{ width: `${Math.min(100, account.credits / Math.max(account.credits, 25) * 100)}%` }} /><b>{formatCredits(account.credits)} 剩余</b></div><div className="account-credit-meta"><span>• 冻结 {formatCredits(account.reserved_credits)} 积分 · $1 = {account.credits_per_usd} 积分</span><span>本月已领免费积分: {formatNumber(account.free_granted)} / 25</span></div></div><div className="account-tabs"><button className={planTab === '计划' ? 'active' : ''} onClick={() => setPlanTab('计划')}>计划</button><button className={planTab === '支付' ? 'active' : ''} onClick={() => setPlanTab('支付')}>支付</button><button className="account-billing-link" onClick={() => void billing('portal')}>管理账单 <ExternalLink size={15} /></button></div>{planTab === '支付' ? <BillingHistory onChange={onChange} /> : <><div className="account-annual"><Toggle checked={annual} onChange={setAnnual} label="按年计费" /><b>按年</b><span>最多可节省 21%</span></div><div className="account-plan-grid">{['Free', 'Pro', 'Max'].map((plan, index) => { const credits = index === 1 ? proCredits : maxCredits; const prices: Record<string, number> = { '100': 20, '250': 50, '350': 70, '500': 100 }; const price = index === 0 ? 0 : prices[credits] || Number(credits) / 5; return <article className={`account-plan-card ${index === 2 ? 'max' : ''}`} key={plan}><h2>{plan}{annual && index > 0 && <small>{index === 1 ? '18%' : '21%'} 折扣</small>}</h2><div className="account-plan-price"><strong>${annual ? Number((price * (index === 1 ? .82 : .79)).toFixed(2)) : price}</strong><span>/ 月</span>{annual && index > 0 && <del>${price}</del>}</div><p>{['入门使用指南', '解锁更多功能', '全功能访问Atoms最佳体验'][index]}</p>{index === 0 ? <div className="account-free-credits">15 积分 / 天</div> : <Choice label={`${plan} 积分套餐`} value={credits} options={(index === 1 ? ['100', '250', '350'] : ['500', '1000', '1500', '2500', '3750', '5000', '6000', '7500', '10000', '12500', '15000']).map(value => ({ value, label: `${value} 积分 / 月` }))} onChange={index === 1 ? setProCredits : setMaxCredits} />}<button className={index === 2 ? 'account-primary' : 'account-plan-action'} disabled={saving || account.plan === plan} onClick={() => void billing('checkout', plan, index === 0 ? 0 : Number(credits))}>{account.plan === plan ? '你当前的计划' : index === 0 ? '降级' : '升级'}</button><ul><li><RefreshCw />15 每日积分 (最多 25 / 月)</li>{index > 0 && <li><Gem />{credits} 每月积分</li>}<li><HardDrive />{[2, 10, 100][index]}GB 磁盘空间</li>{(index === 0 ? ['无限项目共享', '2 个 Atoms 后端项目'] : ['私人项目', '下载项目', '移除 Atoms™ 徽章', '编辑项目', ...(annual ? ['积分结转'] : []), '无限制的 Atoms 后端项目', 'Atoms 生产云', '自定义域名', '集中计费', ...(index === 2 ? ['2倍计算资源 (相比 Pro)', '竞赛模式'] : [])]).map((feature, i) => <li key={feature}>{i === 0 ? <Lock /> : i === 1 ? <Download /> : <Check />}{feature}</li>)}</ul></article> })}</div></>}</>}
      {page === 'cloudAiBalance' && <BillingWallet account={account} onChange={onChange} />}
      {page === 'connectors' && <><DatabaseConnector /><GitHubConnector onProject={id => { onClose(); onProject(id) }} onNotice={setNotice} />{connectors.filter(([name]) => name !== 'GitHub').map(([name, description]) => <div className="account-card account-form-card" key={name}><Row title={name} description={description}><button className="account-connect" onClick={() => setNotice(`${name} 尚未配置授权服务`)}>连接</button></Row></div>)}</>}
      {page === 'diskSpace' && <><div className="account-card"><Row title="为磁盘空间启用按量付费模式" description="仅在需要时为额外存储付费"><Toggle label="磁盘空间按量付费" checked={prefs.storage_metered} onChange={() => setNotice('尚未配置存储计费服务，未开启收费。')} /></Row></div><h3>管理项目</h3><div className="account-card"><h3>存储概览 <small>{storage?.projects.length ?? 0} 项目</small></h3><p>{storage ? `${formatNumber(storage.total_bytes / 1048576)} MB` : storageError || '正在计算存储用量…'}</p><table className="account-table"><thead><tr>{['Project', '大小', '比例', '文件', '最后修改时间', '操作'].map(t => <th key={t}>{t}</th>)}</tr></thead><tbody>{storage?.projects.map(p => <tr key={p.id}><td>{p.title}</td><td>{formatNumber(p.bytes / 1048576)} MB</td><td>{storage.total_bytes ? formatNumber(p.bytes / storage.total_bytes * 100) : 0}%</td><td>{p.files}</td><td>{new Date(p.updated_at).toLocaleDateString('zh-CN')}</td><td><button className="account-text-link" onClick={() => { onClose(); onProject(p.id) }}>管理</button></td></tr>)}</tbody></table>{storage && !storage.projects.length && <div className="account-empty">还没有项目</div>}</div></>}

    </div></main>{paymentOrder && <PaymentDialog order={paymentOrder} onChange={onChange} onClose={() => setPaymentOrder(null)} />}{notice && <div className="account-toast" role="status"><span>{notice}</span><small>{countdown}s</small><button aria-label="关闭提示" onClick={() => setNotice('')}><X size={16} /></button></div>}
  </div>
}

export function RedeemDialog({ onClose, onChange }: { onClose: () => void; onChange: (account: Account) => void }) {
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState(false)
  useEffect(() => { const escape = (e: KeyboardEvent) => { if (e.key === 'Escape' && !busy) onClose() }; document.addEventListener('keydown', escape); return () => document.removeEventListener('keydown', escape) }, [busy, onClose])
  return <div className="account-dialog-backdrop" onMouseDown={e => { if (e.currentTarget === e.target && !busy) onClose() }}><form className="account-redeem" role="dialog" aria-label="积分兑换" onSubmit={e => { e.preventDefault(); setBusy(true); setError(''); accountApi<Account>('/redeem', { method: 'POST', body: JSON.stringify({ code }) }).then(data => { onChange(data); setSuccess(true) }).catch(e => setError(e.message)).finally(() => setBusy(false)) }}><button type="button" className="account-dialog-close" aria-label="关闭兑换" onClick={onClose}><X /></button><h2>积分兑换</h2>{success ? <><p className="account-redeem-success"><Check size={20} />兑换成功，积分已到账</p><button type="button" className="account-primary" onClick={onClose}>完成</button></> : <><input autoFocus aria-label="兑换码" placeholder="请输入" value={code} onChange={e => setCode(e.target.value)} maxLength={200} />{error && <p role="alert" className="account-error">{error}</p>}<button className="account-primary" disabled={!code.trim() || busy}>{busy ? '兑换中…' : '兑换'}</button></>}</form></div>
}
