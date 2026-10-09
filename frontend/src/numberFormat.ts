const numberFormat = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 8 })
const preciseNumberFormat = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 12 })
const usdFormat = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 8 })
const creditsFormat = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

export function formatNumber(value: number | string | null | undefined, precise = false) {
  if (value == null || value === '') return '—'
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return '—'
  return (precise ? preciseNumberFormat : numberFormat).format(parsed)
}

export function formatUsd(value: number | string | null | undefined) {
  if (value == null || value === '') return '—'
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return '—'
  return `$${usdFormat.format(parsed)}`
}

export function formatCredits(value: number | string | null | undefined) {
  if (value == null || value === '') return '—'
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return '—'
  return creditsFormat.format(parsed)
}
