import { authFetch } from './auth'

export type Account = {
  user_id: string; email: string; display_name: string; avatar: string;
  workspace_name: string; workspace_description: string; workspace_avatar: string;
  credits: number; plan: string; created_at: string; billing_enabled: boolean; checkout_enabled: boolean;
  available_credits: number; reserved_credits: number; credits_per_usd: number;
  free_granted: number; plan_credits: number; plan_interval: string; paid_until: string | null; next_grant_at: string | null;
  preferences: { language: string; theme: string; default_model: string; visibility: string; show_credits: boolean; sound: string; email_notifications: boolean; remove_badge: boolean; storage_metered: boolean }
}
export function requestKey() {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}
export async function accountApi<T>(path = '', options?: RequestInit): Promise<T> {
  const response = await authFetch(`/account${path}`, options)
  const data = await response.json()
  if (!response.ok) throw new Error(data.detail || '请求失败')
  return data as T
}
export type AccountPatch = Omit<Partial<Account>, 'preferences'> & { preferences?: Partial<Account['preferences']> }
export const saveAccount = (patch: AccountPatch) => accountApi<Account>('', { method: 'PATCH', body: JSON.stringify(patch) })
export async function avatarData(file: File): Promise<string> {
  if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type) || file.size > 2_000_000) throw new Error('请上传 2MB 以内的 PNG、JPEG 或 WebP 图片')
  return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = () => reject(new Error('无法读取图片')); reader.readAsDataURL(file) })
}
