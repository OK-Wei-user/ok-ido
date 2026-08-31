'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import { SessionHeader } from '@/components/session-header'
import { ChatInput } from '@/components/chat-input'
import { PlanPanel } from '@/components/plan-panel'
import { ChatMessage } from '@/components/chat-message'
import { FilePreviewPanel } from '@/components/file-preview-panel'
import { ToolPreviewPanel } from '@/components/tool-preview-panel'
import { VNCOverlay } from '@/components/vnc-overlay'
import { useSessionDetail } from '@/hooks/use-session-detail'
import { getToolKind } from '@/components/tool-use/utils'
import {
  eventsToTimeline,
  getLatestPlanFromEvents,
} from '@/lib/session-events'
import type { ToolEvent, FileInfo } from '@/lib/api/types'
import type { AttachmentFile, TimelineItem } from '@/lib/session-events'
import { sessionApi } from '@/lib/api/session'
import { toast } from 'sonner'
import { Loader2 } from 'lucide-react'

export interface SessionDetailViewProps {
  sessionId: string
  initialMessage?: string
  initialAttachments?: string[]
  hasInitialMessage?: boolean
}

/**
 * 从 timeline 中找到最后一个非 message 类型的工具事件
 */
function findLatestTool(timeline: TimelineItem[]): ToolEvent | null {
  for (let i = timeline.length - 1; i >= 0; i--) {
    const item = timeline[i]
    if (item.kind === 'tool' && getToolKind(item.data) !== 'message') {
      return item.data
    }
    if (item.kind === 'step' && item.tools.length > 0) {
      for (let j = item.tools.length - 1; j >= 0; j--) {
        if (getToolKind(item.tools[j]) !== 'message') {
          return item.tools[j]
        }
      }
    }
  }
  return null
}

/**
 * 思考指示器: 任务运行中的状态占位(spinner)
 *
 * ThoughtBlock 内部展示思考过程文本(可折叠,永驻),
 * 底部指示器保留 spinner + "正在思考中..." 状态提示,与 ThoughtBlock 互补。
 */
function ThinkingIndicator() {
  return (
    <div className="flex items-center gap-2 text-sm text-gray-500 py-3">
      <Loader2 className="size-4 animate-spin" />
      <span>正在思考中...</span>
    </div>
  )
}

export function SessionDetailView({ sessionId, initialMessage, initialAttachments, hasInitialMessage }: SessionDetailViewProps) {
  const router = useRouter()
  const {
    session,
    files,
    events,
    loading,
    error,
    refresh,
    refreshFiles,
    sendMessage,
    streaming,
  } = useSessionDetail(sessionId, hasInitialMessage)

  const timeline = useMemo(() => eventsToTimeline(events), [events])
  const planSteps = useMemo(() => getLatestPlanFromEvents(events), [events])

  // 会话活跃状态: running(执行中) 或 waiting(等待用户输入)
  // VNC按钮、内嵌VNC、工具预览面板的VNC入口均依赖此状态
  // waiting状态下用户可能需要手动操作浏览器(如选择登录方式),VNC必须可用
  const isSessionActive = session?.status === 'running' || session?.status === 'waiting'
  // 沙箱是否仍存在(TTL内未销毁): 会话结束后沙箱仍保留,用户可通过VNC查看操作结果
  const hasSandbox = !!session?.sandbox_id

  // 运行中/等待中会话: 找到最后一个 thought 项索引,标记为活跃(展开显示当前思考)。
  // 用户切换到运行中会话时,直接看到"正在思考中"上面的当前思考内容;
  // 已完成会话返回 -1,所有 thought 项折叠(用户可手动展开)。
  const activeThoughtIndex = useMemo(() => {
    if (session?.status !== 'running' && session?.status !== 'waiting') return -1
    for (let i = timeline.length - 1; i >= 0; i--) {
      if (timeline[i].kind === 'thought') return i
    }
    return -1
  }, [timeline, session?.status])

  const [fileListOpen, setFileListOpen] = useState(false)
  const [previewFile, setPreviewFile] = useState<AttachmentFile | null>(null)
  const [previewTool, setPreviewTool] = useState<ToolEvent | null>(null)
  const [vncOpen, setVncOpen] = useState(false)
  // 预览来源: 'auto'=自动追踪(会话运行中跟随最新工具), 'user'=用户手动点击历史工具
  // VNC粘性模式: auto时VNC持续显示(不随工具状态切换断开), user时显示截图
  const [previewSource, setPreviewSource] = useState<'auto' | 'user'>('auto')
  const initialMessageSentRef = useRef(false)
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const prevToolCountRef = useRef<number>(0)
  // 会话切换后首次加载完成标记(配合 key 重挂载,每次切换重置)
  const hasInitialScrolledRef = useRef(false)

  const hasPreview = previewFile !== null || previewTool !== null

  /**
   * 将 previewTool 解析为 timeline 中最新版本的工具对象。
   * 自动跟踪设置 previewTool 时工具事件可能尚无 content（如截图），
   * 后续 SSE 更新后 timeline 中对象已刷新但 state 仍为旧引用。
   * 通过 tool_call_id 匹配获取最新版本。
   */
  const resolvedPreviewTool = useMemo(() => {
    if (!previewTool) return null
    const id = (previewTool as { tool_call_id?: string }).tool_call_id
    if (!id) return previewTool

    for (let i = timeline.length - 1; i >= 0; i--) {
      const item = timeline[i]
      if (item.kind === 'tool' && (item.data as { tool_call_id?: string }).tool_call_id === id) {
        return item.data
      }
      if (item.kind === 'step') {
        for (const t of item.tools) {
          if ((t as { tool_call_id?: string }).tool_call_id === id) return t
        }
      }
    }
    return previewTool
  }, [previewTool, timeline])

  // 任务运行中自动追踪最新工具预览（VNC 打开时暂停）
  useEffect(() => {
    if (session?.status !== 'running' || vncOpen) return

    const latestTool = findLatestTool(timeline)
    const toolCount = timeline.reduce((n, item) => {
      if (item.kind === 'tool') return n + 1
      if (item.kind === 'step') return n + item.tools.length
      return n
    }, 0)

    if (toolCount > prevToolCountRef.current && latestTool) {
      setPreviewTool(latestTool)
      setPreviewFile(null)
      setPreviewSource('auto')
      scrollContainerRef.current?.scrollTo({ top: scrollContainerRef.current.scrollHeight, behavior: 'smooth' })
    }
    prevToolCountRef.current = toolCount
  }, [timeline, session?.status, vncOpen])

  // 会话切换后首次加载完成: 运行中/等待中会话即时滚动到底部展示最新进度。
  // 配合 page.tsx 的 key={sessionId} 重挂载机制,每次会话切换 ref 重置 → 重新触发。
  // 依赖 timeline.length 确保 events 已加载(避免空 timeline 滚动无意义)。
  // 用 ResizeObserver 监听内容高度变化,等异步渲染(MarkdownContent/图片)稳定后再滚动,
  // 比固定 setTimeout 延迟更健壮(适应不同会话大小和渲染速度)。
  useEffect(() => {
    if (hasInitialScrolledRef.current || loading || !session) return

    // 非运行中/等待中会话: 标记已处理,不滚动(已完成会话用户应自主浏览)
    if (session.status !== 'running' && session.status !== 'waiting') {
      hasInitialScrolledRef.current = true
      return
    }

    // 运行中会话: 等 timeline 有内容再滚动(确保历史事件已渲染)
    if (timeline.length === 0) return

    hasInitialScrolledRef.current = true

    const container = scrollContainerRef.current
    if (!container) return

    // 内容可能异步渲染(MarkdownContent/图片等),用 ResizeObserver 监听内容高度变化。
    // 高度稳定 150ms 后滚动到底部,确保所有内容渲染完成。
    let stableTimer: ReturnType<typeof setTimeout> | null = null
    const scrollToBottom = () => {
      container.scrollTo({ top: container.scrollHeight, behavior: 'auto' })
    }

    // 监听滚动容器的第一个子元素(内容容器)的高度变化
    const contentEl = container.firstElementChild as HTMLElement | null
    const ro = new ResizeObserver(() => {
      if (stableTimer) clearTimeout(stableTimer)
      stableTimer = setTimeout(scrollToBottom, 150)
    })
    if (contentEl) ro.observe(contentEl)

    // 初始立即滚动一次(应对快速渲染场景)
    scrollToBottom()

    // 3秒后停止监听(初始加载内容应已稳定,避免长期占用)
    const stopTimer = setTimeout(() => {
      ro.disconnect()
      if (stableTimer) clearTimeout(stableTimer)
    }, 3000)

    return () => {
      ro.disconnect()
      if (stableTimer) clearTimeout(stableTimer)
      clearTimeout(stopTimer)
    }
  }, [loading, session, timeline.length])

  useEffect(() => {
    if (
      initialMessage &&
      !initialMessageSentRef.current &&
      session &&
      !loading &&
      !streaming
    ) {
      initialMessageSentRef.current = true
      sendMessage(initialMessage, initialAttachments || [])
        .then(() => {
          setTimeout(() => {
            router.replace(`/sessions/${sessionId}`)
          }, 100)
        })
        .catch((e) => {
          toast.error(e instanceof Error ? e.message : '发送消息失败')
        })
    }
  }, [initialMessage, initialAttachments, session, loading, streaming, sendMessage, sessionId, router])

  const handleSend = useCallback(
    async (message: string, uploadedFiles: FileInfo[]) => {
      try {
        const attachmentIds = uploadedFiles.map((f) => f.id)
        await sendMessage(message, attachmentIds)
      } catch (e) {
        toast.error(e instanceof Error ? e.message : '发送失败，请重试')
        throw e
      }
    },
    [sendMessage]
  )

  const handleViewAllFiles = useCallback(() => {
    refreshFiles()
    setFileListOpen(true)
  }, [refreshFiles])

  const handleFileClick = useCallback((file: AttachmentFile) => {
    setPreviewFile(file)
    setPreviewTool(null)
  }, [])

  const handleToolClick = useCallback((tool: ToolEvent) => {
    const kind = getToolKind(tool)
    if (kind === 'message') return
    setPreviewTool(tool)
    setPreviewFile(null)
    // 用户手动点击历史工具: 切换为user模式,显示该工具的截图/输出而非VNC
    setPreviewSource('user')
  }, [])

  const handleClosePreview = useCallback(() => {
    setPreviewFile(null)
    setPreviewTool(null)
  }, [])

  const handleJumpToLatest = useCallback(() => {
    const latest = findLatestTool(timeline)
    if (latest) {
      setPreviewTool(latest)
      setPreviewFile(null)
      // 跳转实时: 恢复auto模式,VNC粘性显示
      setPreviewSource('auto')
    }
    scrollContainerRef.current?.scrollTo({ top: scrollContainerRef.current.scrollHeight, behavior: 'smooth' })
  }, [timeline])

  const handleOpenVNC = useCallback(() => {
    setVncOpen(true)
  }, [])

  const handleCloseVNC = useCallback(() => {
    setVncOpen(false)
    // 关闭 VNC 后跳转到最新工具,恢复auto模式
    const latest = findLatestTool(timeline)
    if (latest && isSessionActive) {
      setPreviewTool(latest)
      setPreviewFile(null)
      setPreviewSource('auto')
      setTimeout(() => {
        scrollContainerRef.current?.scrollTo({ top: scrollContainerRef.current.scrollHeight, behavior: 'smooth' })
      }, 100)
    }
  }, [timeline, isSessionActive])

  const handleStop = useCallback(async () => {
    if (!session) return
    try {
      await sessionApi.stopSession(sessionId)
      toast.success('任务已停止')
      refresh()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '停止任务失败')
    }
  }, [session, sessionId, refresh])

  if (loading && !session) {
    return (
      <div className="relative flex flex-col h-full flex-1 min-w-0 px-4 items-center justify-center">
        {hasInitialMessage ? (
          <div className="flex items-center gap-2 text-sm text-gray-500">
            <Loader2 className="size-4 animate-spin" />
            <span>正在思考中...</span>
          </div>
        ) : (
          <p className="text-sm text-gray-500">加载中...</p>
        )}
      </div>
    )
  }

  if (error && !session) {
    return (
      <div className="relative flex flex-col h-full flex-1 min-w-0 px-4 items-center justify-center gap-2">
        <p className="text-sm text-red-600">{error.message}</p>
        <button
          type="button"
          onClick={() => refresh()}
          className="text-sm text-primary underline"
        >
          重试
        </button>
      </div>
    )
  }

  if (!session) {
    return (
      <div className="relative flex flex-col h-full flex-1 min-w-0 px-4 items-center justify-center">
        <p className="text-sm text-gray-500">未找到该任务</p>
      </div>
    )
  }

  return (
    <>
      <div className="flex flex-row h-screen w-full overflow-hidden">
        {/* 主内容区 */}
        <div className="flex flex-col flex-1 min-w-0 h-full overflow-hidden">
          <div className={`flex flex-col h-full mx-auto w-full min-w-0 px-4 ${hasPreview ? '' : 'max-w-[768px]'}`}>
            <div className="flex-shrink-0">
              <SessionHeader
                title={session.title}
                files={files}
                fileListOpen={fileListOpen}
                onFileListOpenChange={setFileListOpen}
                onFetchFiles={refreshFiles}
                onFileClick={handleFileClick}
                onOpenVNC={handleOpenVNC}
                isActive={isSessionActive}
                hasSandbox={hasSandbox}
              />
            </div>

            <div ref={scrollContainerRef} className="flex-1 overflow-y-auto">
              <div className="flex flex-col w-full gap-3 pt-3">
                {timeline.length === 0 && !streaming && !hasInitialMessage && (
                  <div className="flex items-center justify-center py-8 text-sm text-gray-500">
                    暂无对话记录，在下方输入任务或提问
                  </div>
                )}
                {timeline.map((item, index) => {
                  // thought 项: 检查后续是否存在 step/tool/assistant
                  // (表示思考阶段已结束、行动已开始,触发 ThoughtBlock 延迟折叠)
                  const hasFollowingAction =
                    item.kind === 'thought' &&
                    timeline.slice(index + 1).some(
                      (t) => t.kind === 'step' || t.kind === 'tool' || t.kind === 'assistant'
                    )
                  return (
                    <ChatMessage
                      key={item.id}
                      item={item}
                      hasFollowingAction={hasFollowingAction}
                      isActiveThought={index === activeThoughtIndex}
                      onViewAllFiles={handleViewAllFiles}
                      onFileClick={handleFileClick}
                      onToolClick={handleToolClick}
                    />
                  )
                })}

                {(session?.status === 'running' || (hasInitialMessage && !initialMessageSentRef.current)) && (
                  <ThinkingIndicator />
                )}

                <div className="h-[140px]" />
              </div>
            </div>

            <div className="flex-shrink-0 bg-[#f8f8f7] py-4">
              <PlanPanel className="mb-2" steps={planSteps} />
              <ChatInput
                onSend={handleSend}
                sessionId={sessionId}
                isRunning={session?.status === 'running'}
                onStop={handleStop}
              />
            </div>
          </div>
        </div>

        {/* 文件预览面板 */}
        {previewFile && (
          <div className="flex-shrink-0 w-[600px] h-full animate-in slide-in-from-right duration-300">
            <FilePreviewPanel file={previewFile} onClose={handleClosePreview} />
          </div>
        )}

        {/* 工具预览面板 */}
        {resolvedPreviewTool && (
          <div className="flex-shrink-0 w-[600px] h-full py-2 pr-2 animate-in slide-in-from-right duration-300">
            <ToolPreviewPanel
              tool={resolvedPreviewTool}
              onClose={handleClosePreview}
              onJumpToLatest={handleJumpToLatest}
              onOpenVNC={(isSessionActive || hasSandbox) ? handleOpenVNC : undefined}
              sessionId={sessionId}
              isLive={isSessionActive && !vncOpen}
              previewSource={previewSource}
            />
          </div>
        )}
      </div>

      {/* noVNC 全屏远程桌面覆盖层 */}
      {vncOpen && (
        <VNCOverlay sessionId={sessionId} onClose={handleCloseVNC} />
      )}
    </>
  )
}
