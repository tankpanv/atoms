import { useEffect, useRef, useState } from 'react'
import { Check, ChevronRight, CircleHelp, CircleUserRound, Contrast, Gem, Globe, Hexagon, LogOut, Moon, Sun, Monitor, Ticket } from 'lucide-react'
import type { Account } from './account'

export function AccountAvatar({ account, workspace = false }: { account: Account; workspace?: boolean }) {
  const image = workspace ? account.workspace_avatar : account.avatar
  return <span className={`account-avatar ${workspace ? 'workspace' : ''}`}>{image ? <img src={image} alt="头像" /> : (workspace ? account.workspace_name : account.display_name).charAt(0)}</span>
}

export default function AccountMenu({ account, open, onToggle, onClose, onSettings, onProfile, onRedeem, onTheme, onLogout }: {
  account: Account; open: boolean; onToggle: () => void; onClose: () => void; onSettings: (page: string) => void;
  onProfile: () => void; onRedeem: () => void; onTheme: (theme: string) => void; onLogout: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [appearance, setAppearance] = useState(false)
  useEffect(() => {
    if (!open) { setAppearance(false); return }
    const outside = (event: PointerEvent) => { if (!ref.current?.contains(event.target as Node)) onClose() }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    document.addEventListener('pointerdown', outside, true); document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', outside, true); document.removeEventListener('keydown', escape) }
  }, [open, onClose])
  const action = (fn: () => void) => { onClose(); fn() }
  return <div className={`account-menu-anchor ${open ? 'menu-open' : ''}`} ref={ref}>
    <button className="account-menu-trigger" aria-label="个人菜单" aria-expanded={open} onClick={onToggle}><AccountAvatar account={account} /></button>
    {open && <><div className="account-menu-dismiss" aria-hidden="true" onPointerDown={onClose} /><div className="account-menu" role="menu" aria-label="个人菜单">
      <div className="account-menu-header"><AccountAvatar account={account} /><div><strong>{account.display_name}</strong><small>{account.email}</small></div></div>
      <button role="menuitem" onClick={() => action(() => onSettings('profile'))}><Hexagon />设置</button>
      <button role="menuitem" onClick={() => action(() => onSettings('plan'))}><Gem />套餐</button>
      <button role="menuitem" onClick={() => action(onProfile)}><CircleUserRound />个人主页</button>
      <button role="menuitem" onClick={() => action(onRedeem)}><Ticket />兑换</button>
      <div className="appearance-anchor" onMouseEnter={() => setAppearance(true)} onMouseLeave={() => setAppearance(false)}>
        <button role="menuitem" aria-expanded={appearance} onClick={() => setAppearance(!appearance)}><Contrast />外观<ChevronRight className="menu-chevron" /></button>
        {appearance && <div className="appearance-menu" role="menu" aria-label="外观">{[['light', '亮色', Sun], ['dark', '暗色', Moon], ['system', '系统', Monitor]].map(([value, label, Icon]) => { const ItemIcon = Icon as typeof Sun; return <button key={String(value)} role="menuitemradio" aria-checked={account.preferences.theme === value} onClick={() => action(() => onTheme(String(value)))}><ItemIcon />{String(label)}{account.preferences.theme === value && <Check className="menu-chevron" />}</button> })}</div>}
      </div>
      <hr />
      <a role="menuitem" href="https://help.atoms.dev/zh" target="_blank" rel="noreferrer"><CircleHelp />帮助中心</a>
      <a role="menuitem" href="https://atoms.dev/zh" target="_blank" rel="noreferrer"><Globe />官网首页</a>
      <button role="menuitem" onClick={() => action(onLogout)}><LogOut />退出登录</button>
    </div></>}
  </div>
}
