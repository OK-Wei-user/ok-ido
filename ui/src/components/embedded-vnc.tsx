'use client'

import { useMemo, useCallback, useState } from 'react'
import dynamic from 'next/dynamic'
import { Loader2, WifiOff, Radio } from 'lucide-react'
import { buildVNCUrl } from '@/lib/vnc-url'
import type { VNCStatus } from '@/components/vnc-viewer'

/**
 * VNCViewer 动态导入 (SSR 安全)
 * noVNC 依赖浏览器 API,必须在客户端加载
 */
const VNCViewer = dynamic(
  () => import('@/components/vnc-viewer').then((m) => ({ default: m.VNCViewer })),
  { ssr: false },
)

export interface EmbeddedVNCProps {
  /** 会话 ID,用于构造 VNC WS URL */
  sessionId: string
}

/**
 * 内嵌 VNC 容器 — 嵌入工具预览面板的实时远程桌面
 *
 * 与 VNCOverlay (全屏) 对称,但更轻量:
 * - viewOnly=true: 仅监控,不干扰 agent 浏览器操作
 * - 无 ESC 监听、无 body overflow 锁定、无退出按钮
 * - 连接状态 UI 精简: 连接中 spinner / LIVE 徽标 / 错误蒙层
 */
export function EmbeddedVNC({ sessionId }: EmbeddedVNCProps) {
  const vncUrl = useMemo(() => buildVNCUrl(sessionId), [sessionId])
  const [status, setStatus] = useState<VNCStatus>('connecting')
  const [errorDetail, setErrorDetail] = useState('')

  /**
   * 稳定回调: useCallback 保证引用不变,
   * 避免 VNCViewer useEffect 因回调变化触发重连
   */
  const handleStatusChange = useCallback((s: VNCStatus, detail?: string) => {
    setStatus(s)
    if (s === 'error' || s === 'disconnected') {
      setErrorDetail(detail || '连接失败')
    }
  }, [])

  const hasError = status === 'error' || status === 'disconnected'

  return (
    <div className="relative w-full h-full bg-black">
      <VNCViewer url={vncUrl} viewOnly onStatusChange={handleStatusChange} />

      {/* LIVE 徽标: 连接成功时显示 */}
      {status === 'connected' && (
        <div className="absolute top-2 left-2 z-10 flex items-center gap-1 px-2 py-0.5 rounded-full bg-red-600/90 text-white text-xs font-medium">
          <Radio size={10} className="animate-pulse" />
          <span>实时</span>
        </div>
      )}

      {/* 连接中蒙层 */}
      {status === 'connecting' && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/80 z-10">
          <Loader2 className="size-5 text-white animate-spin" />
          <span className="text-xs text-gray-300">连接远程桌面...</span>
        </div>
      )}

      {/* 错误/断开蒙层 */}
      {hasError && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/85 z-10">
          <WifiOff className="size-6 text-gray-400" />
          <span className="text-xs text-gray-400 text-center px-4">
            {errorDetail}
          </span>
        </div>
      )}
    </div>
  )
}
