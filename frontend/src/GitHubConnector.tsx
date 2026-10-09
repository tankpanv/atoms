import { useEffect, useState } from 'react'
import { Github, ExternalLink, RefreshCw, Unplug, UploadCloud } from 'lucide-react'
import { accountApi } from './account'

type Connection = { connected: boolean; configured: boolean; account: { login: string; avatar_url: string; scope: string; expires_at: string | null } | null }
type Repository = { id: number; full_name: string; html_url: string; private: boolean; default_branch: string; description: string | null }
type LinkedProject = { project_id: string; full_name: string; html_url: string; default_branch: string; title: string; status: string }

export default function GitHubConnector({ onProject, onNotice }: { onProject: (id: string) => void; onNotice: (text: string) => void }) {
  const [connection, setConnection] = useState<Connection | null>(null)
  const [repositories, setRepositories] = useState<Repository[]>([])
  const [linked, setLinked] = useState<LinkedProject[]>([])
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [lastCommit, setLastCommit] = useState('')

  const refresh = async (includeRepos = true) => {
    setLoading(true); setError('')
    try {
      const state = await accountApi<Connection>('/connectors/github')
      setConnection(state)
      const projects = await accountApi<LinkedProject[]>('/connectors/github/linked-projects')
      setLinked(projects)
      if (state.connected && includeRepos) setRepositories(await accountApi<Repository[]>('/connectors/github/repositories'))
      else setRepositories([])
    } catch (e) { setError((e as Error).message) } finally { setLoading(false) }
  }
  useEffect(() => { void refresh() }, [])

  const connect = async () => {
    setBusy('connect'); setError('')
    try { const result = await accountApi<{ url: string }>('/connectors/github/authorize'); window.location.assign(result.url) }
    catch (e) { setError((e as Error).message); setBusy('') }
  }
  const importRepo = async (repo: Repository) => {
    setBusy(`import:${repo.id}`); setError('')
    try {
      const result = await accountApi<{ project_id: string; imported_files: number }>('/connectors/github/import', { method: 'POST', body: JSON.stringify({ repository_id: repo.id, full_name: repo.full_name, default_branch: repo.default_branch }) })
      onNotice(`已导入 ${repo.full_name}，共 ${result.imported_files} 个文件`); onProject(result.project_id)
    } catch (e) { setError((e as Error).message) } finally { setBusy('') }
  }
  const sync = async (project: LinkedProject) => {
    setBusy(`sync:${project.project_id}`); setError('')
    try {
      const result = await accountApi<{ url: string; commit: string; files: number }>(`/connectors/github/sync/${project.project_id}`, { method: 'POST' })
      setLastCommit(result.url); onNotice(`已提交 ${result.files} 个文件到 ${project.full_name}，提交 ${result.commit.slice(0, 7)}`)
    } catch (e) { setError((e as Error).message) } finally { setBusy('') }
  }
  const disconnect = async () => {
    setBusy('disconnect'); setError('')
    try { await accountApi('/connectors/github', { method: 'DELETE' }); setConnection({ connected: false, configured: true, account: null }); setRepositories([]); onNotice('已断开 GitHub 连接') }
    catch (e) { setError((e as Error).message) } finally { setBusy('') }
  }

  return <section className="account-card github-card">
    <header className="github-heading"><Github size={22} /><div><h2>GitHub</h2><p>连接仓库、导入代码，并将项目改动同步为 GitHub 提交。</p></div>{connection?.connected && <button className="account-connect" disabled={busy !== ''} onClick={() => void disconnect()}><Unplug size={15} />断开连接</button>}</header>
    {connection?.connected && connection.account ? <div className="github-connected"><img src={connection.account.avatar_url} alt="" /><div><strong>{connection.account.login}</strong><small>已授权仓库读写 · 令牌加密保存在服务端</small></div><button className="account-text-link" disabled={loading || busy !== ''} onClick={() => void refresh(true)}><RefreshCw size={15} />刷新仓库</button></div>
      : <div className="github-connect-row"><p>{connection?.configured ? '授权后可以导入你有权访问的仓库，并将 Atoms 项目提交回 GitHub。' : '需要管理员先配置 GitHub OAuth App 和服务端令牌加密密钥。'}</p><button className="account-primary" disabled={!connection?.configured || busy !== ''} onClick={() => void connect()}>{busy === 'connect' ? '正在跳转…' : '连接 GitHub'}</button></div>}
    {error && <p className="account-error" role="alert">{error}</p>}
    {lastCommit && <p className="github-last-commit"><a href={lastCommit} target="_blank" rel="noreferrer">查看最近一次 GitHub 提交 <ExternalLink size={14} /></a></p>}
    {connection?.connected && <>
      <div className="github-section-title"><h3>导入仓库</h3><span>{repositories.length} 个仓库</span></div>
      {loading && !repositories.length ? <div className="account-empty">正在读取 GitHub 仓库…</div> : repositories.length ? <div className="github-repositories">{repositories.map(repo => <article key={repo.id}><div><a href={repo.html_url} target="_blank" rel="noreferrer">{repo.full_name}<ExternalLink size={13} /></a><small>{repo.description || `默认分支 ${repo.default_branch}`}</small></div><span className="github-visibility">{repo.private ? '私有' : '公开'}</span><button className="account-connect" disabled={busy !== ''} onClick={() => void importRepo(repo)}>{busy === `import:${repo.id}` ? '导入中…' : '导入为新项目'}</button></article>)}</div> : <div className="account-empty">此 GitHub 账号还没有可访问的仓库</div>}
      {!!linked.length && <><div className="github-section-title"><h3>已关联项目</h3></div><div className="github-repositories">{linked.map(project => <article key={project.project_id}><div><strong>{project.title}</strong><small><a href={project.html_url} target="_blank" rel="noreferrer">{project.full_name}<ExternalLink size={13} /></a> · {project.default_branch}</small></div><button className="account-connect" disabled={busy !== '' || ['queued','running'].includes(project.status)} onClick={() => void sync(project)}><UploadCloud size={15} />{busy === `sync:${project.project_id}` ? '同步中…' : '提交到 GitHub'}</button></article>)}</div></>}
    </>}
    <p className="github-scope-note">授权范围：GitHub 仓库读写权限。仓库导入会创建独立 Atoms 项目；同步会在默认分支创建提交，忽略依赖、构建产物和 .env 文件。</p>
  </section>
}
