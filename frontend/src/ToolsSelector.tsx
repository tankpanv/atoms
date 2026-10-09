import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { ChevronDown, Wrench } from 'lucide-react'
import './tools-selector.css'

export type BuildTool = 'browser_check'
export function savedBuildTools(): BuildTool[] {
  try { return JSON.parse(localStorage.getItem('atoms-build-tools') || '[]').includes('browser_check') ? ['browser_check'] : [] }
  catch { return [] }
}

export default function ToolsSelector({ value, onChange, disabled = false }: {
  value: BuildTool[]; onChange: (tools: BuildTool[]) => void; disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [position, setPosition] = useState({ left: 0, top: 0 })
  const trigger = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDivElement>(null)
  useEffect(() => { if (disabled) setOpen(false) }, [disabled])
  useLayoutEffect(() => {
    if (!open) return
    const place = () => {
      const anchor = trigger.current?.getBoundingClientRect()
      const box = menu.current?.getBoundingClientRect()
      if (!anchor || !box) return
      setPosition({ left: Math.max(12, Math.min(anchor.left, window.innerWidth - box.width - 12)),
        top: Math.max(12, anchor.top >= box.height + 20 ? anchor.top - box.height - 8 : Math.min(anchor.bottom + 8, window.innerHeight - box.height - 12)) })
    }
    place()
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    return () => { window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true) }
  }, [open])
  useEffect(() => {
    if (!open) return
    const outside = (event: PointerEvent) => {
      if (!trigger.current?.contains(event.target as Node) && !menu.current?.contains(event.target as Node)) setOpen(false)
    }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { setOpen(false); trigger.current?.focus() } }
    document.addEventListener('pointerdown', outside, true)
    document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', outside, true); document.removeEventListener('keydown', escape) }
  }, [open])
  return <div className="tools-selector">
    <button type="button" ref={trigger} className="tools-trigger" aria-label="Tools" aria-expanded={open}
      aria-controls={open ? 'build-tools-popover' : undefined} disabled={disabled} onClick={() => setOpen(!open)}>
      <Wrench size={14} />Tools{value.length > 0 && <span className="tools-count">{value.length}</span>}<ChevronDown size={13} />
    </button>
    {open && createPortal(<div ref={menu} id="build-tools-popover" className="tools-popover" style={position} role="group" aria-label="构建工具">
      <div className="tools-heading">选择工具</div>
      <label><input type="checkbox" checked={value.includes('browser_check')} onChange={event => onChange(event.target.checked ? ['browser_check'] : [])} />
        <span><strong>浏览器验收</strong><small>检查应用页面和核心交互</small></span>
      </label>
    </div>, document.body)}
  </div>
}
