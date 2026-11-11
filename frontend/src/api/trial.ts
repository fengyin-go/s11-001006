/** 离线样本试验相关接口封装。 */
import { request } from './client'

export interface FieldResult {
  field: string
  stage: string
  passed: boolean
  actual?: unknown
  expected?: unknown
  reason: string
}

export interface TrialResult {
  trial_id: string
  created_at: string
  rule_id: number | null
  规则编号: string | null
  规则版本: number
  lang_version: number
  expression: string
  target_field: string
  sample_source: string
  source_note: string
  sample: Record<string, unknown>
  compile_ok: boolean
  cache_hit: boolean
  cache_key: string
  passed: boolean
  actual: unknown
  expected: unknown
  field_results: FieldResult[]
  failure_reasons: string[]
  cache: {
    capacity: number
    size: number
    hits: number
    misses: number
    negative_hits: number
    evictions: number
    lang_version: number
    hit_rate: number
  }
}

export interface TrialPayload {
  rule_id?: number | null
  sample: Record<string, unknown>
  expression?: string
  rule_version?: number | null
  expected?: unknown
  tolerance?: string | null
  sample_source?: string
  parser_template_id?: number | null
}

export async function runTrial(payload: TrialPayload): Promise<TrialResult> {
  const response = await request('/api/validate/trials', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail.detail ?? '试验请求被后端拒绝')
  }
  return (await response.json()) as TrialResult
}

export async function fetchTrialHistory(): Promise<{ total: number; items: TrialResult[] }> {
  const response = await request('/api/validate/trials/history')
  if (!response.ok) {
    throw new Error('试验历史读取失败')
  }
  return response.json()
}

export async function clearTrialHistory(): Promise<void> {
  await request('/api/validate/trials/history', { method: 'DELETE' })
}

export async function fetchCacheStats(): Promise<TrialResult['cache']> {
  const response = await request('/api/validate/cache')
  if (!response.ok) {
    throw new Error('编译缓存状态读取失败')
  }
  return response.json()
}

export async function clearCompileCache(): Promise<TrialResult['cache']> {
  const response = await request('/api/validate/cache', { method: 'DELETE' })
  if (!response.ok) {
    throw new Error('编译缓存清空失败')
  }
  return response.json()
}

export interface ParserHandoff {
  ok: boolean
  message: string
  sample_source: string
  parser_template_id: number | null
  模板编号: string | null
  目标字段名: string | null
  sample: Record<string, unknown>
}

export async function handoffParserSample(
  sample: Record<string, unknown>,
  parserTemplateId: number | null,
  targetField?: string | null,
): Promise<ParserHandoff> {
  const response = await request('/api/parser/sample-handoff', {
    method: 'POST',
    body: JSON.stringify({
      sample,
      parser_template_id: parserTemplateId,
      target_field: targetField ?? null,
    }),
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail.detail ?? '解析样本交接失败')
  }
  return response.json()
}

/** 交接样本只存浏览器会话存储，不进 URL/服务端日志，校验页读取一次后立即清除。 */
export const PARSER_HANDOFF_KEY = 'parser-trial-handoff'

export function stashParserHandoff(payload: ParserHandoff): void {
  sessionStorage.setItem(PARSER_HANDOFF_KEY, JSON.stringify(payload))
}

export function consumeParserHandoff(): ParserHandoff | null {
  const raw = sessionStorage.getItem(PARSER_HANDOFF_KEY)
  if (!raw) {
    return null
  }
  sessionStorage.removeItem(PARSER_HANDOFF_KEY)
  try {
    return JSON.parse(raw) as ParserHandoff
  } catch {
    return null
  }
}
