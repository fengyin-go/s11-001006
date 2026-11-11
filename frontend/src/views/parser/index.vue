<template>
  <section class="page" data-module="parser">
    <header class="page-head">
      <div>
        <h2>内容解析器管理</h2>
        <p class="page-desc">维护内容解析模板；可把同一份解析样本送往采集校验页试验面板，交接过程只读模板、不改模板内容。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记解析模板</button>
        <button class="btn" type="button" @click="exportRows">导出内容解析器清单</button>
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
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="fillHandoff(row)">填入送样面板</button>
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
          <td :colspan="columns.length + 1" class="empty-state">暂无内容解析器数据，可先登记解析模板</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条内容解析器记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section class="trial-panel" data-testid="parser-handoff-panel">
      <header class="trial-head">
        <div>
          <h3>解析样本送校验试验</h3>
          <p class="page-desc">
            同一份样本先经后端交接校验，再带到采集校验页离线试验；模板与正式记录均保持只读。
          </p>
        </div>
      </header>
      <div class="trial-grid">
        <form class="trial-form" @submit.prevent="handoff">
          <label class="filter-item">
            <span>来源解析模板</span>
            <select v-model="handoffForm.parser_template_id">
              <option :value="null">不关联模板（仅交接样本）</option>
              <option v-for="row in rows" :key="String(row.id)" :value="Number(row.id)">
                {{ row['模板编号'] }} · {{ row['目标字段名'] }}
              </option>
            </select>
          </label>
          <label class="filter-item">
            <span>解析样本（JSON 对象）</span>
            <textarea
              v-model="handoffForm.sampleText"
              rows="8"
              :class="{ invalid: sampleParseError }"
              placeholder='{"price": "10.5", "title": "采集结果 A"}'
            ></textarea>
            <small v-if="sampleParseError" class="error-text">{{ sampleParseError }}</small>
          </label>
          <div class="trial-inline">
            <button class="btn primary" type="submit" :disabled="sending">{{ sending ? '交接中…' : '送往采集校验试验面板' }}</button>
            <button class="btn" type="button" @click="loadExample">填入示例样本</button>
          </div>
          <p v-if="lastHandoff" class="capability-note">
            后端已确认交接：{{ lastHandoff.message }}（来源标记 {{ lastHandoff.sample_source }}）
          </p>
        </form>
      </div>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { request } from '@/api/client'
import { handoffParserSample, stashParserHandoff, type ParserHandoff } from '@/api/trial'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/parser'
const columns = ['模板编号', '目标字段名', 'XPath表达式', '正则模式', '默认取值', '字段类型', '必填标识', '清洗函数']
const actions = ['提交校验', '启用模板', '停用模板']
const stats = [{ label: '解析模板总数', value: 0 }, { label: '已启用模板', value: 0 }, { label: '待校验模板', value: 0 }]

const router = useRouter()
const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const sending = ref(false)
const sampleParseError = ref('')
const lastHandoff = ref<ParserHandoff | null>(null)

const handoffForm = reactive({
  parser_template_id: null as number | null,
  sampleText: '{\n  "price": "10.5",\n  "title": "采集结果 A"\n}',
})

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '解析模板登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('内容解析器动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '内容解析器操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('解析模板列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '内容解析器列表读取失败'
  }
}

function fillHandoff(row: Row) {
  handoffForm.parser_template_id = Number(row.id)
}

function loadExample() {
  handoffForm.sampleText = '{\n  "price": "10.5",\n  "title": "采集结果 A"\n}'
  sampleParseError.value = ''
}

async function handoff() {
  let sample: Record<string, unknown>
  try {
    const parsed: unknown = JSON.parse(handoffForm.sampleText)
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      sampleParseError.value = '样本必须是 JSON 对象（字段名到取值）'
      return
    }
    sample = parsed as Record<string, unknown>
    sampleParseError.value = ''
  } catch (error) {
    sampleParseError.value = error instanceof Error ? `样本 JSON 不合法：${error.message}` : '样本 JSON 不合法'
    return
  }

  sending.value = true
  errorMessage.value = ''
  try {
    const confirmed = await handoffParserSample(sample, handoffForm.parser_template_id)
    lastHandoff.value = confirmed
    // 同一份样本经会话存储交接给校验页；不进 URL/日志，也不进任何共享缓存。
    stashParserHandoff(confirmed)
    await router.push({ path: '/validate' })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '解析样本交接失败'
  } finally {
    sending.value = false
  }
}

onMounted(reload)
</script>
