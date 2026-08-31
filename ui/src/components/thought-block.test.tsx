import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { ThoughtBlock } from './thought-block'
import type { ChatMessage } from '@/lib/api/types'

/**
 * Mock MarkdownContent 避免 react-markdown 在 jsdom 环境的复杂性,
 * 仅保留 content 文本渲染用于断言展开/折叠状态。
 */
vi.mock('@/components/markdown-content', () => ({
  MarkdownContent: ({ content }: { content: string }) => (
    <div data-testid="markdown-content">{content}</div>
  ),
}))

/** 构造思考消息段 */
function mkThinking(message: string, extra?: Partial<ChatMessage>): ChatMessage {
  return {
    role: 'assistant',
    message,
    is_thinking: true,
    ...extra,
  }
}

describe('ThoughtBlock - 历史回放默认折叠(已完成会话)', () => {
  it('历史回放(is_final=true, is_streaming=false, 非活跃) → 内容不可见(折叠)', () => {
    const data = [mkThinking('历史思考内容', { is_final: true, is_streaming: false })]
    render(<ThoughtBlock data={data} hasFollowingAction />)

    // 非活跃历史回放: 默认折叠,内容不可见
    expect(screen.queryByText('历史思考内容')).not.toBeInTheDocument()
    expect(screen.getByText('已深度思考')).toBeInTheDocument()
  })

  it('历史回放无 hasFollowingAction → 仍折叠(非活跃)', () => {
    const data = [mkThinking('历史思考数据', { is_final: true, is_streaming: false })]
    render(<ThoughtBlock data={data} />)

    expect(screen.queryByText('历史思考数据')).not.toBeInTheDocument()
  })
})

describe('ThoughtBlock - 活跃思考块(运行中会话当前思考)', () => {
  it('活跃思考块(isActive=true, is_final=true) → 内容可见(展开)', () => {
    const data = [mkThinking('当前活跃思考', { is_final: true, is_streaming: false })]
    render(<ThoughtBlock data={data} isActive hasFollowingAction />)

    // 活跃思考块: 默认展开,用户切换到运行中会话即可看到当前思考
    expect(screen.getByText('当前活跃思考')).toBeInTheDocument()
    expect(screen.getByText('已深度思考')).toBeInTheDocument()
  })

  it('活跃思考块 + hasFollowingAction=true → 不自动折叠(保持展开)', () => {
    const data = [mkThinking('活跃思考不折叠', { is_final: true, is_streaming: false })]
    render(<ThoughtBlock data={data} isActive hasFollowingAction />)

    // 活跃思考块即使后续有行动也不折叠(运行中会话当前思考)
    expect(screen.getByText('活跃思考不折叠')).toBeInTheDocument()
  })
})

describe('ThoughtBlock - 实时流式展示', () => {
  it('实时流式(is_streaming=true) → 内容可见 + spinner 显示', () => {
    const data = [mkThinking('实时思考内容', { is_streaming: true, is_final: false })]
    render(<ThoughtBlock data={data} />)

    expect(screen.getByText('实时思考内容')).toBeInTheDocument()
    expect(screen.getByText('思考中')).toBeInTheDocument()
    // spinner 存在(Loader2 带 animate-spin class)
    expect(document.querySelector('.animate-spin')).toBeInTheDocument()
  })

  it('实时流式完成(rerender: streaming → completed + hasFollowingAction, 非活跃) → 自动折叠', () => {
    const streamingData = [mkThinking('思考过程中', { is_streaming: true, is_final: false })]
    const { rerender } = render(<ThoughtBlock data={streamingData} />)

    // 流式中内容可见
    expect(screen.getByText('思考过程中')).toBeInTheDocument()

    // rerender 为完成态 + hasFollowingAction=true → 触发自动折叠(非活跃)
    const completedData = [mkThinking('思考过程中', { is_streaming: false, is_final: true })]
    rerender(<ThoughtBlock data={completedData} hasFollowingAction />)

    // 折叠后内容不可见
    expect(screen.queryByText('思考过程中')).not.toBeInTheDocument()
    expect(screen.getByText('已深度思考')).toBeInTheDocument()
  })

  it('实时流式完成(活跃 isActive=true) → 不自动折叠', () => {
    const streamingData = [mkThinking('活跃流式思考', { is_streaming: true, is_final: false })]
    const { rerender } = render(<ThoughtBlock data={streamingData} isActive />)

    expect(screen.getByText('活跃流式思考')).toBeInTheDocument()

    // rerender 为完成态 + isActive=true → 不折叠(运行中会话当前思考)
    const completedData = [mkThinking('活跃流式思考', { is_streaming: false, is_final: true })]
    rerender(<ThoughtBlock data={completedData} isActive hasFollowingAction />)

    // 活跃思考块不折叠
    expect(screen.getByText('活跃流式思考')).toBeInTheDocument()
  })
})

describe('ThoughtBlock - 用户手动切换', () => {
  it('点击标题行 → 切换展开/折叠状态', () => {
    const data = [mkThinking('可切换内容', { is_final: true, is_streaming: false })]
    // 用 isActive 让初始展开,测试手动折叠再展开
    render(<ThoughtBlock data={data} isActive />)

    // 初始展开
    expect(screen.getByText('可切换内容')).toBeInTheDocument()

    // 点击标题折叠
    fireEvent.click(screen.getByText('已深度思考'))
    expect(screen.queryByText('可切换内容')).not.toBeInTheDocument()

    // 再点击展开
    fireEvent.click(screen.getByText('已深度思考'))
    expect(screen.getByText('可切换内容')).toBeInTheDocument()
  })
})
