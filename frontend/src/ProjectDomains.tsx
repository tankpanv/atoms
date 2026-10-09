import { useEffect,useState } from 'react'
import { authFetch } from './auth'
type Domain = { domain: string; status: string; verification_token: string }
type Domains = { target: string; domains: Domain[]; gateway_configured: boolean }
export default function ProjectDomains({ projectId }: { projectId: string }) {
  const [data,setData]=useState<Domains | null>(null)
  const [adding,setAdding]=useState(false)
  const [name,setName]=useState('')
  const [busy,setBusy]=useState(false)
  const [error,setError]=useState('')
  const api=async(path='',method='GET',body?:unknown) => { const response=await authFetch(`/projects/${projectId}/domains${path}`,{method,...(body ? {body:JSON.stringify(body)} : {})}); const result=response.status===204 ? null : await response.json(); if(!response.ok) throw new Error(result?.detail || '域名操作失败'); return result }
  const refresh=async()=>setData(await api())
  useEffect(()=>{void refresh().catch(e=>setError(e.message))},[projectId])
  const run=async(operation:()=>Promise<void>)=>{setBusy(true);setError('');try{await operation();await refresh()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  return <section className="account-card"><h2>自定义域名</h2><p>连接现有域名，将 DNS 记录指向发布入口。配置生效后检查连接。</p>{error && <p className="account-error" role="alert">{error}</p>}{data && !data.gateway_configured && <p className="project-muted">部署管理员需配置发布入口 PUBLISH_PUBLIC_HOST，并为域名配置 HTTPS。当前可添加域名并验证所有权。</p>}
    {data?.domains.map(domain=><article className="project-domain-row" key={domain.domain}><div><strong>{domain.domain}</strong><span className="project-plan">{{pending:'等待 DNS 验证',verified:'所有权已验证 · 等待发布入口',connected:'DNS 已连接'}[domain.status] || domain.status}</span></div><p className="project-muted">添加以下 DNS 记录：</p><dl className="project-details"><dt>TXT 名称</dt><dd>_atoms-verification.{domain.domain}</dd><dt>TXT 值</dt><dd>{domain.verification_token}</dd><dt>CNAME 目标</dt><dd>{data.target || '等待管理员配置发布入口'}</dd></dl><div><button className="account-connect" disabled={busy} onClick={()=>void run(async()=>{await api('/'+domain.domain+'/verify','POST')})}>检查连接</button><button className="account-connect" disabled={busy} onClick={()=>void run(async()=>{await api('/'+domain.domain,'DELETE')})}>移除域名</button></div></article>)}
    {adding ? <form onSubmit={event=>{event.preventDefault();void run(async()=>{await api('','POST',{domain:name.trim()});setName('');setAdding(false)})}}><label className="project-field">在此输入您自己的域名<input autoFocus aria-label="自定义域名" placeholder="app.example.com" value={name} onChange={e=>setName(e.target.value)} /></label><button type="button" className="account-connect" disabled={busy} onClick={()=>setAdding(false)}>取消</button> <button className="account-primary" disabled={busy || !name.trim()}>{busy ? '处理中…' : '连接'}</button></form> : <button className="account-connect" onClick={()=>setAdding(true)}>连接域名</button>}
    <div className="account-row"><div><strong>购买新域名</strong><p>购买后返回此处连接域名</p></div><a className="account-connect" href="https://www.ionos.com/domains/domain-names" target="_blank" rel="noopener noreferrer">购买新域名 ↗</a></div>
  </section>
}
