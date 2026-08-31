import { describe, expect, it } from 'vitest'
import {
  extractResearchSummary,
  extractInsights,
  formatScore,
  scoreBadgeClass,
  extractStringList,
} from './deep-research-utils'
import type { ToolEvent, ResearchInsight } from '@/lib/api/types'

/** 构造 ToolEvent 的辅助函数，简化测试用例 */
function makeTool(content: unknown, args?: Record<string, unknown>): ToolEvent {
  return {
    name: 'deep_research',
    function: 'deep_research',
    args: args ?? {},
    content,
  }
}

function makeInsight(overrides: Partial<ResearchInsight> = {}): ResearchInsight {
  return {
    content: '关键洞察内容',
    source_url: 'https://example.com/source',
    source_title: '来源标题',
    relevance_score: 0.85,
    ...overrides,
  }
}

describe('extractResearchSummary', () => {
  it('正常数据返回 ResearchSummary', () => {
    const tool = makeTool({
      summary: {
        query: 'LLM 趋势',
        key_findings: [makeInsight()],
        additional_findings: [],
        supplementary: [],
        follow_up_queries: ['后续查询1'],
        total_sources: 5,
      },
    })
    const summary = extractResearchSummary(tool)
    expect(summary).not.toBeNull()
    expect(summary?.query).toBe('LLM 趋势')
    expect(summary?.total_sources).toBe(5)
  })

  it('content 为空返回 null', () => {
    expect(extractResearchSummary(makeTool(null))).toBeNull()
    expect(extractResearchSummary(makeTool(undefined))).toBeNull()
  })

  it('content 为数组返回 null', () => {
    expect(extractResearchSummary(makeTool(['a', 'b']))).toBeNull()
  })

  it('summary 为非对象返回 null', () => {
    expect(extractResearchSummary(makeTool({ summary: 'not object' }))).toBeNull()
    expect(extractResearchSummary(makeTool({ summary: null }))).toBeNull()
  })

  it('query 字段非字符串返回 null', () => {
    const tool = makeTool({
      summary: {
        query: 123,
        key_findings: [],
      },
    })
    expect(extractResearchSummary(tool)).toBeNull()
  })

  it('分档数组缺失时不影响 summary 提取', () => {
    const tool = makeTool({
      summary: {
        query: '测试',
        // 缺失 key_findings / additional_findings / supplementary
      },
    })
    const summary = extractResearchSummary(tool)
    expect(summary).not.toBeNull()
    expect(summary?.query).toBe('测试')
  })
})

describe('extractInsights', () => {
  it('正常数组返回有效洞察', () => {
    const insights = [makeInsight(), makeInsight({ content: '另一条' })]
    const result = extractInsights(insights)
    expect(result).toHaveLength(2)
    expect(result[0].content).toBe('关键洞察内容')
  })

  it('非数组返回空数组', () => {
    expect(extractInsights(null)).toEqual([])
    expect(extractInsights(undefined)).toEqual([])
    expect(extractInsights('not array')).toEqual([])
    expect(extractInsights({})).toEqual([])
  })

  it('过滤掉 content 非字符串的项', () => {
    const insights = [
      makeInsight(),
      { content: 123, source_url: 'x', source_title: 't', relevance_score: 0.5 },
      makeInsight({ content: 'valid' }),
      null,
      undefined,
      { content: '' },  // 空字符串仍算有效（类型上是 string）
    ]
    const result = extractInsights(insights)
    // 仅保留 content 为字符串的项：3 个
    expect(result).toHaveLength(3)
  })

  it('空数组返回空数组', () => {
    expect(extractInsights([])).toEqual([])
  })
})

describe('formatScore', () => {
  it('正常数值格式化为百分比', () => {
    expect(formatScore(0.85)).toBe('85%')
    expect(formatScore(0.5)).toBe('50%')
    expect(formatScore(1)).toBe('100%')
    expect(formatScore(0)).toBe('0%')
  })

  it('数值四舍五入', () => {
    expect(formatScore(0.854)).toBe('85%')
    expect(formatScore(0.856)).toBe('86%')
  })

  it('字符串数字可转换', () => {
    expect(formatScore('0.85')).toBe('85%')
    expect(formatScore('1')).toBe('100%')
  })

  it('非数字返回 "-"', () => {
    expect(formatScore('not a number')).toBe('-')
    expect(formatScore(null)).toBe('-')
    expect(formatScore(undefined)).toBe('-')
    expect(formatScore(NaN)).toBe('-')
    expect(formatScore(Infinity)).toBe('-')
  })
})

describe('scoreBadgeClass', () => {
  it('score >= 0.8 返回核心发现样式', () => {
    expect(scoreBadgeClass(0.8)).toContain('bg-emerald-50')
    expect(scoreBadgeClass(0.85)).toContain('text-emerald-700')
    expect(scoreBadgeClass(1.0)).toContain('border-emerald-200')
  })

  it('0.5 <= score < 0.8 返回补充发现样式', () => {
    expect(scoreBadgeClass(0.5)).toContain('bg-amber-50')
    expect(scoreBadgeClass(0.7)).toContain('text-amber-700')
    expect(scoreBadgeClass(0.79)).toContain('border-amber-200')
  })

  it('score < 0.5 返回参考信息样式', () => {
    expect(scoreBadgeClass(0.49)).toContain('bg-gray-50')
    expect(scoreBadgeClass(0.3)).toContain('text-gray-600')
    expect(scoreBadgeClass(0)).toContain('border-gray-200')
  })

  it('边界值正确分档', () => {
    // 0.8 是核心发现边界
    expect(scoreBadgeClass(0.8)).toContain('emerald')
    // 0.79 是补充发现
    expect(scoreBadgeClass(0.79)).toContain('amber')
    // 0.5 是补充发现边界
    expect(scoreBadgeClass(0.5)).toContain('amber')
    // 0.49 是参考信息
    expect(scoreBadgeClass(0.49)).toContain('gray')
  })
})

describe('extractStringList', () => {
  it('正常字符串数组返回', () => {
    expect(extractStringList(['a', 'b', 'c'])).toEqual(['a', 'b', 'c'])
  })

  it('过滤空字符串', () => {
    expect(extractStringList(['a', '', '  ', 'b'])).toEqual(['a', 'b'])
  })

  it('过滤非字符串项', () => {
    expect(extractStringList(['a', 1, true, null, undefined, { x: 1 }, 'b'])).toEqual(['a', 'b'])
  })

  it('非数组返回空数组', () => {
    expect(extractStringList(null)).toEqual([])
    expect(extractStringList(undefined)).toEqual([])
    expect(extractStringList('not array')).toEqual([])
    expect(extractStringList({})).toEqual([])
  })

  it('空数组返回空数组', () => {
    expect(extractStringList([])).toEqual([])
  })
})
