import { describe, expect, it } from 'vitest'
import { eventsToTimeline, appendEventWithStreaming } from './session-events'
import type { SSEEventData, ChatMessage } from '@/lib/api/types'

function mkAssistant(message: string, extra?: Partial<ChatMessage>): SSEEventData {
  return {
    type: 'message',
    data: { role: 'assistant', message, ...extra } as ChatMessage,
  }
}

function mkUser(message: string, extra?: Partial<ChatMessage>): SSEEventData {
  return {
    type: 'message',
    data: { role: 'user', message, ...extra } as ChatMessage,
  }
}

function mkTool(name: string, fn: string, toolCallId?: string): SSEEventData {
  return {
    type: 'tool',
    data: {
      name,
      function: fn,
      args: {},
      ...(toolCallId ? { tool_call_id: toolCallId } : {}),
    },
  }
}

function mkStep(id: string, status: 'running' | 'completed' | 'failed' | 'pending'): SSEEventData {
  return {
    type: 'step',
    data: { id, status, description: `step-${id}` },
  }
}

/** 构造思考流式增量消息(is_thinking=true, is_streaming=true) */
function mkThinking(message: string, extra?: Partial<ChatMessage>): SSEEventData {
  return {
    type: 'message',
    data: {
      role: 'assistant',
      message,
      is_thinking: true,
      is_streaming: true,
      is_final: false,
      ...extra,
    } as ChatMessage,
  }
}

/** 构造思考最终聚合消息(is_thinking=true, is_final=true, 写DB+前端替换累积) */
function mkThinkingFinal(message: string, extra?: Partial<ChatMessage>): SSEEventData {
  return {
    type: 'message',
    data: {
      role: 'assistant',
      message,
      is_thinking: true,
      is_streaming: false,
      is_final: true,
      ...extra,
    } as ChatMessage,
  }
}

/** 构造 shell 工具事件(含 content.console 数组,可选 is_streaming) */
function mkShellTool(
  toolCallId: string,
  console: Array<{ ps1: string; command: string; output: string }>,
  streaming?: boolean,
): SSEEventData {
  return {
    type: 'tool',
    data: {
      name: 'shell',
      function: 'shell_execute',
      args: {},
      tool_call_id: toolCallId,
      content: { console },
      ...(streaming ? { is_streaming: true } : {}),
    },
  }
}

describe('eventsToTimeline - 非流式消息渲染', () => {
  it('单条 assistant 消息 → 1 个 assistant 项', () => {
    const events = [mkAssistant('hello world', { is_final: true })]
    const timeline = eventsToTimeline(events)
    const assistants = timeline.filter((t) => t.kind === 'assistant')
    expect(assistants).toHaveLength(1)
    const ast = assistants[0]
    expect(ast.kind).toBe('assistant')
    if (ast.kind === 'assistant') {
      expect(ast.data.message).toBe('hello world')
      expect(ast.data.is_final).toBe(true)
    }
  })

  it('多条 assistant 消息 → 多个独立 assistant 项（非流式不合并）', () => {
    const events = [
      mkAssistant('第一条回复'),
      mkAssistant('第二条回复', { is_final: true }),
    ]
    const timeline = eventsToTimeline(events)
    const assistants = timeline.filter((t) => t.kind === 'assistant')
    expect(assistants).toHaveLength(2)
    if (assistants[0].kind === 'assistant') {
      expect(assistants[0].data.message).toBe('第一条回复')
    }
    if (assistants[1].kind === 'assistant') {
      expect(assistants[1].data.message).toBe('第二条回复')
      expect(assistants[1].data.is_final).toBe(true)
    }
  })

  it('用户消息 + assistant 消息 → 各自独立项', () => {
    const events = [mkUser('提问'), mkAssistant('回答', { is_final: true })]
    const timeline = eventsToTimeline(events)
    const users = timeline.filter((t) => t.kind === 'user')
    const assistants = timeline.filter((t) => t.kind === 'assistant')
    expect(users).toHaveLength(1)
    expect(assistants).toHaveLength(1)
    if (users[0].kind === 'user') {
      expect(users[0].data.message).toBe('提问')
    }
    if (assistants[0].kind === 'assistant') {
      expect(assistants[0].data.message).toBe('回答')
    }
  })

  it('无 is_final 字段的旧版消息 → 兼容为整条消息', () => {
    const events = [mkAssistant('legacy complete message')]
    const timeline = eventsToTimeline(events)
    const ast = timeline.find((t) => t.kind === 'assistant')
    expect(ast?.kind).toBe('assistant')
    if (ast?.kind === 'assistant') {
      expect(ast.data.message).toBe('legacy complete message')
      expect(ast.data.is_final).toBeUndefined()
    }
  })

  it('assistant 消息带 attachments → 生成 attachments 项', () => {
    const events = [
      mkAssistant('请查看附件', {
        attachments: [{ file_id: 'f1', filename: 'report.pdf' }],
      }),
    ]
    const timeline = eventsToTimeline(events)
    const attachmentsItems = timeline.filter((t) => t.kind === 'attachments')
    expect(attachmentsItems).toHaveLength(1)
    if (attachmentsItems[0].kind === 'attachments') {
      expect(attachmentsItems[0].files).toHaveLength(1)
      expect(attachmentsItems[0].files[0].filename).toBe('report.pdf')
    }
  })

  it('用户消息带 attachments → 生成 attachments 项', () => {
    const events = [
      mkUser('请处理', {
        attachments: [
          { file_id: 'f1', filename: 'a.txt' },
          { file_id: 'f2', filename: 'b.txt' },
        ],
      }),
    ]
    const timeline = eventsToTimeline(events)
    const attachmentsItems = timeline.filter((t) => t.kind === 'attachments')
    expect(attachmentsItems).toHaveLength(1)
    if (attachmentsItems[0].kind === 'attachments') {
      expect(attachmentsItems[0].files).toHaveLength(2)
    }
  })
})

describe('eventsToTimeline - 工具事件归属', () => {
  it('独立 tool 事件（无 step 上下文）→ 独立 tool 项', () => {
    const events: SSEEventData[] = [
      {
        type: 'tool',
        data: {
          name: 'search',
          function: 'search_web',
          args: { query: 'test' },
          tool_call_id: 'call_001',
        },
      },
    ]
    const timeline = eventsToTimeline(events)
    const tools = timeline.filter((t) => t.kind === 'tool')
    expect(tools).toHaveLength(1)
    if (tools[0].kind === 'tool') {
      expect(tools[0].data.function).toBe('search_web')
    }
  })

  it('step 内的 tool 事件 → 添加到 step.tools', () => {
    const events = [
      mkStep('s1', 'running'),
      {
        type: 'tool',
        data: {
          name: 'shell',
          function: 'shell_execute',
          args: {},
          tool_call_id: 'call_001',
        },
      },
      mkStep('s1', 'completed'),
    ]
    const timeline = eventsToTimeline(events)
    const steps = timeline.filter((t) => t.kind === 'step')
    expect(steps).toHaveLength(1)
    if (steps[0].kind === 'step') {
      expect(steps[0].tools).toHaveLength(1)
      expect(steps[0].tools[0].function).toBe('shell_execute')
    }
  })

  it('相同 tool_call_id 的 tool 事件 → 更新而非新增', () => {
    const events = [
      mkStep('s1', 'running'),
      {
        type: 'tool',
        data: {
          name: 'shell',
          function: 'shell_execute',
          args: {},
          tool_call_id: 'call_001',
          status: 'calling',
        },
      },
      {
        type: 'tool',
        data: {
          name: 'shell',
          function: 'shell_execute',
          args: {},
          tool_call_id: 'call_001',
          status: 'called',
        },
      },
    ]
    const timeline = eventsToTimeline(events)
    const steps = timeline.filter((t) => t.kind === 'step')
    if (steps[0].kind === 'step') {
      expect(steps[0].tools).toHaveLength(1)
    }
  })

  it('独立 tool 事件相同 tool_call_id → 更新最后一个独立 tool', () => {
    const events: SSEEventData[] = [
      {
        type: 'tool',
        data: {
          name: 'search',
          function: 'search_web',
          args: {},
          tool_call_id: 'call_001',
          status: 'calling',
        },
      },
      {
        type: 'tool',
        data: {
          name: 'search',
          function: 'search_web',
          args: {},
          tool_call_id: 'call_001',
          status: 'called',
        },
      },
    ]
    const timeline = eventsToTimeline(events)
    const tools = timeline.filter((t) => t.kind === 'tool')
    expect(tools).toHaveLength(1)
  })
})

describe('eventsToTimeline - step 状态更新', () => {
  it('同一 step id 从 running 到 completed → 更新而非新增', () => {
    const events = [mkStep('s1', 'running'), mkStep('s1', 'completed')]
    const timeline = eventsToTimeline(events)
    const steps = timeline.filter((t) => t.kind === 'step')
    expect(steps).toHaveLength(1)
    if (steps[0].kind === 'step') {
      expect(steps[0].data.status).toBe('completed')
    }
  })

  it('多个不同 step → 多个 step 项', () => {
    const events = [
      mkStep('s1', 'running'),
      mkStep('s1', 'completed'),
      mkStep('s2', 'running'),
      mkStep('s2', 'completed'),
    ]
    const timeline = eventsToTimeline(events)
    const steps = timeline.filter((t) => t.kind === 'step')
    expect(steps).toHaveLength(2)
  })

  it('用户消息后 step 上下文清除 → 后续 tool 为独立项', () => {
    const events = [
      mkStep('s1', 'running'),
      mkUser('打断'),
      {
        type: 'tool',
        data: {
          name: 'search',
          function: 'search_web',
          args: {},
          tool_call_id: 'call_002',
        },
      },
    ]
    const timeline = eventsToTimeline(events)
    const tools = timeline.filter((t) => t.kind === 'tool')
    expect(tools).toHaveLength(1)
    const steps = timeline.filter((t) => t.kind === 'step')
    if (steps[0].kind === 'step') {
      expect(steps[0].tools).toHaveLength(0)
    }
  })
})

describe('eventsToTimeline - 错误事件', () => {
  it('error 事件 → error 项', () => {
    const events: SSEEventData[] = [
      { type: 'error', data: { error: '任务执行失败' } },
    ]
    const timeline = eventsToTimeline(events)
    const errors = timeline.filter((t) => t.kind === 'error')
    expect(errors).toHaveLength(1)
    if (errors[0].kind === 'error') {
      expect(errors[0].error).toBe('任务执行失败')
    }
  })

  it('error 事件无 error 字段 → 不生成项', () => {
    const events: SSEEventData[] = [
      { type: 'error', data: {} },
    ]
    const timeline = eventsToTimeline(events)
    const errors = timeline.filter((t) => t.kind === 'error')
    expect(errors).toHaveLength(0)
  })
})

describe('eventsToTimeline - 忽略的事件类型', () => {
  it('title/plan/wait/done 事件不生成 timeline 项', () => {
    const events: SSEEventData[] = [
      { type: 'title', data: { title: '任务标题' } },
      { type: 'plan', data: { steps: [] } },
      { type: 'wait', data: {} },
      { type: 'done', data: {} },
    ]
    const timeline = eventsToTimeline(events)
    expect(timeline).toHaveLength(0)
  })
})

describe('appendEventWithStreaming - 流式delta聚合', () => {
  it('is_streaming=true 首个delta → 新增流式消息项', () => {
    const prev: SSEEventData[] = []
    const ev = mkAssistant('hello', { is_streaming: true, is_final: false })
    const result = appendEventWithStreaming(prev, ev)
    expect(result).toHaveLength(1)
    const msg = result[0].data as ChatMessage
    expect(msg.message).toBe('hello')
    expect(msg.is_streaming).toBe(true)
    expect(msg.is_final).toBe(false)
  })

  it('is_streaming=true 后续delta → 合并到最后一条流式消息', () => {
    const prev: SSEEventData[] = [
      mkAssistant('hello', { is_streaming: true, is_final: false }),
    ]
    const ev = mkAssistant(' world', { is_streaming: true, is_final: false })
    const result = appendEventWithStreaming(prev, ev)
    expect(result).toHaveLength(1)
    const msg = result[0].data as ChatMessage
    expect(msg.message).toBe('hello world')
    expect(msg.is_streaming).toBe(true)
  })

  it('多个 is_streaming delta → 顺序累积', () => {
    const deltas = ['Hello', ', ', 'world', '!']
    let events: SSEEventData[] = []
    for (const d of deltas) {
      events = appendEventWithStreaming(events, mkAssistant(d, { is_streaming: true, is_final: false }))
    }
    expect(events).toHaveLength(1)
    const msg = events[0].data as ChatMessage
    expect(msg.message).toBe('Hello, world!')
    expect(msg.is_streaming).toBe(true)
  })

  it('is_final=true → 替换最后一条流式消息', () => {
    const prev: SSEEventData[] = [
      mkAssistant('partial content', { is_streaming: true, is_final: false }),
    ]
    const ev = mkAssistant('final complete answer', { is_streaming: false, is_final: true })
    const result = appendEventWithStreaming(prev, ev)
    expect(result).toHaveLength(1)
    const msg = result[0].data as ChatMessage
    expect(msg.message).toBe('final complete answer')
    expect(msg.is_streaming).toBe(false)
    expect(msg.is_final).toBe(true)
  })

  it('is_final=true 无前置流式消息 → 直接追加', () => {
    const prev: SSEEventData[] = []
    const ev = mkAssistant('direct final', { is_streaming: false, is_final: true })
    const result = appendEventWithStreaming(prev, ev)
    expect(result).toHaveLength(1)
    const msg = result[0].data as ChatMessage
    expect(msg.message).toBe('direct final')
    expect(msg.is_final).toBe(true)
  })

  it('流式delta后接非流式非final消息 → 各自独立', () => {
    const prev: SSEEventData[] = [
      mkAssistant('streaming', { is_streaming: true, is_final: false }),
    ]
    const ev = mkAssistant('normal message')
    const result = appendEventWithStreaming(prev, ev)
    expect(result).toHaveLength(2)
    const msg1 = result[0].data as ChatMessage
    expect(msg1.is_streaming).toBe(true)
    const msg2 = result[1].data as ChatMessage
    expect(msg2.is_streaming).toBeUndefined()
  })

  it('用户消息 → 直接追加（不参与流式聚合）', () => {
    const prev: SSEEventData[] = [
      mkAssistant('streaming', { is_streaming: true, is_final: false }),
    ]
    const ev = mkUser('user question')
    const result = appendEventWithStreaming(prev, ev)
    expect(result).toHaveLength(2)
    expect(result[1].type).toBe('message')
    const msg = result[1].data as ChatMessage
    expect(msg.role).toBe('user')
  })

  it('非消息事件 → 直接追加', () => {
    const prev: SSEEventData[] = [
      mkAssistant('streaming', { is_streaming: true, is_final: false }),
    ]
    const ev: SSEEventData = { type: 'tool', data: { name: 'search', function: 'search_web', args: {} } }
    const result = appendEventWithStreaming(prev, ev)
    expect(result).toHaveLength(2)
    expect(result[1].type).toBe('tool')
  })

  it('is_final=true 保留 attachments', () => {
    const prev: SSEEventData[] = [
      mkAssistant('partial', { is_streaming: true, is_final: false }),
    ]
    const ev = mkAssistant('final with file', {
      is_streaming: false,
      is_final: true,
      attachments: [{ file_id: 'f1', filename: 'report.pdf' }],
    })
    const result = appendEventWithStreaming(prev, ev)
    expect(result).toHaveLength(1)
    const msg = result[0].data as ChatMessage
    expect(msg.attachments).toHaveLength(1)
    expect(msg.attachments![0].filename).toBe('report.pdf')
  })

  it('完整流式流程：delta累积→final替换', () => {
    let events: SSEEventData[] = []
    // 模拟3个delta chunk
    events = appendEventWithStreaming(events, mkAssistant('Hello', { is_streaming: true, is_final: false }))
    events = appendEventWithStreaming(events, mkAssistant(', ', { is_streaming: true, is_final: false }))
    events = appendEventWithStreaming(events, mkAssistant('world!', { is_streaming: true, is_final: false }))
    expect(events).toHaveLength(1)
    expect((events[0].data as ChatMessage).message).toBe('Hello, world!')

    // 最终答案替换
    events = appendEventWithStreaming(events, mkAssistant('Hello, world! Final answer.', {
      is_streaming: false,
      is_final: true,
    }))
    expect(events).toHaveLength(1)
    const msg = events[0].data as ChatMessage
    expect(msg.message).toBe('Hello, world! Final answer.')
    expect(msg.is_streaming).toBe(false)
    expect(msg.is_final).toBe(true)
  })

  it('不修改原数组（返回新数组）', () => {
    const prev: SSEEventData[] = [
      mkAssistant('original', { is_streaming: true, is_final: false }),
    ]
    const ev = mkAssistant(' delta', { is_streaming: true, is_final: false })
    const result = appendEventWithStreaming(prev, ev)
    expect(result).not.toBe(prev)
    expect(prev).toHaveLength(1)
    expect((prev[0].data as ChatMessage).message).toBe('original')
    expect(result).toHaveLength(1)
    expect((result[0].data as ChatMessage).message).toBe('original delta')
  })
})

describe('appendEventWithStreaming - 思考增量与最终答案增量分组聚合(改进A)', () => {
  it('思考流式delta + 最终答案流式delta → 互不合并,各自独立项', () => {
    let events: SSEEventData[] = []
    events = appendEventWithStreaming(events, mkThinking('思考片段一'))
    events = appendEventWithStreaming(events, mkAssistant('最终答案片段', { is_streaming: true, is_final: false }))

    expect(events).toHaveLength(2)
    const msg1 = events[0].data as ChatMessage
    const msg2 = events[1].data as ChatMessage
    expect(msg1.is_thinking).toBe(true)
    expect(msg1.message).toBe('思考片段一')
    expect(msg2.is_thinking).toBeUndefined()
    expect(msg2.message).toBe('最终答案片段')
  })

  it('思考流式delta累积 → 合并到最后一条思考消息', () => {
    let events: SSEEventData[] = []
    events = appendEventWithStreaming(events, mkThinking('思考'))
    events = appendEventWithStreaming(events, mkThinking('继续'))

    expect(events).toHaveLength(1)
    const msg = events[0].data as ChatMessage
    expect(msg.is_thinking).toBe(true)
    expect(msg.message).toBe('思考继续')
  })

  it('思考 is_final=true → 替换最后一条思考流式消息(聚合为完整思考)', () => {
    let events: SSEEventData[] = []
    events = appendEventWithStreaming(events, mkThinking('思考片段'))
    events = appendEventWithStreaming(events, mkThinking('继续'))
    // 思考聚合事件: 替换累积的思考流式消息
    events = appendEventWithStreaming(events, mkThinkingFinal('思考片段继续完整原文'))

    expect(events).toHaveLength(1)
    const msg = events[0].data as ChatMessage
    expect(msg.is_thinking).toBe(true)
    expect(msg.is_final).toBe(true)
    expect(msg.is_streaming).toBe(false)
    expect(msg.message).toBe('思考片段继续完整原文')
  })

  it('is_final=true 仅替换非思考流式消息,不替换思考消息', () => {
    let events: SSEEventData[] = []
    events = appendEventWithStreaming(events, mkThinking('思考过程'))
    events = appendEventWithStreaming(events, mkAssistant('答案增量', { is_streaming: true, is_final: false }))
    events = appendEventWithStreaming(events, mkAssistant('完整最终答案', { is_streaming: false, is_final: true }))

    expect(events).toHaveLength(2)
    const thinking = events[0].data as ChatMessage
    const final = events[1].data as ChatMessage
    // 思考消息保留(不被最终答案替换)
    expect(thinking.is_thinking).toBe(true)
    expect(thinking.message).toBe('思考过程')
    // 最终答案替换了流式答案增量
    expect(final.is_final).toBe(true)
    expect(final.message).toBe('完整最终答案')
  })

  it('思考 is_final + 最终答案 is_final → 各自替换同类流式消息(互不干扰)', () => {
    // 实际流式顺序: 思考增量 → 思考聚合 → 答案增量 → 答案聚合
    // (is_final 仅替换「最后一条同类流式消息」,故同类须连续到达)
    let events: SSEEventData[] = []
    events = appendEventWithStreaming(events, mkThinking('思考增量'))
    // 思考聚合替换思考流式(此时最后一条是思考流式,可替换)
    events = appendEventWithStreaming(events, mkThinkingFinal('完整思考'))
    events = appendEventWithStreaming(events, mkAssistant('答案增量', { is_streaming: true, is_final: false }))
    // 最终答案聚合替换答案流式(此时最后一条是答案流式,可替换)
    events = appendEventWithStreaming(events, mkAssistant('完整答案', { is_streaming: false, is_final: true }))

    expect(events).toHaveLength(2)
    const thinking = events[0].data as ChatMessage
    const final = events[1].data as ChatMessage
    expect(thinking.is_thinking).toBe(true)
    expect(thinking.is_final).toBe(true)
    expect(thinking.message).toBe('完整思考')
    expect(final.is_final).toBe(true)
    expect(final.message).toBe('完整答案')
  })
})

describe('eventsToTimeline - 思考消息进时间线作为thought项(Thought永驻)', () => {
  it('is_thinking=true 流式增量消息 → 生成 thought 项(永驻展示)', () => {
    const events = [mkThinking('思考增量内容应进时间线')]
    const timeline = eventsToTimeline(events)
    expect(timeline).toHaveLength(1)
    expect(timeline[0].kind).toBe('thought')
    if (timeline[0].kind === 'thought') {
      expect(timeline[0].data).toHaveLength(1)
      expect(timeline[0].data[0].message).toBe('思考增量内容应进时间线')
      expect(timeline[0].data[0].is_thinking).toBe(true)
    }
  })

  it('is_thinking=true 最终聚合消息 → 生成 thought 项(历史回放可见)', () => {
    const events = [mkThinkingFinal('完整思考原文')]
    const timeline = eventsToTimeline(events)
    expect(timeline).toHaveLength(1)
    expect(timeline[0].kind).toBe('thought')
    if (timeline[0].kind === 'thought') {
      expect(timeline[0].data).toHaveLength(1)
      expect(timeline[0].data[0].message).toBe('完整思考原文')
      expect(timeline[0].data[0].is_final).toBe(true)
    }
  })

  it('思考消息 + 正常 assistant 消息 → thought 与 assistant 各自独立项', () => {
    const events = [
      mkThinkingFinal('思考过程'),
      mkAssistant('正式回复', { is_final: true }),
    ]
    const timeline = eventsToTimeline(events)
    const thoughts = timeline.filter((t) => t.kind === 'thought')
    const assistants = timeline.filter((t) => t.kind === 'assistant')
    expect(thoughts).toHaveLength(1)
    expect(assistants).toHaveLength(1)
    if (thoughts[0].kind === 'thought') {
      expect(thoughts[0].data).toHaveLength(1)
      expect(thoughts[0].data[0].message).toBe('思考过程')
    }
    if (assistants[0].kind === 'assistant') {
      expect(assistants[0].data.message).toBe('正式回复')
    }
  })

  it('同轮次连续思考消息(流式增量+聚合) → 合并为 1 个 thought 块(2段)', () => {
    // eventsToTimeline 看到的是 appendEventWithStreaming 聚合后的结果,
    // 但若直接传入未聚合的原始事件,同轮次连续思考合并为单个块(多段展示)
    const events = [
      mkThinking('片段一'),
      mkThinkingFinal('片段一完整'),
    ]
    const timeline = eventsToTimeline(events)
    const thoughts = timeline.filter((t) => t.kind === 'thought')
    expect(thoughts).toHaveLength(1)
    if (thoughts[0].kind === 'thought') {
      expect(thoughts[0].data).toHaveLength(2)
      expect(thoughts[0].data[0].message).toBe('片段一')
      expect(thoughts[0].data[1].message).toBe('片段一完整')
    }
  })
})

describe('eventsToTimeline - ThoughtBlock 多段思考按轮次合并', () => {
  it('同轮次多段思考(被 step/tool 隔开) → 合并为 1 个 thought 块', () => {
    const events = [
      mkUser('分析趋势'),
      mkThinkingFinal('先思考第一步'),
      mkStep('s1', 'running'),
      mkStep('s1', 'completed'),
      mkThinkingFinal('再思考第二步'),
      mkStep('s2', 'running'),
      mkStep('s2', 'completed'),
      mkThinkingFinal('最后思考第三步'),
      mkAssistant('最终答案', { is_final: true }),
    ]
    const timeline = eventsToTimeline(events)
    const thoughts = timeline.filter((t) => t.kind === 'thought')
    expect(thoughts).toHaveLength(1)
    if (thoughts[0].kind === 'thought') {
      expect(thoughts[0].data).toHaveLength(3)
      expect(thoughts[0].data[0].message).toBe('先思考第一步')
      expect(thoughts[0].data[1].message).toBe('再思考第二步')
      expect(thoughts[0].data[2].message).toBe('最后思考第三步')
    }
  })

  it('不同轮次(user 分隔)的思考 → 各自独立 thought 块', () => {
    const events = [
      mkUser('第一个问题'),
      mkThinkingFinal('第一轮思考'),
      mkAssistant('第一轮答案', { is_final: true }),
      mkUser('第二个问题'),
      mkThinkingFinal('第二轮思考'),
      mkAssistant('第二轮答案', { is_final: true }),
    ]
    const timeline = eventsToTimeline(events)
    const thoughts = timeline.filter((t) => t.kind === 'thought')
    expect(thoughts).toHaveLength(2)
    if (thoughts[0].kind === 'thought') {
      expect(thoughts[0].data).toHaveLength(1)
      expect(thoughts[0].data[0].message).toBe('第一轮思考')
    }
    if (thoughts[1].kind === 'thought') {
      expect(thoughts[1].data).toHaveLength(1)
      expect(thoughts[1].data[0].message).toBe('第二轮思考')
    }
  })

  it('合并块位置 = 首段思考出现处(先思考后行动心智模型)', () => {
    const events = [
      mkUser('分析'),
      mkThinkingFinal('首段思考'),
      mkStep('s1', 'running'),
      mkStep('s1', 'completed'),
      mkThinkingFinal('次段思考'),
      mkAssistant('答案', { is_final: true }),
    ]
    const timeline = eventsToTimeline(events)
    // 顺序应为: user → thought(合并块) → step → assistant
    expect(timeline[0].kind).toBe('user')
    expect(timeline[1].kind).toBe('thought')
    expect(timeline[2].kind).toBe('step')
    expect(timeline[3].kind).toBe('assistant')
    if (timeline[1].kind === 'thought') {
      expect(timeline[1].data).toHaveLength(2)
    }
  })

  it('流式增量 + 聚合混合的思考段 → 同块内按时间序排列', () => {
    const events = [
      mkUser('提问'),
      mkThinking('流式片段'),
      mkThinkingFinal('流式片段完整原文'),
      mkStep('s1', 'running'),
      mkStep('s1', 'completed'),
      mkThinkingFinal('第二段完整思考'),
      mkAssistant('答案', { is_final: true }),
    ]
    const timeline = eventsToTimeline(events)
    const thoughts = timeline.filter((t) => t.kind === 'thought')
    expect(thoughts).toHaveLength(1)
    if (thoughts[0].kind === 'thought') {
      expect(thoughts[0].data).toHaveLength(3)
      expect(thoughts[0].data[0].message).toBe('流式片段')
      expect(thoughts[0].data[1].message).toBe('流式片段完整原文')
      expect(thoughts[0].data[2].message).toBe('第二段完整思考')
    }
  })

  it('无前置 user 的思考(历史回放场景) → 独立成块', () => {
    const events = [
      mkThinkingFinal('历史思考一'),
      mkThinkingFinal('历史思考二'),
      mkAssistant('历史答案', { is_final: true }),
    ]
    const timeline = eventsToTimeline(events)
    const thoughts = timeline.filter((t) => t.kind === 'thought')
    expect(thoughts).toHaveLength(1)
    if (thoughts[0].kind === 'thought') {
      expect(thoughts[0].data).toHaveLength(2)
    }
  })
})

describe('eventsToTimeline - shell 工具 console 累积与替换(改进B)', () => {
  it('相同 tool_call_id 的 streaming ToolEvent → console append 累积', () => {
    const rec1 = { ps1: '$', command: 'ls', output: 'file1.txt' }
    const rec2 = { ps1: '$', command: 'ls', output: 'file2.txt' }
    const events = [
      mkStep('s1', 'running'),
      mkShellTool('call_001', [rec1], true),
      mkShellTool('call_001', [rec2], true),
    ]
    const timeline = eventsToTimeline(events)
    const steps = timeline.filter((t) => t.kind === 'step')
    expect(steps).toHaveLength(1)
    if (steps[0].kind === 'step') {
      expect(steps[0].tools).toHaveLength(1)
      const console = (steps[0].tools[0].content as { console: unknown[] }).console
      expect(console).toHaveLength(2)
    }
  })

  it('CALLED ToolEvent → console replace 为完整(非追加)', () => {
    const rec1 = { ps1: '$', command: 'ls', output: 'file1.txt' }
    const rec2 = { ps1: '$', command: 'ls', output: 'file2.txt' }
    const rec3 = { ps1: '$', command: 'ls', output: 'file3.txt' }
    const events = [
      mkStep('s1', 'running'),
      mkShellTool('call_001', [rec1], true),
      mkShellTool('call_001', [rec2], true),
      // CALLED 携带完整3条,replace 而非 append
      mkShellTool('call_001', [rec1, rec2, rec3], false),
    ]
    const timeline = eventsToTimeline(events)
    const steps = timeline.filter((t) => t.kind === 'step')
    if (steps[0].kind === 'step') {
      expect(steps[0].tools).toHaveLength(1)
      const console = (steps[0].tools[0].content as { console: unknown[] }).console
      expect(console).toHaveLength(3)
    }
  })
})
