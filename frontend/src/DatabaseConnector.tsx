import { useEffect, useState } from 'react'
import { ArrowLeft, Database, RefreshCw } from 'lucide-react'
import { authFetch } from './auth'

type Project = { id: string; title: string; schema?: string; configured: boolean }
type Probe = { connected: boolean; tables: number }

export default function DatabaseConnector() {
  const [selected, setSelected] = useState<Project | null>(null)
  const [projects, setProjects] = useState<Project[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [checks, setChecks] = useState<Record<string, Probe>>({})
  useEffect(() => {
    let active = true
    void authFetch('/database-connector').then(async response => {
      if (!response.ok) throw new Error('读取数据库连接器失败')
      const data = await response.json()
      if (active) setProjects(data.projects)
    }).catch(e => { if (active) setError(e.message) }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])
  const check = async (id: string) => {
    setBusy(id); setError('')
    try {
      const response = await authFetch(`/projects/${id}/database/check`, { method: 'POST' })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || '数据库连接检查失败')
      setChecks(previous => ({ ...previous, [id]: data }))
    } catch (e) {
      setChecks(previous => { const next = { ...previous }; delete next[id]; return next })
      setError((e as Error).message)
    } finally { setBusy('') }
  }
  if (selected) return <DatabaseTables project={selected} onBack={() => setSelected(null)} />
  return <section className="account-card github-card">
    <header className="github-heading"><Database size={22} /><div><h2>PostgreSQL 数据库</h2><p>平台托管数据库，每个项目自动配置独立 Schema 和专属应用账号。</p></div></header>
    {error && <p className="account-error" role="alert">{error}</p>}
    {loading ? <div className="account-empty">正在读取项目数据库…</div> : projects.length ? <div className="github-repositories">{projects.map(project => <article key={project.id}>
      <div><strong>{project.title}</strong><small>{project.schema || '尚未配置'}{checks[project.id]?.connected ? ` · 连接正常 · ${checks[project.id].tables} 张表` : project.configured ? ' · 已配置' : ''}</small></div>
      <button className="account-connect" disabled={!!busy || !project.configured} onClick={() => void check(project.id)}><RefreshCw size={15} />{busy === project.id ? '检查中…' : '检查连接'}</button>
      <button className="account-connect" disabled={!!busy || !project.configured} onClick={() => setSelected(project)}>查看数据表</button>
    </article>)}</div> : <div className="account-empty">创建项目后自动配置数据库连接器</div>}
    <p className="github-scope-note">默认后端模板自动使用本项目数据库。连接凭据只注入服务端；无需启动 PostgreSQL 容器。</p>
  </section>
}


type TableData = { table: string; columns: { name: string; type: string; nullable: string }[]; rows: Record<string, unknown>[]; has_more: boolean }
function DatabaseTables({ project, onBack }: { project: Project; onBack: () => void }) {
  const [tables, setTables] = useState<{ name: string }[]>([])
  const [table, setTable] = useState('')
  const [offset, setOffset] = useState(0)
  const [data, setData] = useState<TableData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)
  useEffect(() => {
    let active = true
    setLoading(true); setError(''); setData(null)
    const params = table ? `?table=${encodeURIComponent(table)}&offset=${offset}` : ''
    void authFetch(`/projects/${project.id}/database/tables${params}`).then(async response => {
      const result = await response.json()
      if (!response.ok) throw new Error(result.detail || '读取数据表失败')
      if (!active) return
      if (table) setData(result)
      else { setTables(result.tables); if (result.tables.length) setTable(result.tables[0].name) }
    }).catch(e => { if (active) setError(e.message) }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [project.id, table, offset, refresh])
  return <section className="account-card database-tables"><button className="account-back" onClick={onBack}><ArrowLeft size={16} />返回连接器</button><header className="github-heading"><Database size={22} /><div><h2>{project.title} · 数据表</h2><p>{project.schema}</p></div><button className="account-connect" disabled={loading} onClick={() => { setTable(''); setOffset(0); setRefresh(n => n + 1) }}><RefreshCw size={15} />刷新</button></header>
    {error && <p className="account-error" role="alert">{error}</p>}
    <div className="database-browser"><nav aria-label="数据表">{tables.map(item => <button key={item.name} className={`account-connect ${table === item.name ? 'selected' : ''}`} onClick={() => { setTable(item.name); setOffset(0) }}>{item.name}</button>)}</nav><div className="database-table-content">{loading ? <div className="account-empty">正在读取数据表…</div> : data ? <><h3>{data.table}</h3><div className="database-table-scroll"><table className="account-table"><thead><tr>{data.columns.map(column => <th key={column.name} title={`${column.type} · ${column.nullable === 'YES' ? '允许为空' : '不能为空'}`}>{column.name}<small>{column.type}</small></th>)}</tr></thead><tbody>{data.rows.map((row,index) => <tr key={offset+index}>{data.columns.map(column => <td key={column.name}>{row[column.name] == null ? <span className="account-muted">NULL</span> : String(row[column.name])}</td>)}</tr>)}</tbody></table></div>{!data.rows.length && <div className="account-empty">此表暂无数据</div>}<footer><button className="account-connect" disabled={offset === 0} onClick={() => setOffset(n => Math.max(0,n-50))}>上一页</button><span>第 {offset/50+1} 页 · 本页 {data.rows.length} 条</span><button className="account-connect" disabled={!data.has_more} onClick={() => setOffset(n => n+50)}>下一页</button></footer></> : !error && <div className="account-empty">此项目暂无数据表，应用创建数据表后会显示在这里。</div>}</div></div>
  </section>
}
