/**
 * VNC WebSocket URL 构造工具
 *
 * 供内嵌 VNC (embedded-vnc) 和全屏 VNC (vnc-overlay) 共用,
 * 确保两地连接同一后端 WS 端点 /sessions/{sessionId}/vnc。
 */

/**
 * 根据会话 ID 构造 VNC WebSocket 连接 URL
 *
 * @param sessionId 会话 ID
 * @returns WebSocket URL (ws:// 或 wss://)
 */
export function buildVNCUrl(sessionId: string): string {
  const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://10.235.127.227:8000/api'

  let host: string
  let pathname: string
  let isHttps: boolean

  try {
    const url = new URL(apiBase)
    host = url.host
    pathname = url.pathname
    isHttps = url.protocol === 'https:'
  } catch {
    host = window.location.host
    pathname = apiBase
    isHttps = window.location.protocol === 'https:'
  }

  const protocol = isHttps ? 'wss:' : 'ws:'
  return `${protocol}//${host}${pathname}/sessions/${sessionId}/vnc`
}
