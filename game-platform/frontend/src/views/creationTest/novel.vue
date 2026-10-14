<script setup>
import { ref, reactive, onMounted, onBeforeUnmount } from 'vue'
import { message, Modal } from 'ant-design-vue'
import {
  getPipelinesApi,
  createNovelsApi,
  getPlatformTaskApi,
  getMyTaskListApi,
  deleteTaskApi,
  restartTaskApi,
  runTaskStageApi,
} from '@/api'
import {
  getCreationTaskRestartPlan,
  RESTART_MODES,
} from '../tasks/restart'

/**
 * 创作测试(视觉小说)
 * 真实创作:提交后由中台按所选管线分发到各组实现,一路跑到打包完成,
 * 会产生真实素材产物与 Token 消耗。
 *
 * 这里只按管线提交(有几条管线就能测几条);效率/均衡/高质量三档模式是给创造平台用的,
 * 中台测试要的是「这条管线本身能不能跑通」,套一层模式映射反而看不清测的是谁。
 */

/** capability → 中文名,顺序即 7 阶段执行顺序 */
const STAGES = [
  ['story_planner', '文本处理'],
  ['info_extractor', '信息提取'],
  ['char_designer', '角色设计'],
  ['scene_painter', '场景绘制'],
  ['audio_producer', '配音生成'],
  ['sound_arranger', '音效编排'],
  ['game_builder', '游戏打包'],
]

const ART_STYLES = [
  ['ink_chinese', '水墨国风'],
  ['cyberpunk', '赛博朋克'],
  ['japanese', '日式动漫'],
  ['dark_european', '暗黑欧式'],
  ['modern_realism', '现代写实'],
]

const LANGUAGES = [
  ['zh-CN', '简体中文'],
  ['en-US', '英语'],
  ['ja-JP', '日语'],
]

const STORY_MODES = [
  ['original', '原文适应'],
  ['multi', '多剧情改写'],
  ['chapter_branch', '章节分支'],
]

const STEP_TAGS = {
  succeeded: ['success', '已完成'],
  running: ['processing', '进行中'],
  publishing: ['processing', '待可靠发布'],
  dispatched: ['processing', '已下发'],
  failed: ['error', '失败'],
  cancelled: ['default', '已取消'],
  pending: ['default', '等待'],
}

const TASK_TAGS = {
  running: ['processing', '进行中'],
  manual_stage: ['processing', '单模块运行'],
  created: ['default', '已创建'],
  succeeded: ['success', '已完成'],
  failed: ['error', '失败'],
  cancelled: ['default', '已取消'],
}

/** novel_tasks.overall_status → 中台任务状态词,两侧口径统一后前端只认一套 */
const OVERALL_STATUS = { 0: 'created', 1: 'running', 2: 'succeeded', 3: 'failed' }

/** novel_tasks 的阶段状态字段,顺序与 STAGES 一一对应 */
const STAGE_STATUS_FIELDS = [
  'story_planner_status',
  'info_extractor_status',
  'char_designer_status',
  'scene_painter_status',
  'audio_producer_status',
  'sound_arranger_status',
  'game_builder_status',
]

const STAGE_STATUS = { 0: 'pending', 1: 'running', 2: 'succeeded', 3: 'failed' }

const pipelines = ref([])
const metaLoading = ref(false)
const fileList = ref([])

const form = reactive({
  pipeline_name: '',
  output_language: 'zh-CN',
  art_style: 'ink_chinese',
  story_mode: 'original',
})

function pipelineLabel(name) {
  const hit = pipelines.value.find((p) => p.name === name)
  return hit ? hit.display_name || hit.name : name
}

async function fetchMeta() {
  metaLoading.value = true
  try {
    pipelines.value = await getPipelinesApi()
  } finally {
    metaLoading.value = false
  }
}

// ==================== 提交与轮询 ====================
const submitting = ref(false)
const BATCH_KEY_STORAGE = 'game-platform:pending-batch-idempotency-key'
const batchIdempotencyKey = ref(
  (() => {
    try {
      return globalThis.sessionStorage?.getItem(BATCH_KEY_STORAGE) || ''
    } catch {
      return ''
    }
  })(),
)
const listLoading = ref(false)
/** 我的任务:{ task_id, project_name, pipeline_name, status, progress, steps } */
const tasks = ref([])
let timer = null

/** 返回 false 阻止 antd 自动上传,文件留到提交时一起发 */
function beforeUpload(file) {
  if (!file.name.toLowerCase().endsWith('.txt')) {
    message.error('仅支持 .txt 文件')
    return false
  }
  if (fileList.value.length >= 50) {
    message.error('单次最多 50 本')
    return false
  }
  fileList.value = [...fileList.value, file]
  return false
}

function removeFile(file) {
  fileList.value = fileList.value.filter((f) => f.uid !== file.uid)
}

async function handleSubmit() {
  if (!fileList.value.length) {
    message.warning('请先选择小说文件')
    return
  }
  if (!form.pipeline_name) {
    message.warning('请选择要测试的管线')
    return
  }
  const data = new FormData()
  for (const file of fileList.value) data.append('novels', file)
  data.append('output_language', form.output_language)
  data.append('art_style', form.art_style)
  data.append('story_mode', form.story_mode)
  data.append('pipeline_name', form.pipeline_name)
  if (!batchIdempotencyKey.value) {
    batchIdempotencyKey.value = globalThis.crypto?.randomUUID
      ? globalThis.crypto.randomUUID()
      : `batch-${Date.now()}-${Math.random().toString(16).slice(2)}`
    try {
      globalThis.sessionStorage?.setItem(BATCH_KEY_STORAGE, batchIdempotencyKey.value)
    } catch {
      // 当前页面内的 ref 仍会在失败重试时复用。
    }
  }
  data.append('batch_id', batchIdempotencyKey.value)

  submitting.value = true
  try {
    const res = await createNovelsApi(data, batchIdempotencyKey.value)
    fileList.value = []
    batchIdempotencyKey.value = ''
    try {
      globalThis.sessionStorage?.removeItem(BATCH_KEY_STORAGE)
    } catch {
      // ignore storage-disabled browsers
    }
    message.success(`已提交 ${(res?.tasks || []).length} 个任务`)
    await loadTasks()
    startPolling()
  } finally {
    submitting.value = false
  }
}

function isFinal(status) {
  return status === 'succeeded' || status === 'failed' || status === 'cancelled'
}

/** novel_tasks 行 → 页面展示结构;steps 先按阶段状态字段兜底,中台详情再覆盖 */
function toRow(item) {
  return {
    task_id: item.task_id,
    project_name: item.project_name || item.novel_filename,
    pipeline_name: item.group_name,
    status: OVERALL_STATUS[item.overall_status] || 'created',
    progress: item.progress_percentage || 0,
    error_msg: item.error_message || '',
    steps: STAGES.map(([capability], i) => ({
      capability,
      impl_id: null,
      status: STAGE_STATUS[item[STAGE_STATUS_FIELDS[i]]] || 'pending',
    })),
  }
}

/**
 * 从服务端拉「我的任务」。任务态存在 novel_tasks 里,所以刷新/换机器都还在;
 * 组件内存里存一份就会像之前那样刷新即丢。
 */
async function loadTasks() {
  listLoading.value = true
  try {
    const res = await getMyTaskListApi({ page: 1, page_size: 20 })
    tasks.value = (res?.items || []).map(toRow)
  } finally {
    listLoading.value = false
  }
  await enrichSteps()
}

/** 已取到中台 steps 的终态任务:详情不会再变,后续轮询不必重查 */
const enrichedFinal = new Set()

/**
 * 中台编排详情:每步用了哪个实现、失败原因。
 * 只有走中台的任务才有,老链路直发 Celery 的任务是 404,静默跳过用兜底状态。
 *
 * 只查还会变的任务。之前每轮对全部 20 个任务并发请求,一轮就打满后端连接池
 * (每个都是同步端点各占一条连接),请求排队直到前端 15s 超时。
 */
async function enrichSteps() {
  const pending = tasks.value.filter(
    (row) => !(isFinal(row.status) && enrichedFinal.has(row.task_id)),
  )
  await Promise.all(
    pending.map(async (row) => {
      try {
        const detail = await getPlatformTaskApi(row.task_id)
        row.status = detail.status
        row.progress = detail.progress
        if (detail.steps?.length) row.steps = detail.steps
        if (isFinal(row.status)) enrichedFinal.add(row.task_id)
      } catch {
        // 查不到就保留 novel_tasks 的阶段状态
      }
    }),
  )
}

async function handleDelete(row) {
  await deleteTaskApi(row.task_id)
  message.success('已取消并移除')
  await loadTasks()
}

// 单行重新启动状态：显示等待反馈，同时避免短时间内重复下发同一个任务。
const restartingTaskId = ref('')
const runningStageKey = ref('')
const restartDialog = reactive({
  open: false,
  row: null,
  mode: RESTART_MODES.RUNNING_AND_FAILED,
})

function restartPlan(row, mode = RESTART_MODES.RUNNING_AND_FAILED) {
  return getCreationTaskRestartPlan(row, mode)
}

function restartDisabled(row) {
  const plan = restartPlan(row)
  return !plan.canRestart || Boolean(
    restartingTaskId.value || runningStageKey.value,
  )
}

function restartTooltip(row) {
  if (!restartPlan(row).canRestart) return '所有模块均已完成，无需重新启动'
  if (restartingTaskId.value || runningStageKey.value) {
    return '请等待当前运行请求完成'
  }
  return '选择仅重启失败模块，或同时重启进行中模块'
}

function restartErrorMessage(error) {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string' && detail) return detail
  return error?.message || '重新启动失败，请稍后重试'
}

async function submitRestart(row) {
  restartingTaskId.value = row.task_id
  try {
    const res = await restartTaskApi(row.task_id, restartDialog.mode)
    enrichedFinal.delete(row.task_id)
    message.success(res?.message || '重新启动请求已提交')
    await loadTasks()
    startPolling()
  } catch (error) {
    // 登录失效由全局拦截器处理，其他错误在当前操作位置给出明确反馈。
    if (error?.response?.status !== 401) {
      message.error(restartErrorMessage(error))
    }
  } finally {
    restartingTaskId.value = ''
  }
}

function handleRestart(row) {
  const plan = restartPlan(row, RESTART_MODES.RUNNING_AND_FAILED)
  if (!plan.canRestart || restartingTaskId.value || runningStageKey.value) return
  restartDialog.row = row
  restartDialog.mode = RESTART_MODES.RUNNING_AND_FAILED
  restartDialog.open = true
}

function closeRestartDialog() {
  if (!restartingTaskId.value) restartDialog.open = false
}

async function confirmRestartDialog() {
  const row = restartDialog.row
  if (!row) return
  const plan = restartPlan(row, restartDialog.mode)
  if (!plan.canRestart) {
    message.warning('当前模式没有可重启模块')
    return
  }
  restartDialog.open = false
  await submitRestart(row)
}

function stageRunKey(row, capability) {
  return `${row.task_id}:${capability}`
}

function stageRunErrorMessage(error) {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string' && detail) return detail
  return error?.message || '单模块运行失败，请稍后重试'
}

async function submitStageRun(row, capability) {
  const key = stageRunKey(row, capability)
  runningStageKey.value = key
  try {
    const res = await runTaskStageApi(row.task_id, capability)
    enrichedFinal.delete(row.task_id)
    const label = STAGES.find(([cap]) => cap === capability)?.[1] || capability
    message.success(res?.message || `${label} 单模块运行已提交`)
    await loadTasks()
    startPolling()
  } catch (error) {
    if (error?.response?.status !== 401) {
      message.error(stageRunErrorMessage(error))
    }
  } finally {
    runningStageKey.value = ''
  }
}

function handleStageRun(row, capability) {
  if (restartingTaskId.value || runningStageKey.value) return
  const label = STAGES.find(([cap]) => cap === capability)?.[1] || capability
  const current = stepOf(row, capability)?.status || 'pending'
  Modal.confirm({
    title: `单独运行「${label}」？`,
    content: `当前状态：${STEP_TAGS[current]?.[1] || current}。只运行该模块，其他模块和已有产物不变；仍使用原 task_id，并复用模块已有断点。`,
    okText: '开始运行',
    cancelText: '取消',
    onOk: () => submitStageRun(row, capability),
  })
}

async function refreshTasks() {
  await loadTasks()
  if (tasks.value.length && tasks.value.every((r) => isFinal(r.status))) {
    stopPolling()
  }
}

function startPolling() {
  stopPolling()
  // 生成任务动辄几十分钟,5s 一轮没有意义,只是把后端连接池打满
  timer = setInterval(refreshTasks, 15000)
  refreshTasks()
}

function stopPolling() {
  if (timer) clearInterval(timer)
  timer = null
}

function stepOf(row, capability) {
  return row.steps.find((s) => s.capability === capability)
}

function stepTag(row, capability) {
  const status = stepOf(row, capability)?.status || 'pending'
  return { status, tag: STEP_TAGS[status] || ['default', status] }
}

function taskTag(status) {
  return TASK_TAGS[status] || ['default', status]
}

function progressStatus(status) {
  if (status === 'failed') return 'exception'
  if (status === 'succeeded') return 'success'
  return 'active'
}

onMounted(async () => {
  await Promise.all([fetchMeta(), loadTasks()])
  if (tasks.value.some((r) => !isFinal(r.status))) startPolling()
})
onBeforeUnmount(stopPolling)
</script>

<template>
  <div class="creation-test">
    <div class="dr-page-head">
      <div>
        <div class="dr-page-title">创作测试 · 视觉小说</div>
        <div class="dr-page-desc">
          上传小说,按创作模式或管线跑一轮完整生成 · 产物与 Token 消耗均为真实
        </div>
      </div>
    </div>

    <a-alert
      type="warning"
      show-icon
      banner
      message="真实创作"
      description="提交后按所选管线分发到各组实现,生成真实素材并打包成游戏,消耗真实 Token。管线指定的实现若未注册或心跳异常,任务会直接失败并给出原因。效率/均衡/高质量三档模式留给创造平台,这里直接按管线测。"
      class="creation-test__alert"
    />

    <a-row :gutter="16">
      <a-col :xs="24" :lg="9">
        <a-card :bordered="false" title="创作参数">
          <a-spin :spinning="metaLoading">
            <a-form layout="vertical">
              <a-form-item label="小说文件(.txt,最多 50 本)">
                <a-upload
                  :file-list="fileList"
                  :before-upload="beforeUpload"
                  accept=".txt"
                  multiple
                  @remove="removeFile"
                >
                  <a-button>选择文件</a-button>
                </a-upload>
              </a-form-item>

              <a-form-item label="管线(要测哪条就选哪条)">
                <a-select v-model:value="form.pipeline_name" placeholder="选择管线">
                  <a-select-option v-for="p in pipelines" :key="p.name" :value="p.name">
                    {{ p.display_name || p.name }}
                  </a-select-option>
                </a-select>
              </a-form-item>

              <a-row :gutter="12">
                <a-col :span="12">
                  <a-form-item label="画面风格">
                    <a-select v-model:value="form.art_style">
                      <a-select-option v-for="[value, text] in ART_STYLES" :key="value" :value="value">
                        {{ text }}
                      </a-select-option>
                    </a-select>
                  </a-form-item>
                </a-col>
                <a-col :span="12">
                  <a-form-item label="输出语言">
                    <a-select v-model:value="form.output_language">
                      <a-select-option v-for="[value, text] in LANGUAGES" :key="value" :value="value">
                        {{ text }}
                      </a-select-option>
                    </a-select>
                  </a-form-item>
                </a-col>
              </a-row>

              <a-form-item label="剧情模式">
                <a-select v-model:value="form.story_mode">
                  <a-select-option v-for="[value, text] in STORY_MODES" :key="value" :value="value">
                    {{ text }}
                  </a-select-option>
                </a-select>
              </a-form-item>

              <a-button type="primary" :loading="submitting" @click="handleSubmit">
                开始创作
              </a-button>
            </a-form>
          </a-spin>
        </a-card>
      </a-col>

      <a-col :xs="24" :lg="15">
        <a-card :bordered="false" title="我的任务">
          <template #extra>
            <a-button size="small" :loading="listLoading" @click="loadTasks">刷新</a-button>
          </template>

          <a-empty v-if="!tasks.length" description="暂无任务,提交后在此查看阶段进度" />

          <div v-for="row in tasks" :key="row.task_id" class="creation-test__task">
            <div class="creation-test__task-head">
              <span class="creation-test__task-name">{{ row.project_name }}</span>
              <a-tag :color="taskTag(row.status)[0]">{{ taskTag(row.status)[1] }}</a-tag>
              <span v-if="row.pipeline_name" class="creation-test__task-pipeline">
                {{ pipelineLabel(row.pipeline_name) }}
              </span>
              <span class="creation-test__task-id">{{ row.task_id }}</span>
              <a-tooltip :title="restartTooltip(row)">
                <a-button
                  type="text"
                  size="small"
                  :loading="restartingTaskId === row.task_id"
                  :disabled="restartDisabled(row)"
                  @click="handleRestart(row)"
                >
                  重新启动
                </a-button>
              </a-tooltip>
              <a-dropdown :trigger="['click']">
                <a-button
                  type="text"
                  size="small"
                  :disabled="Boolean(restartingTaskId || runningStageKey)"
                >
                  单模块运行⌄
                </a-button>
                <template #overlay>
                  <a-menu @click="({ key }) => handleStageRun(row, key)">
                    <a-menu-item v-for="[cap, label] in STAGES" :key="cap">
                      {{ label }}
                    </a-menu-item>
                  </a-menu>
                </template>
              </a-dropdown>
              <a-popconfirm
                title="取消任务并从列表移除?正在跑的那一步会跑完,之后不再往下派发。"
                ok-text="确认"
                cancel-text="返回"
                @confirm="handleDelete(row)"
              >
                <a-button type="text" size="small" danger>删除</a-button>
              </a-popconfirm>
            </div>

            <a-progress :percent="row.progress" :status="progressStatus(row.status)" />

            <div v-if="row.error_msg" class="creation-test__task-error">{{ row.error_msg }}</div>

            <div class="creation-test__stages">
              <div
                v-for="[cap, label] in STAGES"
                :key="cap"
                class="creation-test__stage"
                :class="`creation-test__stage--${stepTag(row, cap).status}`"
              >
                <span class="creation-test__stage-name">{{ label }}</span>
                <span class="creation-test__stage-impl">
                  {{ stepOf(row, cap)?.impl_id || '—' }}
                </span>
                <a-tooltip :title="stepOf(row, cap)?.error_msg">
                  <a-tag :color="stepTag(row, cap).tag[0]">{{ stepTag(row, cap).tag[1] }}</a-tag>
                </a-tooltip>
              </div>
            </div>
          </div>
        </a-card>
      </a-col>
    </a-row>

    <a-modal
      v-model:open="restartDialog.open"
      title="选择重新启动范围"
      :mask-closable="!restartingTaskId"
      :closable="!restartingTaskId"
      @cancel="closeRestartDialog"
    >
      <template v-if="restartDialog.row">
        <a-radio-group v-model:value="restartDialog.mode">
          <a-space direction="vertical">
            <a-radio :value="RESTART_MODES.FAILED_ONLY">
              仅重启失败模块
            </a-radio>
            <a-radio :value="RESTART_MODES.RUNNING_AND_FAILED">
              重启进行中和失败模块
            </a-radio>
          </a-space>
        </a-radio-group>

        <div class="creation-test__restart-preview">
          <div>
            <span class="creation-test__restart-preview-label">将重启：</span>
            {{ restartPlan(restartDialog.row, restartDialog.mode).restartable.join('、') || '无' }}
          </div>
          <div>
            <span class="creation-test__restart-preview-label">保留已完成：</span>
            {{ restartPlan(restartDialog.row, restartDialog.mode).completed.join('、') || '无' }}
          </div>
          <div>
            <span class="creation-test__restart-preview-label">保留进行中：</span>
            {{ restartPlan(restartDialog.row, restartDialog.mode).running.join('、') || '无' }}
          </div>
          <div>
            <span class="creation-test__restart-preview-label">保留等待：</span>
            {{ restartPlan(restartDialog.row, restartDialog.mode).pending.join('、') || '无' }}
          </div>
          <div class="creation-test__restart-tip">
            已完成模块始终保留；仍使用原 task_id，已有产物按模块断点复用。
          </div>
        </div>
      </template>
      <template #footer>
        <a-button :disabled="Boolean(restartingTaskId)" @click="closeRestartDialog">
          取消
        </a-button>
        <a-button
          type="primary"
          :loading="Boolean(restartingTaskId)"
          :disabled="!restartDialog.row || !restartPlan(restartDialog.row, restartDialog.mode).canRestart"
          @click="confirmRestartDialog"
        >
          重新启动
        </a-button>
      </template>
    </a-modal>
  </div>
</template>

<style scoped>
.creation-test__alert {
  margin-bottom: 16px;
  border-radius: 10px;
}

.creation-test__task-pipeline {
  color: var(--dr-gold-2);
  font-size: 12.5px;
}

.creation-test__task-error {
  margin-top: 6px;
  color: #d4237a;
  font-size: 12.5px;
  word-break: break-all;
}

.creation-test__task {
  padding: 12px 0;
  border-bottom: 1px dashed var(--dr-border);
}

.creation-test__task:last-child {
  border-bottom: none;
}

.creation-test__task-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.creation-test__task-name {
  font-family: 'Noto Serif SC', 'Source Han Serif SC', 'Songti SC', 'SimSun', serif;
  font-size: 15px;
  font-weight: 700;
  color: var(--dr-text);
  letter-spacing: 1px;
}

.creation-test__task-id {
  margin-left: auto;
  color: var(--dr-text-3);
  font-family: monospace;
  font-size: 12px;
}

.creation-test__task-head :deep(.ant-btn) {
  padding: 0 4px;
}

.creation-test__stages {
  margin-top: 10px;
  display: grid;
  gap: 6px;
}

.creation-test__stage {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  border: 1px solid var(--dr-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.02);
  transition: border-color 0.18s ease, background 0.18s ease;
}

.creation-test__stage--running,
.creation-test__stage--dispatched {
  border-color: rgba(53, 214, 208, 0.5);
  background: rgba(53, 214, 208, 0.07);
}

.creation-test__stage--succeeded {
  border-color: rgba(232, 182, 76, 0.35);
}

.creation-test__stage--failed {
  border-color: rgba(212, 35, 122, 0.5);
  background: rgba(212, 35, 122, 0.07);
}

.creation-test__stage-name {
  flex: 1;
  font-size: 14px;
}

.creation-test__stage-impl {
  color: var(--dr-text-3);
  font-family: monospace;
  font-size: 11.5px;
}

.creation-test__restart-preview {
  margin-top: 18px;
  padding: 12px;
  border: 1px solid var(--dr-border);
  border-radius: 8px;
  line-height: 1.8;
}

.creation-test__restart-preview-label {
  color: var(--dr-text-3);
}

.creation-test__restart-tip {
  margin-top: 6px;
  color: var(--dr-text-3);
  font-size: 12px;
}
</style>
