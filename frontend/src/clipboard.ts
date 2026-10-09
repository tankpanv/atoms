export async function copyLink(url: string) {
  try {
    await navigator.clipboard.writeText(url)
  } catch {
    throw new Error(window.isSecureContext
      ? '复制失败，请选中链接手动复制。'
      : '非 HTTPS 环境下浏览器不支持复制，请手动复制。')
  }
}
