import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Check, ChevronDown } from 'lucide-react'
import './build-tiers.css'

export type BuildTier = 'normal' | 'deep' | 'advanced'
export const buildTiers: { id: BuildTier; label: string; description: string }[] = [
  { id: 'normal', label: '普通', description: '准确、完整实现你的要求，聚焦核心功能。' },
  { id: 'deep', label: '深度', description: '深入分析需求，补充容易遗漏的细节，系统优化设计和体验。' },
  { id: 'advanced', label: '高级', description: '全面规划完整链路与必要扩展，完善架构、体验，并深入验证关键风险。' },
]

export function savedBuildTier(): BuildTier {
  const saved = localStorage.getItem('atoms-build-tier')
  return buildTiers.find(tier => tier.id === saved)?.id || 'normal'
}

export default function BuildTierSelector({ value, onChange, disabled = false }: {
  value: BuildTier; onChange: (tier: BuildTier) => void; disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const label = buildTiers.find(tier => tier.id === value)?.label || '普通'
  useLayoutEffect(() => {
    if (!open) return
    const position = () => {
      const menu = menuRef.current
      if (!menu) return
      menu.style.transform = ''
      const box = menu.getBoundingClientRect()
      const left = Math.max(12, Math.min(box.x, window.innerWidth - box.width - 12))
      const top = Math.max(12, Math.min(box.y, window.innerHeight - box.height - 12))
      menu.style.transform = `translate(${left - box.x}px, ${top - box.y}px)`
    }
    position()
    window.addEventListener('resize', position)
    window.addEventListener('scroll', position, true)
    return () => {
      window.removeEventListener('resize', position)
      window.removeEventListener('scroll', position, true)
    }
  }, [open])
  useEffect(() => {
    if (disabled) setOpen(false)
  }, [disabled])
  useEffect(() => {
    if (!open) return
    const outside = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false)
    }
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setOpen(false); trigger.current?.focus() }
    }
    document.addEventListener('pointerdown', outside, true)
    document.addEventListener('keydown', keydown)
    ref.current?.querySelector<HTMLButtonElement>('[aria-checked="true"]')?.focus()
    return () => {
      document.removeEventListener('pointerdown', outside, true)
      document.removeEventListener('keydown', keydown)
    }
  }, [open])
  return <div className="build-tier-selector" ref={ref}>
    <button ref={trigger} type="button" className="build-tier-trigger" disabled={disabled}
      aria-label={`构建档位：${label}`} aria-haspopup="menu" aria-expanded={open}
      title="选择需求分析、设计、实现与验证的深度" onClick={() => setOpen(!open)}>
      {label}<ChevronDown size={13} />
    </button>
    {open && <div ref={menuRef} className="build-tier-menu" role="menu" aria-label="构建档位" onKeyDown={event => {
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
      event.preventDefault()
      const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button'))
      const index = items.indexOf(document.activeElement as HTMLButtonElement)
      items[(index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus()
    }}>
      <div className="build-tier-heading">构建档位</div>
      {buildTiers.map(tier => <button type="button" key={tier.id} role="menuitemradio"
        aria-checked={value === tier.id} onClick={() => { onChange(tier.id); setOpen(false); trigger.current?.focus() }}>
        <span><strong>{tier.label}</strong><small>{tier.description}</small></span>
        {value === tier.id && <Check size={16} />}
      </button>)}
    </div>}
  </div>
}
