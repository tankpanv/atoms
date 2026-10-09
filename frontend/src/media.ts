export type MediaAttachment = { name: string; mime: string; data: string; preview: string; kind: 'image' }
export type DocumentAttachment = { name: string; mime: string; data: string; kind: 'document' }
export type UploadAttachment = MediaAttachment | DocumentAttachment

export const mediaAccept = 'image/png,image/jpeg,image/webp,image/gif'
export const documentAccept = '.txt,.md,.json,.csv,.html,.css,.js,.jsx,.ts,.tsx,.pdf,.docx'

const documentTypes: Record<string, string> = {
  txt: 'text/plain', md: 'text/markdown', json: 'application/json', csv: 'text/csv',
  html: 'text/html', css: 'text/css', js: 'text/javascript', jsx: 'text/javascript',
  ts: 'text/plain', tsx: 'text/plain', pdf: 'application/pdf',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
}

const limits: Record<string, number> = {
  'image/png': 5, 'image/jpeg': 5, 'image/webp': 5, 'image/gif': 5,
}

export async function readMediaFiles(files: File[], existing: MediaAttachment[] = []): Promise<MediaAttachment[]> {
  if (files.length + existing.length > 4) throw new Error('一次最多添加 4 张图片')
  const total = files.reduce((size, file) => size + file.size, existing.reduce((size, item) => size + item.data.length * 3 / 4, 0))
  if (total > 20 * 1024 * 1024) throw new Error('附件总大小不能超过 20 MB')
  return Promise.all(files.map(async file => {
    if (!limits[file.type]) throw new Error(`不支持的媒体类型：${file.type || file.name}`)
    if (file.size > limits[file.type] * 1024 * 1024) throw new Error(`${file.name} 超过大小限制`)
    const preview = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.onerror = () => reject(new Error(`${file.name} 读取失败`))
      reader.readAsDataURL(file)
    })
    return { name: file.name, mime: file.type, data: preview.split(',')[1], preview, kind: 'image' as const }
  }))
}

export async function readDocumentFiles(files: File[], existing: DocumentAttachment[] = [],
                                        images: MediaAttachment[] = []): Promise<DocumentAttachment[]> {
  if (files.length + existing.length > 8) throw new Error('一次最多添加 8 份文档')
  const total = [...files].reduce((size, file) => size + file.size,
    [...existing, ...images].reduce((size, item) => size + item.data.length * 3 / 4, 0))
  if (total > 40 * 1024 * 1024) throw new Error('附件总大小不能超过 40 MB')
  return Promise.all(files.map(async file => {
    const extension = file.name.split('.').pop()?.toLowerCase() || ''
    const mime = documentTypes[extension]
    if (!mime) throw new Error(`不支持的文档类型：${file.name}`)
    const limit = extension === 'pdf' || extension === 'docx' ? 5 : 2
    if (!file.size || file.size > limit * 1024 * 1024) throw new Error(`${file.name} 超过大小限制或为空`)
    const encoded = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.onerror = () => reject(new Error(`${file.name} 读取失败`))
      reader.readAsDataURL(file)
    })
    return { name: file.name, mime, data: encoded.split(',')[1], kind: 'document' as const }
  }))
}

export const mediaPayload = (items: UploadAttachment[]) => items.map(({ name, mime, data }) => ({ name, mime, data }))
