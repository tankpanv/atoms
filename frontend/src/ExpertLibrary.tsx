import { useEffect, useMemo, useRef, useState } from 'react'
import { Bookmark, Check, ChevronDown, Code2, ExternalLink, Layers, Lightbulb, Search, Send, Sparkles, X } from 'lucide-react'
import './experts.css'

export type ExpertExample = { id: string; title: string; description: string; color: string }
export type Expert = { id: string; skill: string; version: string; updated_at: string; name: string; author: string; category: string; avatar: string; description: string; tags: string[]; capabilities: string[]; prompts: string[]; examples: ExpertExample[]; saved: boolean; uses: number }

function useDialog(close: () => void) {
  const ref = useRef<HTMLElement>(null)
  const closeRef = useRef(close)
  closeRef.current = close
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    ref.current?.focus()
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeRef.current() }
      if (event.key === 'Tab' && ref.current) {
        const elements = [...ref.current.querySelectorAll<HTMLElement>('button:not(:disabled),input,a[href],iframe,[tabindex="0"]')].filter(element => !element.closest('[inert]'))
        if (!elements.length) return
        const current = elements.indexOf(document.activeElement as HTMLElement)
        const next = current < 0 ? (event.shiftKey ? elements.length - 1 : 0) : (current + (event.shiftKey ? -1 : 1) + elements.length) % elements.length
        event.preventDefault()
        elements[next].focus()
      }
    }
    document.addEventListener('keydown', key)
    return () => { document.removeEventListener('keydown', key); previous?.focus() }
  }, [])
  return ref
}

function ExampleArt({ example }: { example: ExpertExample }) {
  return <div className={`expert-example-art ${example.color}`} aria-hidden="true"><div className="expert-art-browser"><i /><i /><i /></div>{example.color === 'coffee' ? <><small>ATELIER · COFFEE</small><strong>每一杯，<br />都值得停留。</strong><span className="expert-art-cup" /></> : example.color === 'studio' ? <><small>FORM® · DESIGN STUDIO</small><strong>让好的想法，<br /><em>看得见。</em></strong><div className="expert-art-blocks"><span>Solace.</span><span>Orbit ↗</span></div></> : <><small>BACKEND ARCHITECTURE</small><strong>清晰的系统边界。</strong><div className="expert-art-flow"><span>API</span> → <span>Service</span> → <span>Database</span></div></>}</div>
}

export function ExpertDetails({ expert, onClose, onChoose, onSave }: { expert: Expert; onClose: () => void; onChoose: (expert: Expert, prompt?: string) => void; onSave: (expert: Expert) => void }) {
  const [example, setExample] = useState<ExpertExample | null>(null)
  const ref = useDialog(() => example ? setExample(null) : onClose())
  useEffect(() => { if (example) ref.current?.querySelector<HTMLButtonElement>('.expert-case-overlay header button')?.focus() }, [example])
  return <div className="expert-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}><section className="expert-detail" ref={ref} tabIndex={-1} role="dialog" aria-modal="true" aria-label={`${expert.name}详情`}>
    <div inert={!!example}>
    <button className="expert-dialog-close" aria-label="关闭专家详情" onClick={onClose}><X size={22} /></button>
    <div className="expert-detail-header"><img src={expert.avatar} alt="" /><div><h1>{expert.name} <span>│ {expert.author}</span></h1><p>你的任务中已使用 {expert.uses.toLocaleString('zh-CN')} 次 · v{expert.version}</p><div className="expert-detail-actions"><button className="expert-primary" onClick={() => onChoose(expert)}><Send size={17} />召唤专家</button><button className="expert-secondary" aria-pressed={expert.saved} onClick={() => onSave(expert)}><Bookmark size={16} fill={expert.saved ? 'currentColor' : 'none'} />{expert.saved ? '已加入我的专家' : '加入我的专家'}</button></div></div></div>
    <p className="expert-detail-description">{expert.description}</p><div className="expert-tags">{expert.tags.map(tag => <span key={tag}>{tag}</span>)}</div>
    <h2><Lightbulb size={22} />专家帮你做</h2><ul className="expert-capabilities">{expert.capabilities.map(text => <li key={text}><Check size={16} />{text}</li>)}</ul>
    <div className="expert-prompt-list">{expert.prompts.map(prompt => <button key={prompt} onClick={() => onChoose(expert, prompt)} title="使用该专家和示例需求构建"><span>“{prompt}”</span><Send size={20} /></button>)}</div>
    <h2><Layers size={21} />使用案例</h2><p className="expert-case-note">可交互的设计与架构参考，点击预览；构建时以你的实际需求为准。</p><div className="expert-example-grid">{expert.examples.map(item => <button className="expert-example" key={item.id} onClick={() => setExample(item)}><ExampleArt example={item} /><div><strong>{item.title}</strong><p>{item.description}</p><span>预览案例 <ExternalLink size={13} /></span></div></button>)}</div>
    <div className="expert-skill-note"><Code2 size={16} />构建时加载 {expert.skill}/SKILL.md，并记录本次使用的版本。</div>
    </div>
    {example && <div className="expert-case-overlay"><header><div><strong>{example.title}</strong><small>专家案例预览</small></div><button aria-label="关闭案例预览" onClick={() => setExample(null)}><X size={22} /></button></header><iframe title={example.title} src={`/api/experts/${expert.id}/examples/${example.id}`} sandbox="allow-scripts" /><footer><span>{example.description}</span><button className="expert-primary" onClick={() => onChoose(expert)}>使用该专家构建</button></footer></div>}
  </section></div>
}

export function ExpertPicker({ experts, value, onChange, open, setOpen, onDetails, onBrowse, disabled = false }: { experts: Expert[]; value: string[]; onChange: (ids: string[]) => void; open: boolean; setOpen: (open: boolean) => void; onDetails: (expert: Expert) => void; onBrowse: () => void; disabled?: boolean }) {
  const [query, setQuery] = useState('')
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    setQuery('')
    const outside = (event: PointerEvent) => { if (!ref.current?.contains(event.target as Node)) setOpen(false) }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('pointerdown', outside); document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape) }
  }, [open, setOpen])
  const chosen = experts.filter(expert => value.includes(expert.id))
  const visible = experts.filter(expert => `${expert.name} ${expert.author} ${expert.description} ${expert.tags.join(' ')}`.toLowerCase().includes(query.trim().toLowerCase()))
  return <div className="expert-picker" ref={ref}>
    <div className="expert-selection"><button type="button" className="expert-picker-trigger" disabled={disabled} aria-label="选择专家" aria-expanded={open} aria-haspopup="listbox" onClick={() => setOpen(!open)}><Sparkles size={16} />{chosen.length ? '专家' : '选择专家'}<ChevronDown size={13} /></button>{chosen.map(expert => <span className="expert-selected-chip" key={expert.id} title={`使用${expert.name}进行构建`}><button type="button" onClick={() => onDetails(expert)}><img src={expert.avatar} alt="" />{expert.name}</button><button type="button" disabled={disabled} aria-label={`移除${expert.name}`} onClick={() => onChange(value.filter(id => id !== expert.id))}><X size={13} /></button></span>)}</div>
    {open && <div className="expert-picker-menu"><label className="expert-search"><Search size={17} /><input autoFocus aria-label="搜索专家" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索专家名称或能力" />{query && <button type="button" aria-label="清空专家搜索" onClick={() => setQuery('')}><X size={15} /></button>}</label><div className="expert-picker-results" role="listbox" aria-label="可选专家" aria-multiselectable="true">{visible.map(expert => <div className="expert-picker-row" key={expert.id}><button type="button" role="option" aria-selected={value.includes(expert.id)} disabled={disabled || (!value.includes(expert.id) && value.length >= 3)} onClick={() => { onChange(value.includes(expert.id) ? value.filter(id => id !== expert.id) : [...value, expert.id]); setOpen(false) }}><img src={expert.avatar} alt="" /><span><strong>{expert.name}</strong><small>{expert.tags.join(' · ')}</small></span>{value.includes(expert.id) && <Check size={17} />}</button><button type="button" aria-label={`查看${expert.name}详情`} onClick={() => { setOpen(false); onDetails(expert) }}><ExternalLink size={15} /></button></div>)}{!visible.length && <p className="expert-empty">没有找到匹配的专家</p>}</div><footer><small>最多选择 3 位专家</small><button type="button" onClick={() => { setOpen(false); onBrowse() }}>浏览专家页 ↗</button></footer></div>}
  </div>
}

export default function ExpertsPage({ experts, loading, error, onRetry, onDetails, onChoose, onSave }: { experts: Expert[]; loading: boolean; error: string; onRetry: () => void; onDetails: (expert: Expert) => void; onChoose: (expert: Expert) => void; onSave: (expert: Expert) => void }) {
  const [query, setQuery] = useState(''), [category, setCategory] = useState('全部'), [mine, setMine] = useState(false), [sort, setSort] = useState('综合')
  const visible = useMemo(() => experts.filter(expert => (!mine || expert.saved) && (category === '全部' || expert.category === category) && `${expert.name} ${expert.author} ${expert.description} ${expert.tags.join(' ')}`.toLowerCase().includes(query.trim().toLowerCase())).sort((a, b) => sort === '最常用' ? b.uses - a.uses : sort === '最新' ? b.updated_at.localeCompare(a.updated_at) : 0), [experts, category, mine, query, sort])
  return <main className="experts-page"><header className="experts-page-header"><div className="expert-page-title"><Sparkles size={20} /><h1>专家</h1><span>将专业能力带入你的构建</span></div><div className="expert-page-actions"><label className="expert-search"><Search size={18} /><input value={query} onChange={event => setQuery(event.target.value)} aria-label="搜索专家职称或描述" placeholder="搜索专家职称或描述" /></label><button className={`expert-secondary ${mine ? 'active' : ''}`} aria-pressed={mine} onClick={() => setMine(!mine)}><Bookmark size={17} />{mine ? '全部专家' : '我的专家'}</button></div></header>
    {!mine && !query && <><h2>精选场景</h2><div className="expert-scenarios">{[{name:'网站设计与开发',caption:'从想法，到完整上线的产品',color:'website',ids:['website-architect','backend-architect']},{name:'产品与界面设计',caption:'精致视觉，清晰交互',color:'design',ids:['interface-designer','website-architect']}].map(scene => <section key={scene.name} className={`expert-scene ${scene.color}`}><div className="expert-scene-art" /><small>{scene.caption}</small><h3>{scene.name}</h3>{scene.ids.map(id => experts.find(expert => expert.id === id)).filter((expert): expert is Expert => !!expert).map(expert => <button key={expert.id} onClick={() => onDetails(expert)}><img src={expert.avatar} alt="" /><span>{expert.name}</span><ExternalLink size={15} /></button>)}</section>)}</div></>}
    <div className="expert-list-heading"><h2>{mine ? '我的专家' : '专家'}</h2><div className="expert-sort">{['综合','最常用','最新'].map(label => <button className={label === sort ? 'active' : ''} key={label} onClick={() => setSort(label)}>{label}</button>)}</div></div><div className="expert-categories">{['全部',...new Set(experts.map(expert => expert.category))].map(label => <button className={label === category ? 'active' : ''} key={label} onClick={() => setCategory(label)}>{label}</button>)}</div>
    {loading ? <p className="expert-empty">正在加载专家…</p> : error ? <div className="expert-empty" role="alert">{error}<button onClick={onRetry}>重新加载</button></div> : <><div className="expert-card-grid">{visible.map(expert => <article className="expert-card" key={expert.id}><button className="expert-card-main" onClick={() => onDetails(expert)} aria-label={`查看${expert.name}`}><div className="expert-card-header"><img src={expert.avatar} alt="" /><div><h3>{expert.name}</h3><span>{expert.author}</span></div></div><p>{expert.description}</p><div className="expert-tags">{expert.tags.map(tag => <span key={tag}>{tag}</span>)}</div></button><footer><button className="expert-card-save" aria-label={`${expert.saved ? '取消收藏' : '收藏'}${expert.name}`} aria-pressed={expert.saved} onClick={() => onSave(expert)}><Bookmark size={16} fill={expert.saved ? 'currentColor' : 'none'} /></button><span>{expert.examples.length} 个案例</span><button className="expert-primary" onClick={() => onChoose(expert)}>召唤</button></footer></article>)}</div>{!visible.length && <p className="expert-empty">{mine ? '还没有收藏专家，浏览专家详情并加入我的专家。' : '没有匹配的专家，请调整搜索或分类。'}</p>}</>}
  </main>
}
