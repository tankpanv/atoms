import { useEffect, useState } from 'react'
import { Check, ChevronDown, Copy, ExternalLink, Globe2, LoaderCircle, ShieldCheck, X } from 'lucide-react'
import './publish-dialog.css'
import { copyLink } from './clipboard'

export default function PublishDialog({ url, published, busy, success, canPublish, error, onPublish, onUnpublish, onShare, onClose }: {
  url: string; published: boolean; busy: boolean; success: boolean; canPublish: boolean; error: string;
  onPublish: () => void; onUnpublish: () => void; onShare: () => void; onClose: () => void;
}) {
  useEffect(() => { const escape = (event: KeyboardEvent) => { if (event.key === 'Escape' && !busy) onClose() }; document.addEventListener('keydown', escape); return () => document.removeEventListener('keydown', escape) }, [busy, onClose])
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [copied, setCopied] = useState(false)
  const [copyError, setCopyError] = useState('')
  const copy = async () => {
    try { await copyLink(url); setCopied(true); setCopyError('') }
    catch (error) { setCopied(false); setCopyError((error as Error).message) }
  }
  return <div className="publish-popover-layer" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose() }}>
    <section className={`publish-dialog ${success ? 'publish-dialog-success' : ''}`} role="dialog" aria-modal="true" aria-label={success ? '发布成功' : published && !busy ? '已发布' : '发布'}>
      <button className="publish-dialog-close" aria-label="关闭发布窗口" disabled={busy} onClick={onClose}><X size={18} /></button>
      {success ? <>
        <div className="publish-celebration" aria-hidden="true"><span>✦</span><span>🎉</span><span>✧</span></div>
        <h2 className="publish-success-title">您的 App 已上线！</h2>
        <p className="publish-success-description">分享链接，让大家看看你的作品。</p>
        <div className="publish-dialog-url"><input aria-label="已发布网站 URL" readOnly value={url} /><button aria-label={copied ? '链接已复制' : '复制链接'} onClick={() => void copy()}>{copied ? <Check size={17} /> : <Copy size={17} />}</button></div>
        <div className="publish-dialog-footer"><a className="publish-dialog-secondary" href={url} target="_blank" rel="noopener noreferrer">查看 App <ExternalLink size={15} /></a><button className="publish-dialog-primary" onClick={onShare}>分享</button></div>
      </> : <>
        <h2>{published && !busy ? <><i className="publish-online-dot" />已发布</> : '发布'}</h2>
        {published && <div className="publish-online-row"><strong>应用状态</strong><span className="publish-online-badge">● 在线</span></div>}
        <label className="publish-dialog-label" htmlFor="deployment-url">你的网站 URL</label>
        <div className="publish-dialog-url"><input id="deployment-url" readOnly value={url} /><button aria-label={copied ? '链接已复制' : '复制链接'} disabled={!published || busy} onClick={() => void copy()}>{copied ? <Check size={17} /> : <Copy size={17} />}</button></div>
        <div className="publish-settings">
          <button className="publish-settings-toggle" aria-expanded={settingsOpen} onClick={() => setSettingsOpen(!settingsOpen)}><strong>设置</strong><ChevronDown size={19} className={settingsOpen ? 'expanded' : ''} /></button>
          {settingsOpen && <div className="publish-settings-body"><div><span><Globe2 size={17} />发布资源存储</span><strong>MinIO</strong></div><p>发布构建资源后，访问此链接即可打开网站。</p>{published && <button className="publish-offline-button" disabled={busy} onClick={onUnpublish}>停止公开访问</button>}</div>}
        </div>
        {!canPublish && !busy && <p className="publish-dialog-message">请等待项目构建完成后发布。</p>}
        {error && <p className="publish-dialog-error" role="alert">{error}</p>}
        <div className="publish-dialog-footer">
          {busy ? <span className="publish-progress" role="status"><LoaderCircle size={19} className="publish-spin" />正在上传资源</span> : published ? <button className="publish-dialog-secondary" onClick={onShare}>分享</button> : <span className="publish-progress"><ShieldCheck size={19} />准备发布</span>}
          <button className="publish-dialog-primary" disabled={!canPublish || busy} onClick={onPublish}>{busy ? <><LoaderCircle size={18} className="publish-spin" />正在发布</> : published ? '更新发布' : '发布'}</button>
        </div>
      </>}
      {copyError && <p className="publish-dialog-error" role="alert">{copyError}</p>}
    </section>
  </div>
}
