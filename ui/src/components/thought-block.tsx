'use client'

import { useEffect, useRef, useState } from 'react'
import { cn } from '@/lib/utils'
import { Brain, ChevronDown, Loader2 } from 'lucide-react'
import { MarkdownContent } from '@/components/markdown-content'
import type { ChatMessage } from '@/lib/api/types'

/**
 * ThoughtBlock 思考过程折叠块（多段合并版）
 *
 * 将同一对话轮次内的所有 LLM 思考段(reasoning_content)合并为单个可折叠块,
 * 解决多段思考碎片化问题(一个会话可能产生 30+ 段思考)。
 *
 * 展示策略:
 * - 标题行与 StepBlock 视觉对齐(轻量无框行, text-sm, hover bg)
 * - 多段思考按时间序排列, 段间有分隔线, 固定高度可滚动(max-h-[200px])
 * - 流式中(is_streaming=true): 默认展开, 始终自动滚到底部显示最新思考
 * - 活跃思考块(isActive=true): 默认展开(运行中会话"正在思考中"上面那条),
 *   用户切换到运行中会话时直接看到当前思考内容,不自动折叠
 * - 已完成非活跃(!isActive && !isStreaming && hasFollowingAction): 自动折叠
 * - 历史回放(完成会话): 全部折叠,用户可手动展开查看
 *
 * 展开内容用树形左虚线缩进(对齐 StepBlock 的 tools 展开风格)。
 */
export interface ThoughtBlockProps {
  /** 同一轮次的所有思考段(按时间序,由 eventsToTimeline 合并) */
  data: ChatMessage[]
  /** 该 thought 块之后是否存在 step/tool/assistant 项(由父组件计算)。
   *  true=思考阶段已结束、行动已开始,触发自动折叠; false=思考刚结束等待行动,保持展开 */
  hasFollowingAction?: boolean
  /** 是否为运行中会话的当前活跃思考块(最后一个 thought 项)。
   *  true=运行中会话"正在思考中"上面那条思考,默认展开不折叠,用户切换会话即可看到当前思考 */
  isActive?: boolean
  className?: string
}

export function ThoughtBlock({ data, hasFollowingAction = false, isActive = false, className }: ThoughtBlockProps) {
  const segments = data
  // 任一段在流式中则整体为流式状态(通常只有最后一段在流式)
  const isStreaming = segments.some((s) => s.is_streaming === true)
  const segmentCount = segments.length
  const totalChars = segments.reduce((n, s) => n + (s.message?.length ?? 0), 0)

  // 默认展开: 流式中看实时思考, 或运行中会话的活跃思考块(正在思考中上面那条)
  const [expanded, setExpanded] = useState(isStreaming || isActive)
  const scrollRef = useRef<HTMLDivElement>(null)

  // 自动折叠: 仅非活跃且非流式 + 后续行动出现时折叠。
  // - 活跃思考块(isActive=true): 保持展开,用户切换到运行中会话时看到当前思考
  // - 流式中(isStreaming=true): 保持展开,看实时思考
  // - 已完成非活跃(!isActive && !isStreaming && hasFollowingAction): 折叠
  useEffect(() => {
    if (!isActive && !isStreaming && hasFollowingAction) {
      setExpanded(false)
    }
  }, [isStreaming, hasFollowingAction, isActive])

  // 流式时始终自动滚到底部(展示最新思考内容)
  useEffect(() => {
    if (!isStreaming || !expanded || !scrollRef.current) return
    const el = scrollRef.current
    // 用 requestAnimationFrame 确保 DOM 渲染完成后再滚动
    requestAnimationFrame(() => {
      el.scrollTop = el.scrollHeight
    })
  }, [isStreaming, expanded, segments])

  // 标题文案: 流式显示"思考中",完成显示"已深度思考"
  const headerLabel = isStreaming ? '思考中' : '已深度思考'

  // 字数显示: 超 1000 字显示 k 单位
  const charsLabel =
    totalChars > 1000 ? `${(totalChars / 1000).toFixed(1)}k` : `${totalChars}字`

  // 紧凑的元信息: 段数 + 字数(仅完成态显示)
  const metaLabel = !isStreaming && totalChars > 0
    ? segmentCount > 1 ? `${segmentCount}段 · ${charsLabel}` : charsLabel
    : null

  return (
    <div className={cn('flex flex-col mt-3', className)}>
      {/* 紧凑标题行(对齐 StepBlock 风格: 无框, text-sm, hover bg) */}
      <div
        role="button"
        tabIndex={0}
        onClick={() => setExpanded((prev) => !prev)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            setExpanded((prev) => !prev)
          }
        }}
        className="text-sm w-full cursor-pointer flex gap-2 items-center truncate text-gray-500 rounded-md hover:bg-gray-50/80 transition-colors outline-none focus-visible:ring-2 focus-visible:ring-gray-300"
      >
        <div className="flex flex-row gap-2 items-center truncate min-w-0 flex-1">
          {isStreaming ? (
            <Loader2 className="size-4 animate-spin flex-shrink-0 text-gray-400" />
          ) : (
            <Brain className="size-4 flex-shrink-0 text-gray-400" />
          )}
          <span className="truncate">{headerLabel}</span>
          {metaLabel && (
            <span className="text-xs text-gray-400 flex-shrink-0">{metaLabel}</span>
          )}
        </div>
        <ChevronDown
          className={cn(
            'size-4 flex-shrink-0 text-gray-400 transition-transform',
            expanded && 'rotate-180'
          )}
        />
      </div>
      {/* 展开内容: 树形左虚线缩进(对齐 StepBlock tools 展开风格) */}
      {expanded && totalChars > 0 && (
        <div className="flex">
          <div className="w-6 relative flex-shrink-0">
            <div className="absolute left-[7px] top-2 bottom-0 w-[1px] border-l border-dashed border-gray-300" />
          </div>
          <div
            ref={scrollRef}
            className="flex-1 min-w-0 pt-2 max-h-[200px] overflow-y-auto pb-2"
          >
            {segments.map((seg, idx) => {
              const content = seg.message ?? ''
              if (!content) return null
              return (
                <div
                  key={idx}
                  className={cn(idx > 0 && 'mt-2 border-t border-gray-200/50 pt-2')}
                >
                  {segmentCount > 1 && (
                    <div className="text-xs text-gray-400 mb-1 select-none">第 {idx + 1} 段</div>
                  )}
                  <MarkdownContent content={content}  muted={true}  className="italic" />
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
