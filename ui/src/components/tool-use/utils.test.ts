import { describe, expect, it } from 'vitest'
import { getToolKind, getFriendlyToolLabel, getArg, truncate, getToolContent, stripMcpPrefix } from './utils'
import type { ToolEvent } from '@/lib/api/types'

describe('getToolKind', () => {
  it('识别 deep_research 工具（name 匹配）', () => {
    const tool: ToolEvent = {
      name: 'deep_research',
      function: 'deep_research',
      args: { query: 'LLM 趋势' },
    }
    expect(getToolKind(tool)).toBe('deep_research')
  })

  it('识别 deep_research 工具（仅 function 匹配）', () => {
    const tool: ToolEvent = {
      name: '',
      function: 'deep_research',
      args: {},
    }
    expect(getToolKind(tool)).toBe('deep_research')
  })

  it('deep_research 优先于 search 识别', () => {
    // 假设有歧义的命名，确保 deep_research 不被误判为 search
    const tool: ToolEvent = {
      name: 'deep_research',
      function: 'search_web',
      args: {},
    }
    expect(getToolKind(tool)).toBe('deep_research')
  })

  it('未匹配的归入 default', () => {
    const tool: ToolEvent = {
      name: 'unknown_tool',
      function: 'unknown_fn',
      args: {},
    }
    expect(getToolKind(tool)).toBe('default')
  })

  it('null 数据归入 default', () => {
    expect(getToolKind(null)).toBe('default')
    expect(getToolKind(undefined)).toBe('default')
  })

  it('保留原有 search 识别', () => {
    expect(getToolKind({ name: 'search', function: 'search_web', args: {} } as ToolEvent)).toBe('search')
    expect(getToolKind({ name: 'other', function: 'search_web', args: {} } as ToolEvent)).toBe('search')
  })

  it('保留原有 browser 识别', () => {
    expect(getToolKind({ name: 'browser', function: 'browser_view', args: {} } as ToolEvent)).toBe('browser')
    expect(getToolKind({ name: 'other', function: 'browser_click', args: {} } as ToolEvent)).toBe('browser')
  })
})

describe('getFriendlyToolLabel', () => {
  it('deep_research 带 query 显示研究主题', () => {
    const tool: ToolEvent = {
      name: 'deep_research',
      function: 'deep_research',
      args: { query: '2025 年大语言模型发展趋势' },
    }
    const label = getFriendlyToolLabel(tool)
    expect(label).toContain('正在深度研究')
    expect(label).toContain('2025 年大语言模型发展趋势')
  })

  it('deep_research 无 query 显示通用提示', () => {
    const tool: ToolEvent = {
      name: 'deep_research',
      function: 'deep_research',
      args: {},
    }
    expect(getFriendlyToolLabel(tool)).toBe('正在深度研究')
  })

  it('deep_research 长 query 被截断', () => {
    const longQuery = '这是一个非常长的研究主题'.repeat(20)
    const tool: ToolEvent = {
      name: 'deep_research',
      function: 'deep_research',
      args: { query: longQuery },
    }
    const label = getFriendlyToolLabel(tool)
    expect(label).toContain('正在深度研究')
    // 截断阈值为 60 字符，截断后应小于等于 query 原长
    expect(label.length).toBeLessThan(longQuery.length)
  })

  it('保留原有 search label', () => {
    const tool: ToolEvent = {
      name: 'search',
      function: 'search_web',
      args: { query: '北京天气' },
    }
    expect(getFriendlyToolLabel(tool)).toContain('正在搜索')
    expect(getFriendlyToolLabel(tool)).toContain('北京天气')
  })
})

describe('getArg', () => {
  it('按顺序返回首个存在的字符串值', () => {
    expect(getArg({ a: 1, b: 'hello' }, 'a', 'b')).toBe('hello')
    expect(getArg({ q: 'search' }, 'query', 'q')).toBe('search')
  })

  it('忽略非字符串值', () => {
    expect(getArg({ a: 1, b: true }, 'a', 'b')).toBe('')
  })

  it('非对象参数返回空字符串', () => {
    expect(getArg(null, 'a')).toBe('')
    expect(getArg(undefined, 'a')).toBe('')
  })

  it('未找到键返回空字符串', () => {
    expect(getArg({ a: 'x' }, 'b', 'c')).toBe('')
  })
})

describe('truncate', () => {
  it('短字符串不截断', () => {
    expect(truncate('hello', 10)).toBe('hello')
  })

  it('长字符串截断并加省略号', () => {
    const result = truncate('123456789012345', 5)
    expect(result).toBe('12345…')
  })

  it('等于阈值不截断', () => {
    expect(truncate('12345', 5)).toBe('12345')
  })
})

describe('getToolContent', () => {
  it('对象类型 content 正常返回', () => {
    const tool = { content: { summary: { query: 'test' } } } as unknown as ToolEvent
    const result = getToolContent(tool)
    expect(result).not.toBeNull()
    expect(result?.summary).toEqual({ query: 'test' })
  })

  it('null content 返回 null', () => {
    const tool = { content: null } as unknown as ToolEvent
    expect(getToolContent(tool)).toBeNull()
  })

  it('undefined content 返回 null', () => {
    const tool = { content: undefined } as unknown as ToolEvent
    expect(getToolContent(tool)).toBeNull()
  })

  it('数组 content 返回 null', () => {
    const tool = { content: ['a', 'b'] } as unknown as ToolEvent
    expect(getToolContent(tool)).toBeNull()
  })

  it('字符串 content 返回 null', () => {
    const tool = { content: 'string content' } as unknown as ToolEvent
    expect(getToolContent(tool)).toBeNull()
  })
})

describe('stripMcpPrefix', () => {
  it('移除 mcp_ 前缀', () => {
    expect(stripMcpPrefix('mcp_system_getWarehousingDetailExport')).toBe('system_getWarehousingDetailExport')
  })

  it('无 mcp_ 前缀时原样返回', () => {
    expect(stripMcpPrefix('getWarehousingDetailExport')).toBe('getWarehousingDetailExport')
  })

  it('空字符串返回空', () => {
    expect(stripMcpPrefix('')).toBe('')
  })

  it('仅 mcp_ 前缀返回空', () => {
    expect(stripMcpPrefix('mcp_')).toBe('')
  })

  it('多个 mcp_ 前缀仅移除第一个', () => {
    expect(stripMcpPrefix('mcp_mcp_multimodal_vl_image_understand')).toBe('mcp_multimodal_vl_image_understand')
  })
})

describe('getFriendlyToolLabel - MCP 工具', () => {
  it('mcp_tool_search 带 query 显示搜索关键词', () => {
    const tool: ToolEvent = {
      name: 'mcp',
      function: 'mcp_tool_search',
      args: { query: '入库明细导出' },
    }
    const label = getFriendlyToolLabel(tool)
    expect(label).toContain('正在搜索 MCP 工具')
    expect(label).toContain('入库明细导出')
  })

  it('mcp_tool_search 无 query 显示通用提示', () => {
    const tool: ToolEvent = {
      name: 'mcp',
      function: 'mcp_tool_search',
      args: {},
    }
    expect(getFriendlyToolLabel(tool)).toBe('正在搜索 MCP 工具')
  })

  it('mcp_tool_describe 带 name 显示工具名(去 mcp_ 前缀)', () => {
    const tool: ToolEvent = {
      name: 'mcp',
      function: 'mcp_tool_describe',
      args: { name: 'mcp_system_getWarehousingDetailExport' },
    }
    const label = getFriendlyToolLabel(tool)
    expect(label).toContain('正在查看 MCP 工具详情')
    expect(label).toContain('system_getWarehousingDetailExport')
    expect(label).not.toContain('mcp_system_getWarehousingDetailExport')
  })

  it('mcp_tool_describe 无 name 显示通用提示', () => {
    const tool: ToolEvent = {
      name: 'mcp',
      function: 'mcp_tool_describe',
      args: {},
    }
    expect(getFriendlyToolLabel(tool)).toBe('正在查看 MCP 工具详情')
  })

  it('mcp_tool_call 带 name(含 mcp_ 前缀) 显示具体操作', () => {
    const tool: ToolEvent = {
      name: 'mcp',
      function: 'mcp_tool_call',
      args: { name: 'mcp_system_getWarehousingDetailExport', arguments: {} },
    }
    const label = getFriendlyToolLabel(tool)
    expect(label).toContain('正在通过 MCP 服务执行')
    expect(label).toContain('system_getWarehousingDetailExport')
    expect(label).toContain('操作')
    expect(label).not.toContain('mcp_system')
  })

  it('mcp_tool_call 带 name(无 mcp_ 前缀) 显示具体操作', () => {
    const tool: ToolEvent = {
      name: 'mcp',
      function: 'mcp_tool_call',
      args: { name: 'getWarehousingDetailExport', arguments: {} },
    }
    const label = getFriendlyToolLabel(tool)
    expect(label).toContain('正在通过 MCP 服务执行')
    expect(label).toContain('getWarehousingDetailExport')
    expect(label).toContain('操作')
  })

  it('mcp_tool_call 无 name 显示通用提示', () => {
    const tool: ToolEvent = {
      name: 'mcp',
      function: 'mcp_tool_call',
      args: { arguments: {} },
    }
    expect(getFriendlyToolLabel(tool)).toBe('正在通过 MCP 服务执行操作')
  })

  it('直接 MCP 工具(非懒加载)显示具体操作', () => {
    const tool: ToolEvent = {
      name: 'mcp',
      function: 'mcp_system_getWarehousingDetailExport',
      args: { startDate: '2026-01-01', endDate: '2026-05-31' },
    }
    const label = getFriendlyToolLabel(tool)
    expect(label).toContain('正在通过 MCP 服务执行')
    expect(label).toContain('system_getWarehousingDetailExport')
    expect(label).toContain('操作')
    expect(label).not.toContain('mcp_system')
  })

  it('直接 MCP 工具长名称被截断', () => {
    const longName = 'mcp_system_getWarehousingDetailExportWithVeryLongNameThatExceedsLimit'
    const tool: ToolEvent = {
      name: 'mcp',
      function: longName,
      args: {},
    }
    const label = getFriendlyToolLabel(tool)
    expect(label).toContain('正在通过 MCP 服务执行')
    expect(label).toContain('…')
    expect(label.length).toBeLessThan(longName.length + 20)
  })

  it('搜索类 MCP 工具保留原有搜索行为', () => {
    const tool: ToolEvent = {
      name: 'mcp',
      function: 'mcp_search_web',
      args: { query: '北京天气' },
    }
    const label = getFriendlyToolLabel(tool)
    expect(label).toContain('正在搜索')
    expect(label).toContain('北京天气')
    expect(label).not.toContain('MCP 服务执行')
  })

  it('MCP 工具 name 以 mcp_ 开头也能匹配', () => {
    const tool: ToolEvent = {
      name: 'mcp_multimodal',
      function: 'mcp_tool_call',
      args: { name: 'mcp_multimodal_vl_image_understand', arguments: {} },
    }
    const label = getFriendlyToolLabel(tool)
    expect(label).toContain('正在通过 MCP 服务执行')
    expect(label).toContain('multimodal_vl_image_understand')
    expect(label).toContain('操作')
  })
})
