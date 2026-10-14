<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import {
  FileTextOutlined,
  TeamOutlined,
  PartitionOutlined,
  SendOutlined,
} from '@ant-design/icons-vue'
import { getTaskEntryApi, getTaskStatusApi } from '@/api'
import UploadScript from './components/UploadScript.vue'
import ScriptOutline from './components/ScriptOutline.vue'
import RolesAndScenes from './components/rolesAndScenes/index.vue'
import PlotInterpretation from './components/plotInterpretation/index.vue'
import ExportRelease from './components/exportRelease/index.vue'

// 单本创作当前固定四步。游戏界面不再人工选择，game_builder 会根据
// story_tree.json 的 UI_mode(Ancient/Other) 自动应用对应界面。
// 每一步确认后只允许继续向前，步骤条只展示进度、不承担跳转。
// 批量创作是黑盒(提交后按管线跑到底、没有人工确认卡点),提交完直接回首页看进度。
const STEPS = [
  { index: 1, title: '剧本大纲', icon: FileTextOutlined },
  { index: 2, title: '角色和场景', icon: TeamOutlined },
  { index: 3, title: '剧情演绎', icon: PartitionOutlined },
  // 游戏界面选择暂时停用，由 UI_mode 自动判断 Ancient/Other。
  { index: 4, title: '导出与发布', icon: SendOutlined },
]
const EXPORT_STEP = 4

/** 后端 entry.stage → 步骤。
 *  stage 语义是「下一个该去的地方」(见 api_gateway 的 _STAGE_TO_ENTRY)。 */
const STAGE_TO_STEP = {
  create: 1,
  config: 1,
  confirm: 1,
  material: 2,
  performance: 3,
  publish: EXPORT_STEP,
}

/** info_confirm_stage → 步骤。entry 接口不可用时的兜底:
 *  info_confirm_stage 记录「已完成的那一步」,所以要往后推一格。 */
const CONFIRM_STAGE_TO_STEP = {
  upload: 1,
  config: 1,
  confirmed: 2,
  material: 3,
  performance: EXPORT_STEP,
  publish: EXPORT_STEP,
}

const route = useRoute()
const router = useRouter()

const taskId = ref(typeof route.query.task_id === 'string' ? route.query.task_id : '')
const sourceTaskId = computed(() =>
  typeof route.query.source_task_id === 'string' ? route.query.source_task_id : '',
)
const sourceAdaptation = ref(null)
const activeStep = ref(1)
/** 第一步的两种形态:upload 上传剧本 / outline 大纲与信息确认 */
const stepOneView = ref(taskId.value ? 'outline' : 'upload')
const restoring = ref(false)
/** 恢复现场时拿到的任务进度,导出页用来显示真实百分比 */
const taskProgress = ref(0)
/** 已发布的作品前面几步只读:发布后再改素材/演绎,线上包和 CMS 数据就对不上了 */
const published = ref(false)
/** 导出页进版本管理/章节编辑子视图时收掉步骤条 */
const isGameControlShow = ref(true)

const steps = STEPS
const exportStep = EXPORT_STEP

const showSteps = computed(() => isGameControlShow.value)

watch(
  () => route.query.task_id,
  () => {
    const routeTaskId = typeof route.query.task_id === 'string' ? route.query.task_id : ''
    if (!routeTaskId) return
    taskId.value = routeTaskId
    restoreEntry(routeTaskId)
  },
  { immediate: true },
)

/** 带 task_id 进来时读 entry 恢复到上次离开的步骤。
 *  entry 拿不到时回落到 status 里的 info_confirm_stage 自行换算,
 *  所以这两个请求都不弹通用错误提示。 */
async function restoreEntry(id) {
  restoring.value = true
  try {
    try {
      const entry = await getTaskEntryApi(id, { skipErrorToast: true })
      const data = entry?.data ?? entry
      taskProgress.value = Number(data?.progress_percentage ?? 0) || 0
      published.value = Boolean(data?.is_published)
      activeStep.value = resolveEntryStep(data)
      stepOneView.value = activeStep.value === 1 ? 'outline' : 'upload'
      if (data?.error_message) message.error(`任务存在异常：${data.error_message}`)
      return
    } catch {
      // entry 不可用,走 status 兜底
    }

    try {
      const status = await getTaskStatusApi(id, { skipErrorToast: true })
      const data = status?.data ?? status
      taskProgress.value = Number(data?.progress_percentage ?? 0) || 0
      published.value = Boolean(data?.is_published)
      activeStep.value = CONFIRM_STAGE_TO_STEP[data?.info_confirm_stage] ?? 1
      stepOneView.value = activeStep.value === 1 ? 'outline' : 'upload'
    } catch {
      message.error('读取作品进度失败，已回到第一步，可重新上传或稍后重试')
      activeStep.value = 1
      stepOneView.value = taskId.value ? 'outline' : 'upload'
    }
  } finally {
    restoring.value = false
  }
}

function resolveEntryStep(data) {
  if (data?.is_published) return EXPORT_STEP
  // 演绎确认后直接进入导出发布；界面由 story_tree.UI_mode 自动决定。
  if (data?.info_confirm_stage === 'performance') return EXPORT_STEP
  if (data?.info_confirm_stage === 'publish') return EXPORT_STEP
  return STAGE_TO_STEP[data?.stage] ?? 1
}

function goStep(index) {
  // 步骤条只标记当前进度。到达下一步后，前后步骤都不能从这里跳转。
  if (index !== activeStep.value) return
}

/** 单本上传成功:进第 1 步的信息确认 */
function handleUploaded({ taskId: newTaskId, adaptation = null }) {
  taskId.value = newTaskId
  sourceAdaptation.value = adaptation
  stepOneView.value = 'outline'
  activeStep.value = 1
  // 写进地址栏,刷新后能按 task_id 恢复现场
  router.replace({ query: { ...route.query, task_id: newTaskId, upload_type: 'single' } })
}

/** 批量提交完成:批量是黑盒(提交后按管线跑到底,没有人工确认步骤),
 *  所以不进步骤流程,直接回首页看「我的项目」里的进度。 */
function handleBatchSubmitted() {
  router.push('/home')
}

function handleOutlineConfirmed() {
  activeStep.value = 2
}

function handleMaterialConfirmed() {
  activeStep.value = 3
}

function handlePerformanceConfirmed() {
  activeStep.value = exportStep
}

function handlePublished() {
  published.value = true
  if (taskId.value) restoreEntry(taskId.value)
}

function backToUpload() {
  stepOneView.value = 'upload'
  activeStep.value = 1
}
</script>

<template>
  <div class="workspace-create">
    <div class="classic-creation-workspace">
      <nav v-show="showSteps" class="creation-flow">
        <button
          v-for="item in steps"
          :key="item.index"
          type="button"
          :class="{
            active: item.index === activeStep,
            completed: item.index < activeStep,
            future: item.index > activeStep,
          }"
          :disabled="item.index !== activeStep"
          @click="goStep(item.index)"
        >
          <component :is="item.icon" />
          <span class="creation-flow__copy">
            <small>STEP {{ item.index }}</small>
            <b>{{ item.title }}</b>
          </span>
        </button>
      </nav>

      <p v-if="published && showSteps" class="create-published-note">
        作品已发布，前面的步骤仅供查看，不能再修改。
      </p>

      <a-spin :spinning="restoring">
        <div class="create-step-body">
          <!-- 第 1 步:上传剧本(批量在这里提交后直接回首页,不进后续步骤) -->
          <UploadScript
            v-if="activeStep === 1 && stepOneView === 'upload'"
            :source-task-id="sourceTaskId"
            @uploaded="handleUploaded"
            @submitted-batch="handleBatchSubmitted"
          />

          <!-- 第 1 步:剧本大纲与信息确认 -->
          <ScriptOutline
            v-else-if="activeStep === 1"
            :task-id="taskId"
            :source-task-id="sourceTaskId"
            :initial-adaptation="sourceAdaptation"
            :published="published"
            @back="backToUpload"
            @confirmed="handleOutlineConfirmed"
          />

          <!-- 第 2 步:角色和场景 -->
          <RolesAndScenes
            v-else-if="activeStep === 2"
            :task-id="taskId"
            :published="published"
            @confirmed="handleMaterialConfirmed"
          />

          <!-- 第 3 步:剧情演绎 -->
          <PlotInterpretation
            v-else-if="activeStep === 3"
            :task-id="taskId"
            :published="published"
            @confirmed="handlePerformanceConfirmed"
          />

          <!-- 游戏界面选择暂时停用；由 story_tree.UI_mode 自动判断 Ancient/Other。 -->

          <!-- 第 4 步:导出与发布 -->
          <ExportRelease
            v-else-if="activeStep === exportStep"
            :task-id="taskId"
            :progress="taskProgress"
            :published="published"
            @published="handlePublished"
            @is-show-btn-and-step="isGameControlShow = $event"
          />
        </div>
      </a-spin>
    </div>
  </div>
</template>

<style scoped>
.workspace-create .classic-creation-workspace .creation-flow {
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.creation-flow button:disabled {
  cursor: not-allowed;
  opacity: 1;
}

.workspace-create {
  min-height: calc(100vh - 88px);
  padding: 22px 0 64px;
  background:
    radial-gradient(760px 320px at 50% -80px, rgba(102, 119, 204, 0.1), transparent 70%),
    transparent;
}

.classic-creation-workspace {
  width: min(100%, 1260px);
  margin: 0 auto;
}

.creation-flow__copy {
  display: grid;
  gap: 2px;
  text-align: left;
}

.creation-flow__copy small {
  color: #626b82;
  font-size: 9px;
  font-weight: 600;
  letter-spacing: 0.12em;
}

.creation-flow__copy b {
  font-size: 14px;
  font-weight: 500;
}

.creation-flow button.completed {
  color: #7e8daf !important;
}

.creation-flow button.completed .creation-flow__copy small {
  color: #66779b;
}

.creation-flow button.active .creation-flow__copy small {
  color: #8199e8;
}

.creation-flow button.future {
  color: #545c70 !important;
}

.create-published-note {
  width: min(100%, 1180px);
  margin: 0 auto 14px;
  padding: 10px 14px;
  border: 1px solid rgba(196, 157, 88, 0.18);
  border-radius: 10px;
  background: rgba(109, 79, 31, 0.12);
  color: #d6b879;
  font-size: 13px;
}

.create-step-body {
  min-height: 520px;
  padding-bottom: 40px;
}

@media (max-width: 760px) {
  .workspace-create {
    padding-top: 10px;
  }

  .creation-flow__copy {
    display: none;
  }
}

@media (min-width: 1101px) {
  .workspace-create .classic-creation-workspace .creation-flow {
    grid-template-columns: repeat(4, max-content);
  }
}
</style>
