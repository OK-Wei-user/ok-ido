/**
 * 深度研究展示工具函数 - 纯函数，便于单元测试与复用
 *
 * 与后端 ResearchSummary/ResearchInsight 模型对齐，提供：
 * - 安全提取研究总结与洞察数组（异常数据兜底）
 * - 相关性评分格式化与分档颜色样式
 */
import type { ToolEvent, ResearchSummary, ResearchInsight } from '@/lib/api/types'
import { getToolContent } from './utils'

/** 从工具事件中安全提取 ResearchSummary，异常数据返回 null */
export function extractResearchSummary(tool: ToolEvent): ResearchSummary | null {
  const content = getToolContent(tool)
  const summary = content?.summary
  if (!summary || typeof summary !== 'object') return null
  const s = summary as ResearchSummary
  if (typeof s.query !== 'string') return null
  return s
}

/** 安全提取洞察数组，非数组或异常数据返回空数组 */
export function extractInsights(value: unknown): ResearchInsight[] {
  if (!Array.isArray(value)) return []
  return value.filter(
    (item): item is ResearchInsight =>
      item != null && typeof item === 'object' && typeof (item as ResearchInsight).content === 'string'
  )
}

/** 将相关性评分格式化为百分比展示，异常评分显示为 "-" */
export function formatScore(score: unknown): string {
  if (score == null) return '-'
  const n = typeof score === 'number' ? score : Number(score)
  if (!Number.isFinite(n)) return '-'
  return `${Math.round(n * 100)}%`
}

/** 评分对应的颜色样式（核心/补充/参考三档） */
export function scoreBadgeClass(score: number): string {
  if (score >= 0.8) return 'bg-emerald-50 text-emerald-700 border-emerald-200'
  if (score >= 0.5) return 'bg-amber-50 text-amber-700 border-amber-200'
  return 'bg-gray-50 text-gray-600 border-gray-200'
}

/** 安全提取非空字符串数组，用于 follow_up_queries 等列表字段 */
export function extractStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is string => typeof item === 'string' && item.trim() !== '')
}
