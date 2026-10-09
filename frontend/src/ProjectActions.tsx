import { useCallback, useEffect, useLayoutEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react'
import { createPortal } from 'react-dom'
import { MoreHorizontal, Trash2, X } from 'lucide-react'

export function RecentProject({ project, active, open, onMenuChange, onOpen, onDelete }: { project: { id: string; title: string }; active: boolean; open: boolean; onMenuChange: Dispatch<SetStateAction<string | null>>; onOpen: () => void; onDelete: () => void }) {
  const setOpen = useCallback((value: boolean) => {
    onMenuChange(current => value ? project.id : current === project.id ? null : current)
  }, [onMenuChange, project.id])
  const [position, setPosition] = useState({ left: 0, top: 0 })
  const trigger = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    if (!open || !trigger.current || !menu.current) return
    const box = (trigger.current.closest('.recent-project') || trigger.current).getBoundingClientRect()
    const { width, height } = menu.current.getBoundingClientRect()
    setPosition({ left: Math.max(8, Math.min(window.innerWidth - width - 8, box.right + 8)), top: Math.max(8, Math.min(window.innerHeight - height - 8, box.top)) })
  }, [open])
  useEffect(() => {
    if (!open) return
    let timer: ReturnType<typeof setTimeout> | undefined
    const clear = () => { clearTimeout(timer); timer = undefined }
    const move = (event: PointerEvent) => {
      const nearby = [trigger.current, menu.current].some(node => {
        if (!node) return false
        const b = node.getBoundingClientRect()
        return event.clientX >= b.left - 24 && event.clientX <= b.right + 24 && event.clientY >= b.top - 24 && event.clientY <= b.bottom + 24
      })
      if (nearby) clear()
      else if (!timer) timer = setTimeout(() => setOpen(false), 280)
    }
    const outside = (event: PointerEvent) => { if (![trigger.current, menu.current].some(node => node?.contains(event.target as Node))) setOpen(false) }
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape') { setOpen(false); trigger.current?.focus() } }
    const scroll = () => setOpen(false)
    document.addEventListener('pointermove', move)
    document.addEventListener('pointerdown', outside)
    document.addEventListener('keydown', key)
    window.addEventListener('resize', scroll)
    document.addEventListener('scroll', scroll, true)
    return () => { clear(); document.removeEventListener('pointermove', move); document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', key); window.removeEventListener('resize', scroll); document.removeEventListener('scroll', scroll, true) }
  }, [open, setOpen])
  return <div className={`recent-project ${active ? 'active' : ''} ${open ? 'menu-open' : ''}`} onPointerEnter={e => { if (e.pointerType === 'mouse') onMenuChange(current => current === project.id ? current : null) }}>
    <button className="recent-project-title" onClick={() => { setOpen(false); onOpen() }} title={project.title}>{project.title}</button>
    <button ref={trigger} className="recent-project-more" aria-label={`更多：${project.title}`} aria-haspopup="menu" aria-expanded={open} onPointerEnter={e => { if (e.pointerType === 'mouse') setOpen(true) }} onClick={() => setOpen(!open)} onKeyDown={e => { if (e.key === 'ArrowDown') { e.preventDefault(); setOpen(true); requestAnimationFrame(() => menu.current?.querySelector('button')?.focus()) } }}><MoreHorizontal size={18} /></button>
    {open && createPortal(<div ref={menu} className="recent-project-menu" role="menu" style={position} onBlur={e => { if (!e.currentTarget.contains(e.relatedTarget) && e.relatedTarget !== trigger.current) setOpen(false) }}><button role="menuitem" onClick={() => { setOpen(false); onDelete() }}><Trash2 size={14} />删除项目</button></div>, document.body)}
  </div>
}

export function DeleteProjectDialog({ title, busy, error, onCancel, onConfirm }: { title: string; busy: boolean; error: string; onCancel: () => void; onConfirm: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const cancel = useRef<HTMLButtonElement>(null)
  useEffect(() => { dialog.current?.showModal(); cancel.current?.focus() }, [])
  return <dialog ref={dialog} className="delete-project-dialog" aria-labelledby="delete-project-heading" onCancel={e => { e.preventDefault(); if (!busy) onCancel() }}>
    <div className="modal-top"><h2 id="delete-project-heading">删除项目</h2><button aria-label="关闭删除确认" disabled={busy} onClick={onCancel}><X size={18} /></button></div>
    <div className="modal-body"><strong>{title}</strong><p>确认删除这个项目吗？删除会清除项目的所有内容，包括代码目录、对话、附件、缓存、容器、发布资源、项目连接器关联及配置。</p><p className="delete-project-warning">此操作无法撤销，请注意风险，建议先下载备份。正在运行的任务也将终止。</p>{error && <p role="alert" className="delete-project-warning">{error}</p>}<div className="delete-project-footer"><button ref={cancel} className="secondary-button" disabled={busy} onClick={onCancel}>取消</button><button className="delete-project-confirm" disabled={busy} onClick={onConfirm}>{busy ? '正在清理项目…' : '确认删除'}</button></div></div>
  </dialog>
}
