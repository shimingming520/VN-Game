<script setup>
import { ref, reactive, onMounted } from 'vue'
import dayjs from 'dayjs'
import { getTaskListApi } from '@/api'
import PublishModal from './components/PublishModal.vue'

const STATUS_MAP = {
  0: { text: '待处理', color: 'default' },
  1: { text: '处理中', color: 'processing' },
  2: { text: '已完成', color: 'success' },
  3: { text: '失败', color: 'error' },
}

/** 7 阶段执行顺序 + 中文名(与创作侧管线一致) */
const STAGE_FIELDS = [
  ['story_planner_status', '文本处理'],
  ['info_extractor_status', '信息提取'],
  ['char_designer_status', '角色设计'],
  ['scene_painter_status', '场景绘制'],
  ['audio_producer_status', '配音生成'],
  ['sound_arranger_status', '音效编排'],
  ['game_builder_status', '游戏打包'],
]

/**
 * 状态列文案:处理中时细化到当前阶段,如"处理中·游戏打包"
 * 优先找状态=1(处理中)的阶段,否则取第一个未完成(≠2)的阶段
 */
function statusText(record) {
  const base = STATUS_MAP[record.overall_status]?.text ?? String(record.overall_status)
  if (record.overall_status !== 1) return base
  const running = STAGE_FIELDS.find(([field]) => record[field] === 1)
  const stage = running || STAGE_FIELDS.find(([field]) => record[field] !== 2)
  return stage ? `${base}·${stage[1]}` : base
}

/** 将接口 ISO 时间收敛到秒，隐藏 T 与微秒尾数。 */
function formatTaskTime(value) {
  if (!value) return '—'
  const normalized = String(value).replace('T', ' ')
  const isoToSeconds = normalized.match(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/)?.[0]
  if (isoToSeconds) return isoToSeconds
  const parsed = dayjs(value)
  return parsed.isValid() ? parsed.format('YYYY-MM-DD HH:mm:ss') : normalized
}

const loading = ref(false)
const dataSource = ref([])
const total = ref(0)

const filters = reactive({
  group_name: '',
  project_name: '',
  task_id: '',
  overall_status: undefined,
  timeRange: [],
})

const pagination = reactive({
  current: 1,
  pageSize: 20,
})

const columns = [
  { title: '任务ID', dataIndex: 'task_id', key: 'task_id', width: 200, ellipsis: true },
  { title: '项目名', dataIndex: 'project_name', key: 'project_name', width: 160 },
  { title: '管线名', dataIndex: 'group_name', key: 'group_name', width: 110 },
  { title: '状态', dataIndex: 'overall_status', key: 'overall_status', width: 140 },
  { title: '进度', dataIndex: 'progress_percentage', key: 'progress_percentage', width: 90 },
  { title: '创建时间', dataIndex: 'create_time', key: 'create_time', width: 170 },
  { title: '更新时间', dataIndex: 'update_time', key: 'update_time', width: 170 },
  { title: '操作', key: 'action', width: 100, fixed: 'right' },
]

// 发布弹窗
const publishOpen = ref(false)
const publishTaskId = ref('')

function openPublish(record) {
  publishTaskId.value = record.task_id
  publishOpen.value = true
}

/** 拉取任务列表 */
async function fetchList() {
  loading.value = true
  try {
    const [start_time, end_time] = filters.timeRange || []
    const res = await getTaskListApi({
      group_name: filters.group_name || undefined,
      project_name: filters.project_name || undefined,
      task_id: filters.task_id || undefined,
      overall_status: filters.overall_status,
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

function handleSearch() {
  pagination.current = 1
  fetchList()
}

function handleReset() {
  filters.group_name = ''
  filters.project_name = ''
  filters.task_id = ''
  filters.overall_status = undefined
  filters.timeRange = []
  handleSearch()
}

function handleTableChange(pag) {
  pagination.current = pag.current
  pagination.pageSize = pag.pageSize
  fetchList()
}

onMounted(fetchList)
</script>

<template>
  <div class="task-list">
    <div class="dr-page-head">
      <div>
        <div class="dr-page-title">任务日志</div>
        <div class="dr-page-desc">各管线任务执行轨迹 · 七阶段状态实时回传</div>
      </div>
    </div>

    <a-card :bordered="false" class="task-list__filter">
      <a-space wrap>
        <a-input
          v-model:value="filters.group_name"
          placeholder="管线名模糊查询"
          style="width: 160px"
          allow-clear
        />
        <a-input
          v-model:value="filters.project_name"
          placeholder="项目名模糊查询"
          style="width: 160px"
          allow-clear
        />
        <a-input
          v-model:value="filters.task_id"
          placeholder="任务ID模糊查询"
          style="width: 200px"
          allow-clear
        />
        <a-select
          v-model:value="filters.overall_status"
          placeholder="整体状态"
          style="width: 120px"
          allow-clear
        >
          <a-select-option :value="0">待处理</a-select-option>
          <a-select-option :value="1">处理中</a-select-option>
          <a-select-option :value="2">已完成</a-select-option>
          <a-select-option :value="3">失败</a-select-option>
        </a-select>
        <a-range-picker v-model:value="filters.timeRange" show-time />
        <a-button type="primary" @click="handleSearch">查询</a-button>
        <a-button @click="handleReset">重置</a-button>
      </a-space>
    </a-card>

    <a-card :bordered="false" class="task-list__table">
      <a-table
        :columns="columns"
        :data-source="dataSource"
        :loading="loading"
        row-key="task_id"
        :pagination="{
          current: pagination.current,
          pageSize: pagination.pageSize,
          total,
          showSizeChanger: true,
          showTotal: (t) => `共 ${t} 条`,
        }"
        @change="handleTableChange"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'overall_status'">
            <a-tag :color="STATUS_MAP[record.overall_status]?.color">
              {{ statusText(record) }}
            </a-tag>
          </template>
          <template v-else-if="column.key === 'progress_percentage'">
            {{ record.progress_percentage }}%
          </template>
          <template v-else-if="column.key === 'create_time' || column.key === 'update_time'">
            {{ formatTaskTime(record[column.dataIndex]) }}
          </template>
          <template v-else-if="column.key === 'action'">
            <a-tooltip :title="record.overall_status === 2 ? '' : '任务完成后才能发布'">
              <a-button
                type="link"
                size="small"
                :disabled="record.overall_status !== 2"
                @click="openPublish(record)"
              >
                发布
              </a-button>
            </a-tooltip>
          </template>
        </template>
      </a-table>
    </a-card>

    <PublishModal v-model:open="publishOpen" :task-id="publishTaskId" />
  </div>
</template>

<style scoped>
.task-list__filter {
  margin-bottom: 16px;
}
</style>
