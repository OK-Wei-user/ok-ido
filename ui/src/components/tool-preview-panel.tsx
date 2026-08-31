'use client'

import { useMemo, type ReactNode } from 'react'
import type { ToolEvent, ResearchInsight } from '@/lib/api/types'
import { getToolKind, getFriendlyToolLabel, getArg, getToolContent } from '@/components/tool-use/utils'
import type { ToolKind } from '@/components/tool-use/utils'
import {
  extractResearchSummary,
  extractInsights,
  formatScore,
  scoreBadgeClass,
  extractStringList,
} from '@/components/tool-use/deep-research-utils'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Button } from '@/components/ui/button'
import {
  Maximize2,
  Monitor,
  Play,
  Terminal,
  Globe,
  Search,
  FileSearch,
  Wrench,
  Bot,
  Sparkles,
  Microscope,
  ExternalLink,
  Expand,
} from 'lucide-react'
import { EmbeddedVNC } from '@/components/embedded-vnc'

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

export interface ToolPreviewPanelProps {
  tool: ToolEvent
  onClose: () => void
  onJumpToLatest?: () => void
  onOpenVNC?: () => void
  /** 会话 ID,用于构造 VNC WS URL (实时模式时内嵌 VNC) */
  sessionId?: string
  /** 实时模式标志: true=会话运行中(显示VNC), false=回放模式(显示截图) */
  isLive?: boolean
  /** 预览来源: 'auto'=自动追踪(会话运行中跟随最新工具), 'user'=用户手动点击历史工具
   * VNC粘性模式: auto时VNC持续显示(不随工具状态切换断开), user时显示截图 */
  previewSource?: 'auto' | 'user'
}

type ConsoleRecord = { ps1: string; command: string; output: string }

type SearchResultItem = { url: string; title: string; snippet: string }

/* ------------------------------------------------------------------ */
/*  Content extractors                                                 */
/* ------------------------------------------------------------------ */

function getToolDescription(kind: ToolKind): string {
  const map: Record<ToolKind, string> = {
    bash: '终端',
    browser: '浏览器',
    search: '搜索',
    file: '文件',
    mcp: 'MCP 服务',
    a2a: 'A2A 智能体',
    message: '消息',
    deep_research: '深度研究',
    default: '工具',
  }
  return map[kind]
}

/* ------------------------------------------------------------------ */
/*  Jump-to-latest overlay button                                      */
/* ------------------------------------------------------------------ */

function JumpToLatestButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-white/90 backdrop-blur text-sm text-gray-700 hover:bg-white shadow-md border border-gray-200 transition-colors cursor-pointer"
    >
      <Play size={12} className="fill-current" />
      <span>跳转实时</span>
    </button>
  )
}

/* ------------------------------------------------------------------ */
/*  Sub-previews                                                       */
/* ------------------------------------------------------------------ */

function ShellPreview({ tool }: { tool: ToolEvent }) {
  const content = getToolContent(tool)
  const consoleData = content?.console
  const sessionId = getArg(tool.args, 'session_id')

  const records: ConsoleRecord[] = useMemo(() => {
    if (Array.isArray(consoleData)) return consoleData as ConsoleRecord[]
    return []
  }, [consoleData])

  return (
    <div className="flex flex-col gap-3 p-4 h-full">
      <div className="flex-1 rounded-lg overflow-hidden border border-gray-700 bg-[#1e1e1e] flex flex-col min-h-0">
        <div className="text-center text-xs text-gray-400 py-1.5 bg-[#2d2d2d] border-b border-gray-700 flex-shrink-0">
          {sessionId || 'shell'}
        </div>
        <ScrollArea className="flex-1">
          <div className="p-4 font-mono text-sm leading-relaxed">
            {records.length > 0 ? records.map((rec, i) => {
              // 改进B: 最后一条记录且处于流式输出中(is_streaming)时,追加闪烁光标强化「输出进行中」感知
              // CALLED(is_streaming=false/undefined)时光标消失,输出呈现为最终静态结果
              const isLast = i === records.length - 1
              const showCursor = isLast && tool.is_streaming === true
              return (
                <div key={i} className="mb-2">
                  <div>
                    <span className="text-green-400">{rec.ps1}</span>
                    {' '}
                    <span className="text-white">{rec.command}</span>
                  </div>
                  {(rec.output || showCursor) && (
                    <pre className="text-gray-300 whitespace-pre-wrap break-words mt-0.5">
                      {rec.output}
                      {showCursor && <span className="animate-pulse">▋</span>}
                    </pre>
                  )}
                </div>
              )
            }) : (
              <span className="text-gray-500">等待命令输出...</span>
            )}
          </div>
        </ScrollArea>
      </div>
    </div>
  )
}

function BrowserPreview({
  tool,
  onOpenVNC,
  sessionId,
  isLive,
  previewSource = 'auto',
}: {
  tool: ToolEvent
  onOpenVNC?: () => void
  sessionId?: string
  isLive?: boolean
  previewSource?: 'auto' | 'user'
}) {
  const content = getToolContent(tool)
  const screenshot = typeof content?.screenshot === 'string' ? content.screenshot : null
  // 操作结果摘要: 后端在 screenshot=null 时填充(CALLED=操作结果, CALLING=执行中提示)
  const message = typeof content?.message === 'string' ? content.message : null
  const url = getArg(tool.args, 'url', 'href', 'link')
  const isCalling = tool.status === 'calling'

  // VNC粘性模式: 自动追踪(previewSource='auto')时会话运行中持续显示VNC,
  // 不随工具状态切换(CALLING→CALLED)断开,消除闪烁;
  // 用户手动点击历史工具(previewSource='user')时仅工具执行中显示VNC,
  // 工具完成后显示截图,便于回看历史操作结果
  const showVNC = isLive && (previewSource === 'auto' || isCalling) && !!sessionId

  return (
    <div className="flex flex-col gap-3 p-4 h-full">
      {url && (
        <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-gray-100 border text-sm text-gray-600 flex-shrink-0">
          <Globe size={14} className="text-gray-400 flex-shrink-0" />
          <span className="truncate">{url}</span>
        </div>
      )}
      <div className="flex-1 rounded-lg overflow-hidden border min-h-0 relative bg-black">
        {showVNC ? (
          // 实时模式: 内嵌 VNC 远程桌面 (viewOnly, 仅监控)
          <EmbeddedVNC sessionId={sessionId!} />
        ) : screenshot ? (
          // 回放模式: 显示截图
          <ScrollArea className="h-full">
            <img
              src={screenshot}
              alt="浏览器截图"
              className="w-full h-auto"
            />
          </ScrollArea>
        ) : (
          // 兜底: 无截图且非实时 (工具调用中但VNC未就绪,或等待截图)
          <div className="flex flex-col items-center justify-center gap-2 h-full text-sm text-gray-500 bg-white">
            {isCalling && <Monitor size={20} className="text-gray-400 animate-pulse" />}
            <span>{message || (isCalling ? '正在执行浏览器操作...' : '等待页面截图...')}</span>
          </div>
        )}
        {/* 全屏展开按钮: 仅 VNC 模式下显示 */}
        {showVNC && onOpenVNC && (
          <button
            type="button"
            onClick={onOpenVNC}
            className="absolute bottom-3 right-3 w-9 h-9 rounded-full bg-gray-800/80 text-white flex items-center justify-center shadow-lg hover:bg-gray-700 transition-colors cursor-pointer z-10"
            aria-label="全屏查看远程桌面"
          >
            <Expand size={16} />
          </button>
        )}
      </div>
    </div>
  )
}

function SearchPreview({ tool }: { tool: ToolEvent }) {
  const content = getToolContent(tool)
  const rawResults = content?.results

  const results: SearchResultItem[] = useMemo(() => {
    if (Array.isArray(rawResults)) return rawResults as SearchResultItem[]
    return []
  }, [rawResults])

  const query = getArg(tool.args, 'query', 'q')

  return (
    <ScrollArea className="h-full">
      <div className="flex flex-col gap-1 p-4">
        {query && (
          <div className="text-sm text-gray-500 mb-3">
            搜索&ldquo;{query}&rdquo;的结果 · 共 {results.length} 条
          </div>
        )}
        {results.length > 0 ? results.map((item, i) => (
          <a
            key={i}
            href={item.url}
            target="_blank"
            rel="noopener noreferrer"
            className="block p-3 rounded-lg hover:bg-gray-50 transition-colors group"
          >
            <div className="text-xs text-green-700 truncate mb-0.5">{item.url}</div>
            <div className="text-sm font-medium text-blue-700 group-hover:underline mb-1 line-clamp-1">
              {item.title}
            </div>
            {item.snippet && (
              <div className="text-xs text-gray-600 line-clamp-2">{item.snippet}</div>
            )}
          </a>
        )) : (
          <div className="text-sm text-gray-500 text-center py-8">暂无搜索结果</div>
        )}
      </div>
    </ScrollArea>
  )
}

function FileToolPreview({ tool }: { tool: ToolEvent }) {
  const content = getToolContent(tool)
  const fileContent = typeof content?.content === 'string' ? content.content : null
  const filepath = getArg(tool.args, 'filepath', 'path', 'pathname')

  return (
    <div className="flex flex-col gap-3 p-4 h-full">
      <div className="flex-1 rounded-lg overflow-hidden border border-gray-700 bg-[#1e1e1e] flex flex-col min-h-0">
        {filepath && (
          <div className="text-center text-xs text-gray-400 py-1.5 bg-[#2d2d2d] border-b border-gray-700 flex-shrink-0 truncate px-4">
            {filepath}
          </div>
        )}
        <ScrollArea className="flex-1">
          <pre className="p-4 font-mono text-sm text-gray-300 whitespace-pre-wrap break-words leading-relaxed">
            {fileContent ?? '等待文件内容...'}
          </pre>
        </ScrollArea>
      </div>
    </div>
  )
}

function MCPPreview({ tool }: { tool: ToolEvent }) {
  const content = getToolContent(tool)
  const result = content?.result
  // MCP多模态: 后端剥离images base64后填充的图片URL列表
  const rawImages = content?.images
  const images: string[] = Array.isArray(rawImages)
    ? rawImages.filter((u): u is string => typeof u === 'string' && u.length > 0)
    : []

  return (
    <ScrollArea className="h-full">
      <div className="flex flex-col gap-4 p-4">
        <div className="flex flex-col gap-1">
          <div className="text-xs text-gray-500 uppercase tracking-wide">工具信息</div>
          <div className="rounded-lg border bg-gray-50 p-3 text-sm">
            <div><span className="text-gray-500">名称：</span><span className="text-gray-800">{tool.name}</span></div>
            <div><span className="text-gray-500">函数：</span><span className="text-gray-800">{tool.function}</span></div>
            {Object.keys(tool.args).length > 0 && (
              <div className="mt-1">
                <span className="text-gray-500">参数：</span>
                <pre className="text-xs text-gray-700 mt-1 whitespace-pre-wrap break-words">
                  {JSON.stringify(tool.args, null, 2)}
                </pre>
              </div>
            )}
          </div>
        </div>
        <div className="flex flex-col gap-1">
          <div className="text-xs text-gray-500 uppercase tracking-wide">执行结果</div>
          <div className="rounded-lg border border-gray-700 bg-[#1e1e1e] p-4">
            <pre className="font-mono text-sm text-gray-300 whitespace-pre-wrap break-words">
              {result != null
                ? (typeof result === 'string' ? result : JSON.stringify(result, null, 2))
                : '等待执行结果...'}
            </pre>
          </div>
        </div>
        {images.length > 0 && (
          <div className="flex flex-col gap-2">
            <div className="text-xs text-gray-500 uppercase tracking-wide">返回图片({images.length})</div>
            <div className="grid grid-cols-1 gap-2">
              {images.map((src, i) => (
                <a key={i} href={src} target="_blank" rel="noopener noreferrer" className="block rounded-lg overflow-hidden border hover:opacity-90 transition-opacity">
                  <img src={src} alt={`MCP返回图片${i + 1}`} className="w-full h-auto" />
                </a>
              ))}
            </div>
          </div>
        )}
      </div>
    </ScrollArea>
  )
}

function A2APreview({ tool }: { tool: ToolEvent }) {
  const content = getToolContent(tool)
  const result = content?.a2a_result

  const query = getArg(tool.args, 'query', 'message', 'input')

  return (
    <ScrollArea className="h-full">
      <div className="flex flex-col gap-4 p-4">
        <div className="flex flex-col gap-1">
          <div className="text-xs text-gray-500 uppercase tracking-wide">Agent 调用信息</div>
          <div className="rounded-lg border bg-gray-50 p-3 text-sm">
            <div><span className="text-gray-500">工具：</span><span className="text-gray-800">{tool.name}</span></div>
            <div><span className="text-gray-500">函数：</span><span className="text-gray-800">{tool.function}</span></div>
            {query && <div><span className="text-gray-500">指令：</span><span className="text-gray-800">{query}</span></div>}
          </div>
        </div>
        <div className="flex flex-col gap-1">
          <div className="text-xs text-gray-500 uppercase tracking-wide">执行结果</div>
          <div className="rounded-lg border border-gray-700 bg-[#1e1e1e] p-4">
            <pre className="font-mono text-sm text-gray-300 whitespace-pre-wrap break-words">
              {result != null
                ? (typeof result === 'string' ? result : JSON.stringify(result, null, 2))
                : '等待执行结果...'}
            </pre>
          </div>
        </div>
      </div>
    </ScrollArea>
  )
}

function DefaultPreview({ tool }: { tool: ToolEvent }) {
  return (
    <ScrollArea className="h-full">
      <div className="flex flex-col gap-4 p-4">
        <div className="rounded-lg border bg-gray-50 p-3 text-sm">
          <div><span className="text-gray-500">名称：</span><span className="text-gray-800">{tool.name}</span></div>
          <div><span className="text-gray-500">函数：</span><span className="text-gray-800">{tool.function}</span></div>
        </div>
        {tool.content != null && (
          <div className="rounded-lg border border-gray-700 bg-[#1e1e1e] p-4">
            <pre className="font-mono text-sm text-gray-300 whitespace-pre-wrap break-words">
              {typeof tool.content === 'string' ? tool.content : JSON.stringify(tool.content, null, 2)}
            </pre>
          </div>
        )}
      </div>
    </ScrollArea>
  )
}

/* ------------------------------------------------------------------ */
/*  Deep Research Preview                                               */
/* ------------------------------------------------------------------ */

/** 单条洞察展示 */
function InsightItem({ insight }: { insight: ResearchInsight }) {
  const score = typeof insight.relevance_score === 'number' ? insight.relevance_score : 0
  const title = insight.source_title || insight.source_url || '未知来源'
  const url = insight.source_url
  return (
    <div className="rounded-lg border border-gray-200 bg-white p-3 hover:border-gray-300 transition-colors">
      <div className="text-sm text-gray-800 leading-relaxed whitespace-pre-wrap break-words">
        {insight.content}
      </div>
      <div className="flex items-center justify-between gap-2 mt-2">
        <div className="flex items-center gap-1.5 min-w-0 flex-1">
          {url ? (
            <a
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-xs text-blue-600 hover:text-blue-700 hover:underline truncate"
            >
              <ExternalLink size={11} className="flex-shrink-0" />
              <span className="truncate">{title}</span>
            </a>
          ) : (
            <span className="text-xs text-gray-500 truncate">{title}</span>
          )}
        </div>
        <span
          className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs border flex-shrink-0 ${scoreBadgeClass(score)}`}
        >
          {formatScore(insight.relevance_score)}
        </span>
      </div>
    </div>
  )
}

/** 单个分档区域：标题 + 洞察列表 */
function FindingsSection({
  title,
  icon,
  insights,
  accentClass,
}: {
  title: string
  icon: ReactNode
  insights: ResearchInsight[]
  accentClass: string
}) {
  if (insights.length === 0) return null
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <span className={`inline-flex items-center justify-center w-6 h-6 rounded-md ${accentClass}`}>
          {icon}
        </span>
        <span className="text-sm font-medium text-gray-700">{title}</span>
        <span className="text-xs text-gray-400">{insights.length} 条</span>
      </div>
      <div className="flex flex-col gap-2">
        {insights.map((insight, i) => (
          <InsightItem key={i} insight={insight} />
        ))}
      </div>
    </div>
  )
}

function DeepResearchPreview({ tool }: { tool: ToolEvent }) {
  const summary = useMemo(() => extractResearchSummary(tool), [tool])

  // 异常数据兜底：从工具参数中提取 query 作为最小信息
  const queryFromArgs = getArg(tool.args, 'query', 'q')
  const displayQuery = summary?.query ?? queryFromArgs ?? '深度研究'

  if (!summary) {
    return (
      <div className="flex items-center justify-center h-full p-4">
        <div className="text-sm text-gray-500">等待深度研究结果...</div>
      </div>
    )
  }

  const keyFindings = extractInsights(summary.key_findings)
  const additionalFindings = extractInsights(summary.additional_findings)
  const supplementary = extractInsights(summary.supplementary)
  const followUps = extractStringList(summary.follow_up_queries)
  const totalInsights = keyFindings.length + additionalFindings.length + supplementary.length
  const totalSources = typeof summary.total_sources === 'number' ? summary.total_sources : 0

  return (
    <ScrollArea className="h-full">
      <div className="flex flex-col gap-4 p-4">
        {/* 顶部信息卡：研究主题 + 统计数据 */}
        <div className="rounded-lg border bg-gray-50 p-3">
          <div className="text-xs text-gray-500 uppercase tracking-wide mb-1">研究主题</div>
          <div className="text-sm font-medium text-gray-800 break-words">{displayQuery}</div>
          <div className="flex items-center gap-4 mt-2 text-xs text-gray-500">
            <span>来源：{totalSources} 个</span>
            <span>洞察：{totalInsights} 条</span>
          </div>
        </div>

        {/* 分档洞察展示 */}
        {totalInsights === 0 ? (
          <div className="text-sm text-gray-500 text-center py-8">未获取到有效研究洞察</div>
        ) : (
          <>
            <FindingsSection
              title="核心发现"
              icon={<Sparkles size={12} className="text-emerald-700" />}
              insights={keyFindings}
              accentClass="bg-emerald-100"
            />
            <FindingsSection
              title="补充发现"
              icon={<Sparkles size={12} className="text-amber-700" />}
              insights={additionalFindings}
              accentClass="bg-amber-100"
            />
            <FindingsSection
              title="参考信息"
              icon={<Sparkles size={12} className="text-gray-600" />}
              insights={supplementary}
              accentClass="bg-gray-100"
            />
          </>
        )}

        {/* 未探索的后续查询 */}
        {followUps.length > 0 && (
          <div className="flex flex-col gap-2">
            <div className="text-xs text-gray-500 uppercase tracking-wide">未探索的后续查询</div>
            <div className="flex flex-wrap gap-1.5">
              {followUps.map((q, i) => (
                <span
                  key={i}
                  className="inline-flex items-center px-2 py-1 rounded-md bg-gray-100 text-xs text-gray-600 border border-gray-200"
                >
                  {q}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>
    </ScrollArea>
  )
}

/* ------------------------------------------------------------------ */
/*  Main Component                                                     */
/* ------------------------------------------------------------------ */

/** 工具图标渲染器 - 用 switch 直接渲染，避免 render 中创建组件触发 ESLint 规则 */
function ToolIconRenderer({
  kind,
  size,
  className,
}: {
  kind: ToolKind
  size: number
  className?: string
}) {
  switch (kind) {
    case 'bash':
      return <Terminal size={size} className={className} />
    case 'browser':
      return <Globe size={size} className={className} />
    case 'search':
      return <Search size={size} className={className} />
    case 'deep_research':
      return <Microscope size={size} className={className} />
    case 'file':
      return <FileSearch size={size} className={className} />
    case 'mcp':
      return <Wrench size={size} className={className} />
    case 'a2a':
      return <Bot size={size} className={className} />
    case 'message':
      return <Monitor size={size} className={className} />
    default:
      return <Monitor size={size} className={className} />
  }
}

export function ToolPreviewPanel({
  tool,
  onClose,
  onJumpToLatest,
  onOpenVNC,
  sessionId,
  isLive,
  previewSource = 'auto',
}: ToolPreviewPanelProps) {
  const kind = getToolKind(tool)
  const label = getFriendlyToolLabel(tool)
  const toolDesc = getToolDescription(kind)

  return (
    <div className="flex flex-col h-full rounded-xl bg-white shadow-xl overflow-hidden">
      {/* Header */}
      <div className="flex flex-col gap-2 px-4 py-3 border-b border-gray-200 bg-gray-50 flex-shrink-0">
        <div className="flex items-center justify-between">
          <h2 className="text-base font-semibold text-gray-900">I-DO 的电脑</h2>
          <div className="flex items-center gap-1">
            {/* VNC全屏按钮: 会话运行时始终可用(不限于浏览器工具),便于用户随时查看远程桌面 */}
            {onOpenVNC && (
              <Button
                variant="ghost"
                size="icon-sm"
                onClick={onOpenVNC}
                aria-label="打开远程桌面"
                title="打开远程桌面"
                className="cursor-pointer"
              >
                <Expand size={16} />
              </Button>
            )}
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={onClose}
              aria-label="关闭预览"
              className="cursor-pointer"
            >
              <Maximize2 size={16} />
            </Button>
          </div>
        </div>
        <div className="flex items-center gap-2 text-sm text-gray-600">
          <Monitor size={14} className="text-gray-500 flex-shrink-0" />
          <span>I-DO 正在使用</span>
          <span className="font-medium text-gray-800">{toolDesc}</span>
        </div>
        <div className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 border border-gray-200 bg-gray-100 text-gray-700 text-xs w-fit max-w-full">
          <ToolIconRenderer kind={kind} size={14} className="flex-shrink-0 text-gray-500" />
          <span className="truncate">{label}</span>
        </div>
      </div>

      {/* Content with overlaid jump button */}
      <div className="flex-1 overflow-hidden relative">
        {kind === 'bash' && <ShellPreview tool={tool} />}
        {kind === 'browser' && <BrowserPreview tool={tool} onOpenVNC={onOpenVNC} sessionId={sessionId} isLive={isLive} previewSource={previewSource} />}
        {kind === 'search' && <SearchPreview tool={tool} />}
        {kind === 'deep_research' && <DeepResearchPreview tool={tool} />}
        {kind === 'file' && <FileToolPreview tool={tool} />}
        {kind === 'mcp' && <MCPPreview tool={tool} />}
        {kind === 'a2a' && <A2APreview tool={tool} />}
        {(kind === 'default' || kind === 'message') && <DefaultPreview tool={tool} />}

        {/* "跳转实时" overlaid at bottom-center */}
        {onJumpToLatest && (
          <div className="absolute bottom-6 left-1/2 -translate-x-1/2 z-10">
            <JumpToLatestButton onClick={onJumpToLatest} />
          </div>
        )}
      </div>
    </div>
  )
}
