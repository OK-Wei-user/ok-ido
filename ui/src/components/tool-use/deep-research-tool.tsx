'use client'

import { Microscope } from 'lucide-react'
import { ToolBadge } from './tool-badge'

export interface DeepResearchToolProps {
  label: string
  onClick?: () => void
}

/**
 * 深度研究工具列表项 - 与 SearchTool 风格一致的徽标组件
 */
export function DeepResearchTool({ label, onClick }: DeepResearchToolProps) {
  return <ToolBadge icon={Microscope} label={label} onClick={onClick} />
}
