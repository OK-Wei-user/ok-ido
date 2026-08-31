'use client'

import { useState } from 'react'
import { cn } from '@/lib/utils'
import { CheckIcon, ChevronDown, Languages } from 'lucide-react'
import { ToolUse } from '@/components/tool-use'
import { AttachmentsMessage } from '@/components/attachments-message'
import { MarkdownContent } from '@/components/markdown-content'
import { ThoughtBlock } from '@/components/thought-block'
import type { ToolEvent } from '@/lib/api/types'
import { type TimelineItem, type AttachmentFile, getToolTimeLabel } from '@/lib/session-events'

export interface ChatMessageProps {
  className?: string
  item: TimelineItem
  /** thought 项专用: 该块之后是否存在 step/tool/assistant(触发延迟折叠) */
  hasFollowingAction?: boolean
  /** thought 项专用: 是否为运行中会话的当前活跃思考块(最后一个 thought 项)。
   *  true=默认展开不折叠,用户切换到运行中会话时直接看到当前思考内容 */
  isActiveThought?: boolean
  onViewAllFiles?: () => void
  onFileClick?: (file: AttachmentFile) => void
  onToolClick?: (tool: ToolEvent) => void
}

function ToolRow({
  className,
  timeLabel,
  children,
}: {
  className?: string
  timeLabel?: string
  children: React.ReactNode
}) {
  const [hovered, setHovered] = useState(false)
  return (
    <div
      className={cn(
        'flex items-center justify-between gap-2 mt-3 w-full min-w-0',
        className
      )}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <div className="min-w-0 flex-shrink-0">{children}</div>
      <span
        className={cn(
          'flex-shrink-0 text-xs text-gray-500 min-w-[2.5rem] text-right transition-opacity duration-150',
          hovered ? 'opacity-100' : 'opacity-0'
        )}
      >
        {timeLabel ?? '刚刚'}
      </span>
    </div>
  )
}

export function ChatMessage({
  className,
  item,
  hasFollowingAction,
  isActiveThought,
  onViewAllFiles,
  onFileClick,
  onToolClick,
}: ChatMessageProps) {
  if (item.kind === 'user') {
    return (
      <div
        className={cn(
          'flex w-full flex-col items-end justify-end gap-1 group mt-3',
          className
        )}
      >
        <div className="flex max-w-[90%] relative flex-col gap-2 items-end">
          <div className="text-gray-700 relative flex items-center rounded-lg overflow-hidden bg-white p-3 border">
            {item.data.message ?? ''}
          </div>
        </div>
      </div>
    )
  }

  if (item.kind === 'assistant') {
    const isStreaming = item.data.is_streaming === true
    return (
      <div
        className={cn('flex flex-col gap-2 w-full group mt-3', className)}
      >
        <div className="flex items-center justify-between h-7 group">
          <div className="flex items-center justify-center gap-1 text-gray-700">
            <Languages size={18} />
            I-DO
          </div>
        </div>
        <div className="max-w-none p-0 m-0 text-gray-700">
          <MarkdownContent content={item.data.message ?? ''} />
          {isStreaming && (
            <span className="inline-block w-2 h-4 ml-0.5 align-text-bottom bg-gray-700 animate-pulse rounded-sm" />
          )}
        </div>
      </div>
    )
  }

  if (item.kind === 'thought') {
    return <ThoughtBlock data={item.data} hasFollowingAction={hasFollowingAction} isActive={isActiveThought} className={className} />
  }

  if (item.kind === 'tool') {
    return (
      <ToolRow
        className={className}
        timeLabel={item.timeLabel}
      >
        <ToolUse data={item.data} onClick={onToolClick ? () => onToolClick(item.data) : undefined} />
      </ToolRow>
    )
  }

  if (item.kind === 'step') {
    return (
      <StepBlock stepItem={item} className={className} onToolClick={onToolClick} />
    )
  }

  if (item.kind === 'attachments') {
    return (
      <div className={cn('mt-3', className)}>
        <AttachmentsMessage
          role={item.role}
          files={item.files}
          onViewAllFiles={item.role === 'assistant' ? onViewAllFiles : undefined}
          onFileClick={onFileClick}
        />
      </div>
    )
  }

  if (item.kind === 'error') {
    return (
      <div
        className={cn('flex flex-col gap-2 w-full group mt-3', className)}
      >
        <div className="flex items-center justify-between h-7 group">
          <div className="flex items-center justify-center gap-1 text-red-600">
            <Languages size={18} />
            I-DO
          </div>
        </div>
        <div className="max-w-none p-0 m-0 text-red-600">
          <MarkdownContent content={item.error} />
        </div>
      </div>
    )
  }

  return null
}

function StepBlock({
  stepItem,
  className,
  onToolClick,
}: {
  stepItem: Extract<TimelineItem, { kind: 'step' }>
  className?: string
  onToolClick?: (tool: ToolEvent) => void
}) {
  const [expanded, setExpanded] = useState(true)
  const { data, tools } = stepItem
  const isCompleted = data.status === 'completed'

  return (
    <div className={cn('flex flex-col mt-3', className)}>
      <div
        role="button"
        tabIndex={0}
        onClick={() => setExpanded(!expanded)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            setExpanded((prev) => !prev)
          }
        }}
        className="text-sm w-full cursor-pointer flex gap-2 justify-between group/header truncate text-gray-700 rounded-md hover:bg-gray-50/80 transition-colors outline-none focus-visible:ring-2 focus-visible:ring-gray-300"
      >
        <div className="flex flex-row gap-2 justify-start items-center truncate min-w-0 flex-1">
          <div
            className={cn(
              'w-4 h-4 flex-shrink-0 flex items-center justify-center border rounded-[15px] bg-gray-300'
            )}
          >
            <CheckIcon className="text-white" size={10} />
          </div>
          <div className="truncate font-medium markdown-content min-w-0">
            {data.description}
          </div>
          <ChevronDown
            className={cn('flex-shrink-0 transition-transform text-gray-500', expanded && 'rotate-180')}
          />
        </div>
      </div>
      {expanded && tools.length > 0 && (
        <div className="flex">
          <div className="w-6 relative flex-shrink-0">
            <div className="absolute left-[7px] top-2 bottom-0 w-[1px] border-l border-dashed border-gray-300" />
          </div>
          <div className="flex flex-col gap-3 flex-1 min-w-0 overflow-hidden pt-2 transition-[max-height,opacity] duration-150 ease-in-out">
            {tools.map((tool, idx) => (
              <ToolRow key={`${data.id}-tool-${idx}`} timeLabel={getToolTimeLabel(tool)}>
                <ToolUse data={tool} onClick={onToolClick ? () => onToolClick(tool) : undefined} />
              </ToolRow>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}