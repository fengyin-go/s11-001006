<template>
  <section class="page" data-module="validate">
    <header class="page-head">
      <div>
        <h2>采集校验管理</h2>
        <p class="page-desc">维护采集校验规则的输入、执行约束与结果记录；离线试验面板可反复运行样本，不改动任何正式记录。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记校验规则</button>
        <button class="btn" type="button" @click="exportRows">导出采集校验清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>规则版本</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>v{{ row['规则版本'] ?? 1 }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="loadIntoTrial(row)">送试验</button>
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无采集校验数据，可先登记校验规则</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集校验记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section class="trial-panel" data-testid="trial-panel">
      <header class="trial-head">
        <div>
          <h3>离线样本试验面板</h3>
          <p class="page-desc">
            只读正式规则与解析模板；相同表达式与版本复用编译结果，规则改版后旧编译不会混入新结论。
            <span v-if="handoffNotice" class="handoff-note">{{ handoffNotice }}</span>
          </p>
        </div>
        <div class="trial-head-actions">
          <button class="btn ghost" type="button" @click="refreshCache">刷新缓存计数</button>
          <button class="btn ghost" type="button" @click="onClearCache">清空编译缓存</button>
          <button class="btn ghost" type="button" @click="onClearHistory">清空试验历史</button>
        </div>
      </header>

      <div class="trial-grid">
        <form class="trial-form" @submit.prevent="submitTrial">
          <label class="filter-item">
            <span>关联规则（可空，直接粘贴表达式试验）</span>
            <select v-model="trialForm.rule_id">
              <option :value="null">不关联正式规则</option>
              <option v-for="row in rows" :key="String(row.id)" :value="Number(row.id)">
                {{ row['规则编号'] }} · v{{ row['规则版本'] ?? 1 }}
              </option>
            </select>
          </label>
          <label class="filter-item">
            <span>校验表达式（受限语法，不能执行任意 Python）</span>
            <textarea v-model="trialForm.expression" rows="3" placeholder="例如 number(value) &gt;= 10"></textarea>
          </label>
          <div class="trial-inline">
            <label class="filter-item">
              <span>规则版本</span>
              <input v-model.number="trialForm.rule_version" type="number" min="1" placeholder="留空取登记版本" />
            </label>
            <label class="filter-item">
              <span>期望取值</span>
              <input v-model="trialForm.expected" placeholder="留空取登记值" />
            </label>
            <label class="filter-item">
              <span>容差范围（数值 / ±x / 5%）</span>
              <input v-model="trialForm.tolerance" placeholder="留空取登记值" />
            </label>
          </div>
          <label class="filter-item">
            <span>解析样本（JSON 对象，字段名到取值；不会进入共享缓存）</span>
            <textarea
              v-model="trialForm.sampleText"
              rows="6"
              :class="{ invalid: sampleParseError }"
              placeholder='{"price": "10.5", "batch": "A"}'
            ></textarea>
            <small v-if="sampleParseError" class="error-text">{{ sampleParseError }}</small>
          </label>
          <div class="trial-inline">
            <button class="btn primary" type="submit" :disabled="running">{{ running ? '试验中…' : '运行试验' }}</button>
            <button class="btn" type="button" @click="loadExampleSample">填入示例样本</button>
            <span v-if="trialForm.parser_template_id" class="handoff-note">
              来源解析模板 ID：{{ trialForm.parser_template_id }}
            </span>
          </div>
          <p class="capability-note">
            可用能力：+ - * / // % **、比较与 and/or/not、in、三元表达式、字典/列表下标、
            len/abs/round/min/max/sum/number/is_none/is_blank、字符串白名单方法与 math.floor/ceil/sqrt。
            禁止 import、赋值、lambda/推导式、dunder 属性与任何白名单外函数。
          </p>
        </form>

        <div class="trial-side">
          <article class="cache-card" data-testid="cache-card">
            <h4>编译缓存计数</h4>
            <ul class="cache-list">
              <li><span>容量 / 当前条目</span><strong>{{ cacheStats.capacity }} / {{ cacheStats.size }}</strong></li>
              <li><span>命中 / 未命中</span><strong>{{ cacheStats.hits }} / {{ cacheStats.misses }}</strong></li>
              <li><span>负缓存命中（不合法表达式）</span><strong>{{ cacheStats.negative_hits }}</strong></li>
              <li><span>LRU 淘汰次数</span><strong>{{ cacheStats.evictions }}</strong></li>
              <li><span>规则语言版本</span><strong>v{{ cacheStats.lang_version }}</strong></li>
            </ul>
          </article>

          <div v-if="latestResult" class="result-card" :class="latestResult.passed ? 'pass' : 'fail'">
            <header class="result-head">
              <strong>{{ latestResult.passed ? '试验通过' : '试验未通过' }}</strong>
              <span class="cache-tag" :class="latestResult.cache_hit ? 'hot' : 'cold'">
                {{ latestResult.cache_hit ? '热运行·命中编译缓存' : '冷启动·新编译' }}
              </span>
            </header>
            <p class="page-desc">
              规则 v{{ latestResult.规则版本 }} · 语言 v{{ latestResult.lang_version }} ·
              缓存键 {{ latestResult.cache_key.slice(0, 12) }}…
            </p>
            <p v-if="!latestResult.compile_ok" class="error-text">
              历史不合法表达式已兼容：仅记录失败解释，正式记录未改动。
            </p>
            <table class="data-table result-table">
              <thead>
                <tr><th>字段</th><th>阶段</th><th>结论</th><th>解释</th></tr>
              </thead>
              <tbody>
                <tr v-for="(item, index) in latestResult.field_results" :key="index">
                  <td>{{ item.field }}</td>
                  <td>{{ item.stage }}</td>
                  <td :class="item.passed ? 'pass-text' : 'fail-text'">{{ item.passed ? '通过' : '失败' }}</td>
                  <td>{{ item.reason }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <section class="history-block">
        <header class="trial-head">
          <h4>试验历史（本进程最近 {{ history.length }} 条，独立于正式校验记录）</h4>
        </header>
        <table class="data-table">
          <thead>
            <tr>
              <th>时间</th><th>规则版本</th><th>表达式</th><th>来源</th><th>缓存</th><th>结论</th><th>失败解释</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in history" :key="item.trial_id">
              <td>{{ item.created_at }}</td>
              <td>v{{ item.规则版本 }}</td>
              <td class="expr-cell">{{ item.expression }}</td>
              <td>{{ item.source_note }}</td>
              <td>{{ item.cache_hit ? '命中' : '新编译' }}</td>
              <td :class="item.passed ? 'pass-text' : 'fail-text'">{{ item.passed ? '通过' : '未通过' }}</td>
              <td>{{ item.failure_reasons.join('；') || '—' }}</td>
            </tr>
            <tr v-if="!history.length">
              <td colspan="7" class="empty-state">暂无试验记录</td>
            </tr>
          </tbody>
        </table>
      </section>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'
import {
  clearCompileCache,
  clearTrialHistory,
  consumeParserHandoff,
  fetchCacheStats,
  fetchTrialHistory,
  runTrial,
  type TrialResult,
} from '@/api/trial'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/validate'
const columns = ['规则编号', '校验目标字段', '校验表达式', '期望取值', '容差范围', '采样比例', '阻断级别', '告警触发']
const actions = ['提交校验', '启用规则', '停用规则']
const stats = [{ label: '校验规则总数', value: 0 }, { label: '已启用规则', value: 0 }, { label: '待校验规则', value: 0 }]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const running = ref(false)
const latestResult = ref<TrialResult | null>(null)
const history = ref<TrialResult[]>([])
const handoffNotice = ref('')
const sampleParseError = ref('')
const cacheStats = reactive({
  capacity: 0,
  size: 0,
  hits: 0,
  misses: 0,
  negative_hits: 0,
  evictions: 0,
  lang_version: 1,
  hit_rate: 0,
})

const trialForm = reactive({
  rule_id: null as number | null,
  expression: '',
  rule_version: null as number | null,
  expected: '',
  tolerance: '',
  sampleText: '{\n  "price": "10.5",\n  "batch": "A"\n}',
  sample_source: 'validate',
  parser_template_id: null as number | null,
})

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '校验规则登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('采集校验动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集校验操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('校验规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集校验列表读取失败'
  }
}

function loadIntoTrial(row: Row) {
  trialForm.rule_id = Number(row.id)
  trialForm.expression = String(row['校验表达式'] ?? '')
  trialForm.rule_version = row['规则版本'] != null ? Number(row['规则版本']) : null
  trialForm.expected = String(row['期望取值'] ?? '')
  trialForm.tolerance = String(row['容差范围'] ?? '')
  trialForm.sample_source = 'validate'
  trialForm.parser_template_id = null
  handoffNotice.value = ''
}

function loadExampleSample() {
  trialForm.sampleText = '{\n  "price": "10.5",\n  "batch": "A"\n}'
  sampleParseError.value = ''
}

function parseSample(): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(trialForm.sampleText)
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      sampleParseError.value = '样本必须是 JSON 对象（字段名到取值）'
      return null
    }
    sampleParseError.value = ''
    return parsed as Record<string, unknown>
  } catch (error) {
    sampleParseError.value = error instanceof Error ? `样本 JSON 不合法：${error.message}` : '样本 JSON 不合法'
    return null
  }
}

async function submitTrial() {
  const sample = parseSample()
  if (!sample) {
    return
  }
  running.value = true
  errorMessage.value = ''
  try {
    latestResult.value = await runTrial({
      rule_id: trialForm.rule_id,
      sample,
      expression: trialForm.expression || undefined,
      rule_version: trialForm.rule_version,
      expected: trialForm.expected === '' ? undefined : trialForm.expected,
      tolerance: trialForm.tolerance === '' ? undefined : trialForm.tolerance,
      sample_source: trialForm.sample_source,
      parser_template_id: trialForm.parser_template_id,
    })
    await refreshCache()
    await loadHistory()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '离线试验失败'
  } finally {
    running.value = false
  }
}

async function refreshCache() {
  try {
    Object.assign(cacheStats, await fetchCacheStats())
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '缓存状态读取失败'
  }
}

async function onClearCache() {
  Object.assign(cacheStats, await clearCompileCache())
  errorMessage.value = '编译缓存已清空；下次试验将冷启动并重新编译，结论保持一致'
}

async function onClearHistory() {
  await clearTrialHistory()
  await loadHistory()
}

async function loadHistory() {
  const payload = await fetchTrialHistory()
  history.value = payload.items.slice().reverse()
}

/** 接收内容解析器页通过会话存储交接的同一份解析样本，读取一次即清除。 */
function consumeHandoff() {
  const payload = consumeParserHandoff()
  if (!payload) {
    return
  }
  if (payload.sample) {
    trialForm.sampleText = JSON.stringify(payload.sample, null, 2)
    trialForm.sample_source = payload.sample_source
    trialForm.parser_template_id = payload.parser_template_id ?? null
    handoffNotice.value = `已载入内容解析器页送样（模板：${payload.模板编号 ?? '未关联'}）`
  }
}

onMounted(async () => {
  await reload()
  await refreshCache()
  await loadHistory()
  consumeHandoff()
})
</script>
