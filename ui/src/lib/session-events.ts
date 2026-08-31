/**
 * 将 SSE 事件列表转换为时间线展示项与计划步骤
 * 与 chat 流式 / 任务详情接口的响应格式一致
 *
 * 后端事件格式为 { event: "message"|"title"|..., data: {...} }，
 * 前端统一使用 { type, data }，需先归一化。
 */

import type {
  SSEEventData,
  SSEEventType,
  ChatMessage,
  PlanStep,
  PlanEvent,
  StepEvent,
  ToolEvent,
  SessionFile,
} from "@/lib/api/types";

/** 后端返回的原始事件（可能用 event 或 type 表示类型） */
type RawEvent = { event?: string; type?: string; data?: unknown };

/**
 * 将后端单条事件转为前端 SSEEventData（统一 type + data）
 */
export function normalizeEvent(raw: RawEvent): SSEEventData | null {
  const type = (raw.type ?? raw.event) as SSEEventType | undefined;
  const data = raw.data;
  if (!type || data === undefined) return null;
  return { type, data } as SSEEventData;
}

/**
 * 将后端事件列表转为前端 SSEEventData[]
 */
export function normalizeEvents(rawList: unknown): SSEEventData[] {
  if (!Array.isArray(rawList)) return [];
  const out: SSEEventData[] = [];
  for (const raw of rawList) {
    const normalized = normalizeEvent(raw as RawEvent);
    if (normalized) out.push(normalized);
  }
  return out;
}

/**
 * 将新事件合并到已有事件列表，处理流式delta聚合。
 *
 * - is_streaming=true 的 assistant 增量chunk：按 is_thinking 分组聚合
 *   - is_thinking=true（思考增量）：合并到最后一条「思考流式消息」（若存在），否则新增
 *   - is_thinking=false（最终答案增量）：合并到最后一条「非思考流式消息」（若存在），否则新增
 *   - 思考与最终答案互不合并，避免思考内容污染最终答案累积
 * - is_final=true 的 assistant 最终答案：替换最后一条「非思考流式消息」（若存在），否则新增。
 *   思考消息不被最终答案替换（思考属过程性内容，独立展示）。
 * - 其余事件：直接追加。
 *
 * 返回新数组（不修改原数组），适合 setEvents((prev) => appendEventWithStreaming(prev, ev))。
 */
export function appendEventWithStreaming(
  prev: SSEEventData[],
  ev: SSEEventData
): SSEEventData[] {
  if (ev.type === "message") {
    const msgData = ev.data as ChatMessage;
    if (msgData.role === "assistant" && msgData.is_streaming) {
      const isThinking = !!msgData.is_thinking;
      const last = prev[prev.length - 1];
      const lastMsg = last?.data as ChatMessage | undefined;
      // 同类(思考/最终答案)流式消息才合并,避免二者错误交织
      if (
        last?.type === "message" &&
        lastMsg?.role === "assistant" &&
        lastMsg?.is_streaming &&
        !!lastMsg?.is_thinking === isThinking
      ) {
        const merged: SSEEventData = {
          type: "message",
          data: {
            ...lastMsg,
            message: (lastMsg.message || "") + (msgData.message || ""),
          } as ChatMessage,
        };
        return [...prev.slice(0, -1), merged];
      }
      return [...prev, ev];
    } else if (msgData.role === "assistant" && msgData.is_final) {
      const isThinking = !!msgData.is_thinking;
      const last = prev[prev.length - 1];
      const lastMsg = last?.data as ChatMessage | undefined;
      // 替换最后一条「同类(思考/非思考)流式消息」:
      // - 非思考 is_final: 替换最终答案累积流式消息
      // - 思考 is_final: 替换思考累积流式消息(聚合为完整思考,前端 ThoughtBlock 据此折叠)
      if (
        last?.type === "message" &&
        lastMsg?.role === "assistant" &&
        lastMsg?.is_streaming &&
        !!lastMsg?.is_thinking === isThinking
      ) {
        return [...prev.slice(0, -1), ev];
      }
      return [...prev, ev];
    }
  }
  return [...prev, ev];
}

/** 时间线单项：用于渲染对话区的一条记录 */
export type TimelineItem =
  | { kind: "user"; id: string; data: ChatMessage }
  | { kind: "attachments"; id: string; role: "user" | "assistant"; files: AttachmentFile[] }
  | { kind: "assistant"; id: string; data: ChatMessage }
  | { kind: "thought"; id: string; data: ChatMessage[] }
  | { kind: "tool"; id: string; data: ToolEvent; timeLabel?: string }
  | { kind: "step"; id: string; data: StepEvent; tools: ToolEvent[] }
  | { kind: "error"; id: string; error: string; timestamp?: number };

/** 附件展示用（文件名、类型、大小等） */
export type AttachmentFile = {
  id: string;
  filename: string;
  extension: string;
  size: number;
  sizeLabel?: string;
};

/** 从 SessionFile 转为 AttachmentFile */
export function sessionFileToAttachment(f: SessionFile): AttachmentFile {
  return {
    id: f.id,
    filename: f.filename,
    extension: f.extension,
    size: f.size,
  };
}

/** 从 ChatMessage.attachments 项转为 AttachmentFile（无 size 时用 0） */
export function chatAttachmentToDisplay(
  a: { file_id?: string; id?: string; filename: string; size?: number; [key: string]: unknown }
): AttachmentFile {
  const ext = (a.filename || "").split(".").pop() || "";
  return {
    id: a.file_id || a.id || "",
    filename: a.filename || "",
    extension: ext,
    size: typeof a.size === "number" ? a.size : 0,
  };
}

function stableId(prefix: string, index: number, suffix: string): string {
  return `${prefix}-${index}-${suffix}`;
}

/** 将时间戳格式化为相对时间，如 2天前、刚刚 */
function formatTimeLabel(ts: number | string | undefined): string | undefined {
  if (ts === undefined || ts === null) return undefined;
  let t = typeof ts === "string" ? parseInt(ts, 10) : ts;
  if (Number.isNaN(t)) return undefined;
  
  // 后端返回的是秒级时间戳（10位数），需要转为毫秒级（13位数）
  if (t < 10000000000) {
    t = t * 1000;
  }
  
  const now = Date.now();
  const diff = now - t;
  if (diff < 0) return "刚刚";
  if (diff < 60 * 1000) return "刚刚";
  if (diff < 60 * 60 * 1000) return `${Math.floor(diff / (60 * 1000))}分钟前`;
  if (diff < 24 * 60 * 60 * 1000) return `${Math.floor(diff / (60 * 60 * 1000))}小时前`;
  if (diff < 2 * 24 * 60 * 60 * 1000) return "昨天";
  if (diff < 7 * 24 * 60 * 60 * 1000) return `${Math.floor(diff / (24 * 60 * 60 * 1000))}天前`;
  if (diff < 30 * 24 * 60 * 60 * 1000) return `${Math.floor(diff / (7 * 24 * 60 * 60 * 1000))}周前`;
  return undefined;
}

export function getToolTimeLabel(tool: ToolEvent): string | undefined {
  const ts = (tool as { timestamp?: number; created_at?: number; ts?: number }).timestamp
    ?? (tool as { created_at?: number }).created_at
    ?? (tool as { ts?: number }).ts;
  return formatTimeLabel(ts);
}

/** 从工具 content 中提取 shell console 数组(非数组返回 null) */
function extractConsoleArray(tool: ToolEvent): unknown[] | null {
  const c = tool.content as { console?: unknown } | undefined;
  if (c && Array.isArray(c.console)) return c.console as unknown[];
  return null;
}

/**
 * 合并相同 tool_call_id 的 shell 工具更新(改进B)
 * - 新事件 is_streaming=true 且已有事件也是 is_streaming: console 累积 append(中间轮询增量)
 * - 否则(CALLED 到达或非流式): 用新事件的 console replace(完整输出)
 * 非 shell 工具直接返回新事件(向后兼容,保持原覆盖语义)。
 */
function mergeToolUpdate(existing: ToolEvent, incoming: ToolEvent): ToolEvent {
  if (incoming.name !== "shell") return incoming;
  if (incoming.is_streaming && existing.is_streaming) {
    const existingConsole = extractConsoleArray(existing);
    const incomingConsole = extractConsoleArray(incoming);
    if (existingConsole && incomingConsole) {
      return { ...incoming, content: { console: [...existingConsole, ...incomingConsole] } };
    }
  }
  return incoming;
}

/**
 * 将 SSE 事件列表归并为时间线展示项（顺序与设计一致）
 */
export function eventsToTimeline(events: SSEEventData[]): TimelineItem[] {
  const list: TimelineItem[] = [];
  let lastStepId: string | null = null;
  let messageIndex = 0;
  let toolIndex = 0;
  let stepIndex = 0;
  let errorIndex = 0;
  // 当前对话轮次的合并思考块索引(null=无活跃块,思考阶段已结束或尚未开始)
  // 同一轮次(user消息→非思考assistant消息)内的所有思考段合并为一个 ThoughtBlock,
  // 解决多段思考碎片化问题(一个会话可能产生30+段思考)
  let currentThoughtBlockIdx: number | null = null;

  for (const ev of events) {
    switch (ev.type) {
      case "message": {
        const msg = ev.data as ChatMessage;
        // Thought: 思考消息(is_thinking)合并到当前轮次的 ThoughtBlock
        // - 无活跃块: 新建块(位置=首段思考出现处,符合"思考在行动之前"心智模型)
        // - 有活跃块: 追加到该块data数组(段间在 ThoughtBlock 内用分隔线区分)
        if (msg.is_thinking) {
          if (currentThoughtBlockIdx !== null && currentThoughtBlockIdx < list.length) {
            const block = list[currentThoughtBlockIdx];
            if (block.kind === "thought") {
              list[currentThoughtBlockIdx] = {
                ...block,
                data: [...block.data, msg],
              };
              break;
            }
          }
          // 新建合并块(首段思考生成ID,后续吸收段不递增messageIndex保证ID稳定)
          list.push({
            kind: "thought",
            id: stableId("thought", messageIndex++, String(list.length)),
            data: [msg],
          });
          currentThoughtBlockIdx = list.length - 1;
          break;
        }
        if (msg.role === "user") {
          // 用户消息标志新对话轮次: 结束当前思考块 + 清除 step 上下文
          currentThoughtBlockIdx = null;
          lastStepId = null;

          list.push({
            kind: "user",
            id: stableId("user", messageIndex++, String(list.length)),
            data: msg,
          });
          if (msg.attachments && msg.attachments.length > 0) {
            list.push({
              kind: "attachments",
              id: stableId("att", messageIndex, "user"),
              role: "user",
              files: msg.attachments.map(chatAttachmentToDisplay),
            });
          }
        } else if (msg.role === "assistant") {
          // 非思考 assistant 消息: 本轮思考阶段结束,开始输出答案
          currentThoughtBlockIdx = null;
          // assistant 消息直接 push 为新的 assistant 项。
          // 流式delta聚合由 appendEventWithStreaming 在 setEvents 阶段完成，
          // eventsToTimeline 看到的是聚合后的单条消息（is_streaming=true 累积或 is_final=true 完整）。
          // is_streaming 字段用于 chat-message.tsx 渲染流式光标。
          list.push({
            kind: "assistant",
            id: stableId("assistant", messageIndex++, String(list.length)),
            data: msg,
          });
          if (msg.attachments && msg.attachments.length > 0) {
            list.push({
              kind: "attachments",
              id: stableId("att", messageIndex, "assistant"),
              role: "assistant",
              files: msg.attachments.map(chatAttachmentToDisplay),
            });
          }
        }
        break;
      }
      case "step": {
        const step = ev.data as StepEvent;
        
        // 判断是更新现有 step 还是创建新 step
        // 关键：只有当 lastStepId === step.id 时才是同一个 step 的状态更新
        if (lastStepId !== null && lastStepId === step.id) {
          // 这是同一个 step 的状态更新（running -> completed）
          // 从后往前查找，确保找到最新的（最后一个）匹配的 step
          let existingIdx = -1;
          for (let i = list.length - 1; i >= 0; i--) {
            const item = list[i];
            if (item.kind === "step" && item.data.id === step.id) {
              existingIdx = i;
              break;
            }
          }
          
          if (existingIdx >= 0) {
            const existing = list[existingIdx];
            if (existing.kind === "step") {
              list[existingIdx] = { 
                kind: "step", 
                id: existing.id, 
                data: step, 
                tools: existing.tools // 保留已有的 tools
              };
            }
          }
        } else {
          // 新的 step (第一次出现或新对话轮次的 step)
          list.push({
            kind: "step",
            id: stableId("step", stepIndex++, step.id + "_" + String(list.length)),
            data: step,
            tools: [], // 初始为空，tools 会在后续添加
          });
        }
        
        // 更新 lastStepId 跟踪
        // 只要 step 不是 completed/failed 状态，就保持跟踪
        if (step.status === 'completed' || step.status === 'failed') {
          lastStepId = null;
        } else {
          // running, pending 等其他状态都设置 lastStepId
          lastStepId = step.id;
        }
        
        break;
      }
      case "tool": {
        const tool = ev.data as ToolEvent;
        const toolCallId = (tool as { tool_call_id?: string }).tool_call_id;

        if (lastStepId !== null) {
          // 工具属于当前 step，添加到 step 的 tools 中
          // 重要：从后往前查找，确保找到最新的（最后一个）匹配的 step
          let stepIdx = -1;
          for (let i = list.length - 1; i >= 0; i--) {
            const item = list[i];
            if (item.kind === "step" && item.data.id === lastStepId) {
              stepIdx = i;
              break;
            }
          }
          
          if (stepIdx >= 0) {
            const step = list[stepIdx];
            if (step.kind === "step") {
              if (toolCallId != null) {
                // 检查是否已存在相同 tool_call_id 的工具（更新场景）
                const existingToolIdx = step.tools.findIndex(
                  (t) => (t as { tool_call_id?: string }).tool_call_id === toolCallId
                );
                if (existingToolIdx >= 0) {
                  // 更新现有工具(改进B: shell streaming 事件 console 累积)
                  const newTools = [...step.tools];
                  newTools[existingToolIdx] = mergeToolUpdate(step.tools[existingToolIdx], tool);
                  list[stepIdx] = { ...step, tools: newTools };
                  break;
                }
              }
              // 添加新工具
              list[stepIdx] = { ...step, tools: [...step.tools, tool] };
            }
          }
        } else {
          // 工具不属于任何 step，作为独立工具添加
          if (toolCallId != null) {
            const last = list[list.length - 1];
            if (
              last?.kind === "tool" &&
              (last.data as { tool_call_id?: string }).tool_call_id === toolCallId
            ) {
              // 更新最后一个独立工具(改进B: shell streaming 事件 console 累积)
              list[list.length - 1] = { ...last, data: mergeToolUpdate(last.data, tool) };
              break;
            }
          }
          // 添加新的独立工具
          list.push({
            kind: "tool",
            id: stableId("tool", toolIndex++, (tool.name || "") + (tool.function || "")),
            data: tool,
            timeLabel: getToolTimeLabel(tool),
          });
        }
        break;
      }
      case "title":
      case "plan":
      case "wait":
      case "done":
        break;
      case "error": {
        // 处理错误事件
        const errorData = ev.data as { error?: string; created_at?: number; event_id?: string; [key: string]: unknown };
        if (errorData.error) {
          list.push({
            kind: "error",
            id: stableId("error", errorIndex++, String(list.length)),
            error: errorData.error,
            timestamp: errorData.created_at,
          });
        }
        break;
      }
      default:
        break;
    }
  }

  return list;
}

/**
 * 从事件列表中取最新的 plan 步骤，并合并其后的 step 事件状态（用于底部任务进度面板）
 *
 * 实时更新原理：
 * - plan 事件携带初始步骤列表（通常 status=pending）
 * - 后续 step 事件逐条更新步骤状态（running → completed）
 * - 本函数将两者合并，使进度面板在执行过程中实时更新
 */
export function getLatestPlanFromEvents(events: SSEEventData[]): PlanStep[] {
  let steps: PlanStep[] = [];
  let planIndex = -1;

  // 1.从后往前找到最新的 plan 事件
  for (let i = events.length - 1; i >= 0; i--) {
    const ev = events[i];
    if (ev.type === "plan") {
      const plan = ev.data as PlanEvent;
      if (plan.steps && Array.isArray(plan.steps)) {
        // 深拷贝避免修改原始事件数据
        steps = plan.steps.map((s) => ({ ...s }));
        planIndex = i;
      }
      break;
    }
  }

  if (planIndex < 0 || steps.length === 0) return steps;

  // 2.合并 plan 事件之后的 step 事件，实时更新步骤状态
  for (let i = planIndex + 1; i < events.length; i++) {
    const ev = events[i];
    if (ev.type !== "step") continue;
    const step = ev.data as StepEvent;
    const idx = steps.findIndex((s) => s.id === step.id);
    if (idx >= 0) {
      steps[idx] = {
        ...steps[idx],
        status: step.status,
        description: step.description || steps[idx].description,
      };
    }
  }

  return steps;
}
