import { useEffect, useState } from 'react'
import { Download, File, X } from 'lucide-react'
import type { UploadAttachment } from './media'

export type PendingAttachmentItem = {
  key: string
  attachment: UploadAttachment
  onRemove: () => void
}

type Preview = { name: string; mime: string; url?: string; text?: string }

function extension(name: string) {
  return name.split('.').pop()?.toUpperCase() || 'FILE'
}

export default function PendingAttachments({ items, className = '' }: { items: PendingAttachmentItem[]; className?: string }) {
  const [preview, setPreview] = useState<Preview | null>(null)

  useEffect(() => {
    const url = preview?.url
    if (url?.startsWith('blob:')) return () => URL.revokeObjectURL(url)
  }, [preview])

  const openPreview = async (attachment: UploadAttachment) => {
    if (attachment.kind === 'image') {
      setPreview({ name: attachment.name, mime: attachment.mime, url: attachment.preview })
      return
    }
    const bytes = Uint8Array.from(atob(attachment.data), character => character.charCodeAt(0))
    const blob = new Blob([bytes], { type: attachment.mime })
    if (attachment.mime.startsWith('text/') || ['application/json', 'application/javascript'].includes(attachment.mime)) {
      setPreview({ name: attachment.name, mime: attachment.mime, text: await blob.text() })
    } else {
      setPreview({ name: attachment.name, mime: attachment.mime, url: URL.createObjectURL(blob) })
    }
  }

  return <>
    {!!items.length && <div className={`pending-attachments ${className}`}>
      {items.map(({ key, attachment, onRemove }) => <div className="pending-attachment" key={key}>
        <button type="button" className="pending-attachment-open" title={`预览 ${attachment.name}`} onClick={() => void openPreview(attachment)}>
          {attachment.kind === 'image' ? <img src={attachment.preview} alt="" /> : <File size={19} />}
          <span>{attachment.name}</span>
        </button>
        <button type="button" className="pending-attachment-remove" title="删除附件" aria-label={`删除 ${attachment.name}`} onClick={onRemove}><X size={14} /></button>
      </div>)}
    </div>}
    {preview && <div className="pending-preview-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) setPreview(null) }}>
      <section className="pending-preview-dialog" role="dialog" aria-modal="true" aria-label={`预览 ${preview.name}`}>
        <header><div><strong>{preview.name}</strong><span>{extension(preview.name)}</span></div><button type="button" title="关闭" aria-label="关闭预览" onClick={() => setPreview(null)}><X size={19} /></button></header>
        <div className="pending-preview-content">
          {preview.text !== undefined ? <pre>{preview.text}</pre> : preview.mime.startsWith('image/') ? <img src={preview.url} alt={preview.name} /> : preview.mime === 'application/pdf' ? <iframe src={preview.url} title={preview.name} /> : <div><File size={34} /><p>此文档类型暂不支持直接预览。</p>{preview.url && <a href={preview.url} download={preview.name}><Download size={15} />下载文档</a>}</div>}
        </div>
      </section>
    </div>}
  </>
}
