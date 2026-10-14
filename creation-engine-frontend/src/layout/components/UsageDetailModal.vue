<script setup>
import { computed, ref, watch } from 'vue'
import { getMyUsageDetailApi, getMyUsageModelsApi } from '@/api'

const props = defineProps({
  open: { type: Boolean, default: false },
})
const emit = defineEmits(['update:open'])

const API_TYPES = [
  { value: '', label: '全部类型' },
  { value: 'llm', label: '文本(LLM)' },
  { value: 'image', label: '图像' },
  { value: 'tts', label: '语音' },
  { value: 'music', label: '音乐' },
]

const loading = ref(false)
const rows = ref([])
const total = ref(0)
// 当前筛选条件下的全量合计(后端聚合,不是本页求和)
const totals = ref({ total_tokens: 0, tts_chars: 0, image_count: 0, music_count: 0 })
const modelOptions = ref([{ value: '', label: '全部模型' }])

const filters = ref({ api_type: '', model: '', range: [] })
const page = ref(1)
const pageSize = ref(20)

const open = computed({
  get: () => props.open,
  set: (v) => emit('update:open', v),
})

const columns = [
  { title: '作品', dataIndex: 'project_name', key: 'project_name', ellipsis: true },
  { title: '环节', dataIndex: 'service', key: 'service' },
  { title: '类型', dataIndex: 'api_type', key: 'api_type' },
  { title: '模型', dataIndex: 'model', key: 'model', ellipsis: true },
  { title: '输入', dataIndex: 'prompt_tokens', key: 'prompt_tokens' },
  { title: '输出', dataIndex: 'completion_tokens', key: 'completion_tokens' },
  { title: '合计', dataIndex: 'total_tokens', key: 'total_tokens' },
  { title: '图片', dataIndex: 'image_count', key: 'image_count' },
  { title: 'TTS字符', dataIndex: 'tts_chars', key: 'tts_chars' },
  { title: '时间', dataIndex: 'created_at', key: 'created_at' },
]

function fmtTime(s) {
  return s ? s.replace('T', ' ').slice(0, 19) : ''
}

function fmtNum(n) {
  return (n ?? 0).toLocaleString('zh-CN')
}

/** 底部合计:纯展示,不可点击 */
const summaryStats = computed(() => [
  { label: '总 Token', value: fmtNum(totals.value.total_tokens) },
  { label: 'TTS 字数', value: fmtNum(totals.value.tts_chars) },
  { label: '图片', value: fmtNum(totals.value.image_count) },
  { label: '音乐', value: fmtNum(totals.value.music_count) },
])

async function loadModels() {
  try {
    const list = await getMyUsageModelsApi()
    modelOptions.value = [{ value: '', label: '全部模型' }, ...list.map((m) => ({ value: m, label: m }))]
  } catch {
    modelOptions.value = [{ value: '', label: '全部模型' }]
  }
}

async function fetchDetail() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filters.value.api_type) params.api_type = filters.value.api_type
    if (filters.value.model) params.model = filters.value.model
    if (filters.value.range?.length === 2) {
      // 库里 created_at 是本地时间(无时区),按本地朴素时间发送,避免 UTC 偏移 8 小时
      params.start_time = filters.value.range[0].startOf('day').format('YYYY-MM-DD HH:mm:ss')
      params.end_time = filters.value.range[1].endOf('day').format('YYYY-MM-DD HH:mm:ss')
    }
    const res = await getMyUsageDetailApi(params)
    rows.value = res.items || []
    total.value = res.total || 0
    totals.value = res.totals || { total_tokens: 0, tts_chars: 0, image_count: 0, music_count: 0 }
  } finally {
    loading.value = false
  }
}

function applyFilters() {
  page.value = 1
  fetchDetail()
}

function onPageChange(p, ps) {
  page.value = p
  pageSize.value = ps
  fetchDetail()
}

watch(
  () => props.open,
  (v) => {
    if (v) {
      page.value = 1
      loadModels()
      fetchDetail()
    }
  }
)
</script>

<template>
  <a-modal v-model:open="open" title="Token 账单明细" :footer="null" width="1000px" wrap-class-name="usage-detail-modal">
    <div class="usage-filters">
      <a-select v-model:value="filters.api_type" style="width: 150px" :options="API_TYPES" @change="applyFilters" />
      <a-select v-model:value="filters.model" style="width: 200px" :options="modelOptions" @change="applyFilters" />
      <a-range-picker v-model:value="filters.range" @change="applyFilters" />
    </div>

    <a-table
      :data-source="rows"
      :columns="columns"
      :loading="loading"
      row-key="task_id"
      size="small"
      :pagination="false"
      :scroll="{ y: 420 }"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'created_at'">{{ fmtTime(record.created_at) }}</template>
      </template>
    </a-table>

    <div class="usage-footer">
      <!-- 当前筛选下的全量合计,纯展示 -->
      <div class="usage-totals">
        <span v-for="s in summaryStats" :key="s.label" class="usage-total-item">
          <span class="usage-total-label">{{ s.label }}</span>
          <span class="usage-total-value">{{ s.value }}</span>
        </span>
      </div>
      <a-pagination
        :current="page"
        :page-size="pageSize"
        :total="total"
        :show-total="(t) => `共 ${t} 条`"
        show-size-changer
        size="small"
        @change="onPageChange"
      />
    </div>
  </a-modal>
</template>

<style scoped>
.usage-filters {
  display: flex;
  gap: 10px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
/* 底部一行:左侧合计,右侧分页 */
.usage-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-top: 12px;
  flex-wrap: wrap;
}

/* 合计与账户面板的信息行同款(#1f222d 底 / 13px),不抢表格视线 */
.usage-totals {
  display: flex;
  align-items: center;
  gap: 18px;
  padding: 7px 14px;
  border-radius: 8px;
  background: #1f222d;
  font-size: 13px;
  cursor: default;
}
.usage-total-item {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
}
.usage-total-label {
  color: #9ca3af;
}
.usage-total-value {
  color: #e5e7eb;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}
</style>
