<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import dayjs from 'dayjs'
import { getUsageListApi, getUsageSummaryApi } from '@/api'
import GroupChart from './components/GroupChart.vue'
import GroupDetailChart from './components/GroupDetailChart.vue'

const loading = ref(false)
const dataSource = ref([])
const total = ref(0)
const summaryLoading = ref(false)
const summaryRows = ref([])

const filters = reactive({
  group_name: '',
  task_id: '',
  service: '',
  model: '',
  api_type: undefined,
  timeRange: [],
})

const pagination = reactive({
  current: 1,
  pageSize: 20,
})

const summaryColumns = [
  { title: '管线名', dataIndex: 'group_name', key: 'group_name', width: 120 },
  { title: '任务数', dataIndex: 'task_count', key: 'task_count', width: 80 },
  { title: '调用次数', dataIndex: 'call_count', key: 'call_count', width: 90 },
  { title: 'total_tokens', dataIndex: 'total_tokens', key: 'total_tokens', width: 120 },
  { title: '图片数', dataIndex: 'image_count', key: 'image_count', width: 80 },
  { title: 'TTS字数', dataIndex: 'tts_chars', key: 'tts_chars', width: 100 },
  { title: '音乐数', dataIndex: 'music_count', key: 'music_count', width: 80 },
]

const columns = [
  { title: '任务ID', dataIndex: 'task_id', key: 'task_id', width: 200, ellipsis: true },
  { title: '管线名', dataIndex: 'group_name', key: 'group_name', width: 110 },
  { title: '服务', dataIndex: 'service', key: 'service', width: 130 },
  { title: '类型', dataIndex: 'api_type', key: 'api_type', width: 90 },
  { title: '模型', dataIndex: 'model', key: 'model', width: 140 },
  { title: 'prompt_tokens', dataIndex: 'prompt_tokens', key: 'prompt_tokens', width: 110 },
  { title: 'completion_tokens', dataIndex: 'completion_tokens', key: 'completion_tokens', width: 130 },
  { title: 'total_tokens', dataIndex: 'total_tokens', key: 'total_tokens', width: 110 },
  { title: '图片数', dataIndex: 'image_count', key: 'image_count', width: 80 },
  { title: 'TTS字数', dataIndex: 'tts_chars', key: 'tts_chars', width: 90 },
  { title: '调用次数', dataIndex: 'call_count', key: 'call_count', width: 90 },
  { title: '时间', dataIndex: 'created_at', key: 'created_at', width: 170 },
]

/** 拉取用量明细列表 */
async function fetchList() {
  loading.value = true
  try {
    const [start_time, end_time] = filters.timeRange || []
    const res = await getUsageListApi({
      group_name: filters.group_name || undefined,
      task_id: filters.task_id || undefined,
      service: filters.service || undefined,
      model: filters.model || undefined,
      api_type: filters.api_type,
      start_time: start_time ? dayjs(start_time).format('YYYY-MM-DD HH:mm:ss') : undefined,
      end_time: end_time ? dayjs(end_time).format('YYYY-MM-DD HH:mm:ss') : undefined,
      page: pagination.current,
      page_size: pagination.pageSize,
    })
    dataSource.value = res.items
    total.value = res.total
  } finally {
    loading.value = false
  }
}

/** 拉取按管线汇总(汇总接口是管线名精确筛选,输入的模糊管线名不传,只跟时间范围联动) */
async function fetchSummary() {
  summaryLoading.value = true
  try {
    const [start_time, end_time] = filters.timeRange || []
    summaryRows.value = await getUsageSummaryApi({
      start_time: start_time ? dayjs(start_time).format('YYYY-MM-DD HH:mm:ss') : undefined,
      end_time: end_time ? dayjs(end_time).format('YYYY-MM-DD HH:mm:ss') : undefined,
    })
  } finally {
    summaryLoading.value = false
  }
}

function handleSearch() {
  pagination.current = 1
  fetchList()
  fetchSummary()
}

function handleReset() {
  filters.group_name = ''
  filters.task_id = ''
  filters.service = ''
  filters.model = ''
  filters.api_type = undefined
  filters.timeRange = []
  handleSearch()
}

function handleTableChange(pag) {
  pagination.current = pag.current
  pagination.pageSize = pag.pageSize
  fetchList()
}

// ==================== 管线专属弹层:点击饼片打开,四指标图 ====================
const detailOpen = ref(false)
const detailGroup = ref('')
const detailRow = computed(
  () => summaryRows.value.find((r) => r.group_name === detailGroup.value) || null,
)

function handleGroupClick(groupName) {
  detailGroup.value = groupName
  detailOpen.value = true
}

onMounted(() => {
  fetchList()
  fetchSummary()
})
</script>

<template>
  <div class="usage-list">
    <div class="dr-page-head">
      <div>
        <div class="dr-page-title">Token 用量</div>
        <div class="dr-page-desc">按管线汇总各模型消耗 · 点饼片看该管线专属指标</div>
      </div>
    </div>

    <a-card :bordered="false" class="usage-list__filter">
      <a-space wrap>
        <a-input
          v-model:value="filters.group_name"
          placeholder="管线名模糊查询"
          style="width: 160px"
          allow-clear
        />
        <a-input
          v-model:value="filters.task_id"
          placeholder="任务ID模糊查询"
          style="width: 200px"
          allow-clear
        />
        <a-input
          v-model:value="filters.service"
          placeholder="服务模糊查询"
          style="width: 150px"
          allow-clear
        />
        <a-input
          v-model:value="filters.model"
          placeholder="模型模糊查询"
          style="width: 150px"
          allow-clear
        />
        <a-select
          v-model:value="filters.api_type"
          placeholder="类型"
          style="width: 110px"
          allow-clear
        >
          <a-select-option value="llm">llm</a-select-option>
          <a-select-option value="image">image</a-select-option>
          <a-select-option value="tts">tts</a-select-option>
          <a-select-option value="music">music</a-select-option>
        </a-select>
        <a-range-picker v-model:value="filters.timeRange" show-time />
        <a-button type="primary" @click="handleSearch">查询</a-button>
        <a-button @click="handleReset">重置</a-button>
      </a-space>
    </a-card>

    <a-card :bordered="false" title="按管线汇总" class="usage-list__summary">
      <a-spin :spinning="summaryLoading">
        <GroupChart
          v-if="summaryRows.length"
          :rows="summaryRows"
          @group-click="handleGroupClick"
        />
      </a-spin>
      <a-table
        :columns="summaryColumns"
        :data-source="summaryRows"
        :loading="summaryLoading"
        row-key="group_name"
        size="small"
        :pagination="false"
        :scroll="{ x: 760 }"
        :custom-row="(record) => ({
          onClick: () => record.group_name && handleGroupClick(record.group_name),
          style: { cursor: 'pointer' },
        })"
      />
    </a-card>

    <a-card :bordered="false" class="usage-list__table">
      <a-table
        :columns="columns"
        :data-source="dataSource"
        :loading="loading"
        row-key="id"
        :scroll="{ x: 1300 }"
        :pagination="{
          current: pagination.current,
          pageSize: pagination.pageSize,
          total,
          showSizeChanger: true,
          showTotal: (t) => `共 ${t} 条`,
        }"
        @change="handleTableChange"
      />
    </a-card>

    <a-modal
      v-model:open="detailOpen"
      :title="`管线专属:${detailGroup}`"
      width="720px"
      :footer="null"
    >
      <template v-if="detailRow">
        <a-row :gutter="16" class="usage-list__detail-stats">
          <a-col :span="6">
            <a-statistic title="total_tokens" :value="detailRow.total_tokens" />
          </a-col>
          <a-col :span="6">
            <a-statistic title="图片数" :value="detailRow.image_count" />
          </a-col>
          <a-col :span="6">
            <a-statistic title="TTS字数" :value="detailRow.tts_chars" />
          </a-col>
          <a-col :span="6">
            <a-statistic title="音乐数" :value="detailRow.music_count" />
          </a-col>
        </a-row>
        <GroupDetailChart :row="detailRow" :all-rows="summaryRows" />
      </template>
      <a-empty v-else description="该管线暂无用量数据" />
    </a-modal>
  </div>
</template>

<style scoped>
.usage-list__filter {
  margin-bottom: 16px;
}

.usage-list__summary {
  margin-bottom: 16px;
}

.usage-list__detail-stats {
  margin: 8px 0 4px;
  padding: 14px 16px;
  background: rgba(232, 182, 76, 0.06);
  border: 1px solid var(--dr-border);
  border-radius: 10px;
}
</style>
