import type { ToolEvent } from '@/lib/api/types'

export type ToolKind =
  | 'message'
  | 'bash'
  | 'file'
  | 'search'
  | 'deep_research'
  | 'browser'
  | 'mcp'
  | 'a2a'
  | 'default'

export function getArg(args: Record<string, unknown>, ...keys: string[]): string {
  if (!args || typeof args !== 'object') return ''
  for (const k of keys) {
    const v = args[k]
    if (typeof v === 'string') return v
  }
  return ''
}

export function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max) + '…' : s
}

/** 从工具事件中提取内容对象，非对象或数组返回 null */
export function getToolContent(tool: ToolEvent): Record<string, unknown> | null {
  const c = tool.content
  if (c && typeof c === 'object' && !Array.isArray(c)) return c as Record<string, unknown>
  return null
}

export function getToolKind(data: ToolEvent | null | undefined): ToolKind {
  if (!data) return 'default'
  const name = (data.name ?? '').toLowerCase()
  const fn = (data.function ?? '').toLowerCase()

  if (data.function === 'message_notify_user' || data.function === 'message_ask_user') {
    return 'message'
  }
  if (name === 'shell' || name.includes('bash') || fn === 'shell_execute' || fn === 'run' || fn === 'execute' || fn === 'run_command') {
    return 'bash'
  }
  if (name === 'file' || name.includes('file')) {
    return 'file'
  }
  if (name === 'mcp' || name.startsWith('mcp_')) {
    return 'mcp'
  }
  // 深度研究工具：name 或 function 含 deep_research 时归入此类
  // 注意需在 search 之前判定，避免与可能的 search_web 子串冲突
  if (name === 'deep_research' || fn === 'deep_research') {
    return 'deep_research'
  }
  // 剔除原先fn中关于search_web的包含匹配, 避免mcp工具中也存在搜索工具，识别错误
  if (fn === 'search_web' || name === 'search') {
    return 'search'
  }
  if (name === 'browser' || name.includes('browser') || fn.startsWith('browser_')) {
    return 'browser'
  }
  if (name === 'a2a' || name.includes('a2a')) {
    return 'a2a'
  }
  return 'default'
}

/**
 * 移除 MCP 工具名的 mcp_ 前缀，提取可读的工具名
 *
 * MCP 工具名格式: mcp_<server>_<tool_name> (如 mcp_system_getWarehousingDetailExport)
 * 移除 mcp_ 前缀后保留 server_tool_name,供前端展示具体操作。
 */
export function stripMcpPrefix(name: string): string {
  if (!name) return ''
  return name.startsWith('mcp_') ? name.slice(4) : name
}

/**
 * 根据工具 name/function 和 args 生成事件列表中的人性化提示
 */
export function getFriendlyToolLabel(data: ToolEvent | null | undefined): string {
  if (!data) return '—'
  const name = (data.name ?? '').toLowerCase()
  const fn = (data.function ?? '').toLowerCase()
  const args = data.args && typeof data.args === 'object' ? data.args : {}

  if (data.function === 'message_notify_user' || data.function === 'message_ask_user') {
    const text = typeof args.text === 'string' ? args.text : ''
    return text || '—'
  }

  const filepath = getArg(args, 'filepath', 'path', 'pathname')
  const dirPath = getArg(args, 'dir_path', 'directory', 'dir')
  const query = getArg(args, 'query', 'q')
  const command = getArg(args, 'command', 'cmd', 'script')
  const url = getArg(args, 'url', 'href', 'link')
  const key = getArg(args, 'key')

  if (name === 'file') {
    switch (fn) {
      case 'read_file':
        return filepath ? `正在读取文件 ${truncate(filepath, 60)}` : '正在读取文件'
      case 'write_file':
        return filepath ? `正在写入文件 ${truncate(filepath, 60)}` : '正在写入文件'
      case 'replace_in_file':
        return filepath ? `正在替换文件内容 ${truncate(filepath, 60)}` : '正在替换文件内容'
      case 'search_in_file':
        return filepath ? `正在在文件中搜索 ${truncate(filepath, 60)}` : '正在在文件中搜索'
      case 'find_files':
        return dirPath ? `正在查找文件 ${truncate(dirPath, 60)}` : '正在查找文件'
      case 'list_files':
        return dirPath ? `正在列出目录 ${truncate(dirPath, 60)}` : '正在列出目录'
      default:
        return filepath ? `正在访问文件 ${truncate(filepath, 60)}` : dirPath ? `正在访问目录 ${truncate(dirPath, 60)}` : '正在访问文件'
    }
  }

  if (name === 'browser' || fn.startsWith('browser_')) {
    switch (fn) {
      case 'browser_view':
        return '正在查看当前页面'
      case 'browser_navigate':
        return url ? `正在打开页面 ${truncate(url, 80)}` : '正在打开页面'
      case 'browser_restart':
        return url ? `正在重启浏览器并打开 ${truncate(url, 80)}` : '正在重启浏览器'
      case 'browser_click':
        return '正在点击页面元素'
      case 'browser_input':
        return '正在输入内容'
      case 'browser_move_mouse':
        return '正在移动鼠标'
      case 'browser_press_key':
        return key ? `正在按键 ${key}` : '正在按键'
      case 'browser_select_option':
        return '正在选择下拉选项'
      case 'browser_scroll_up':
        return '正在向上滚动页面'
      case 'browser_scroll_down':
        return '正在向下滚动页面'
      case 'browser_console_exec':
        return '正在执行控制台脚本'
      case 'browser_console_view':
        return '正在查看控制台输出'
      default:
        return url ? `正在打开页面 ${truncate(url, 80)}` : '正在使用浏览器访问页面'
    }
  }

  if (name === 'search' || fn === 'search_web' || fn.includes('search_web')) {
    return query ? `正在搜索 ${truncate(query, 60)}` : '正在搜索'
  }

  if (name === 'deep_research' || fn === 'deep_research') {
    return query ? `正在深度研究 ${truncate(query, 60)}` : '正在深度研究'
  }

  if (name === 'shell') {
    switch (fn) {
      case 'shell_execute':
        return command ? `正在执行命令 ${truncate(command, 60)}` : '正在执行命令'
      case 'shell_read_output':
        return '正在查看命令输出'
      case 'shell_wait':
        return '正在等待命令完成'
      case 'shell_write_input':
        return '正在向命令输入内容'
      case 'shell_kill_process':
        return '正在终止进程'
      default:
        return command ? `正在执行命令 ${truncate(command, 60)}` : '正在执行命令'
    }
  }

  if (name.includes('bash') || fn === 'run' || fn === 'execute' || fn === 'run_command') {
    const cmd = command || (typeof args.input === 'string' ? args.input : '')
    return cmd ? `正在执行命令 ${truncate(cmd, 60)}` : '正在执行命令'
  }

  if (name === 'a2a') {
    switch (fn) {
      case 'get_remote_agent_cards':
        return '正在获取远程 Agent 列表'
      case 'call_remote_agent':
        return query ? `正在调用远程 Agent：${truncate(query, 40)}` : '正在调用远程 Agent'
      default:
        return '正在调用 Agent'
    }
  }

  if (name === 'mcp' || name.startsWith('mcp_')) {
    // 桥接工具(懒加载模式)优先匹配: mcp_tool_search / mcp_tool_describe / mcp_tool_call
    if (fn === 'mcp_tool_search') {
      return query ? `正在搜索 MCP 工具：${truncate(query, 40)}` : '正在搜索 MCP 工具'
    }
    if (fn === 'mcp_tool_describe') {
      const descName = getArg(args, 'name')
      return descName ? `正在查看 MCP 工具详情：${truncate(stripMcpPrefix(descName), 40)}` : '正在查看 MCP 工具详情'
    }
    if (fn === 'mcp_tool_call') {
      const callName = getArg(args, 'name')
      const displayName = callName ? stripMcpPrefix(callName) : ''
      return displayName
        ? `正在通过 MCP 服务执行 ${truncate(displayName, 40)} 操作`
        : '正在通过 MCP 服务执行操作'
    }
    // 搜索类 MCP 工具(search_web 等)
    if (fn.includes('search_web') || fn.includes('search')) {
      return query ? `正在搜索 ${truncate(query, 60)}` : '正在搜索'
    }
    // 直接 MCP 工具调用(非懒加载模式): 使用原始 function 名保留大小写
    const directName = stripMcpPrefix(data.function ?? '')
    return directName
      ? `正在通过 MCP 服务执行 ${truncate(directName, 40)} 操作`
      : '正在通过 MCP 服务执行操作'
  }

  return '正在执行操作'
}
