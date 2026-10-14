<script setup>
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import { ReloadOutlined } from '@ant-design/icons-vue'
import {
  confirmStoryApi,
  generateProjectNameApi,
  getSimilarConfigApi,
  getStorySummaryApi,
  getTaskGenerationParamsApi,
  getTaskStatusApi,
  regenerateStorySummaryApi,
  saveStoryAdaptationApi,
  saveStoryConfigApi,
} from '@/api'
import {
  ART_STYLE_LABELS,
  ART_STYLES,
  GAMEPLAYS,
  OUTPUT_LANGUAGES,
  RESOLUTION_LABELS,
  STORY_MODES,
} from '../constants'

// 第 1 步后半段:剧本大纲。摘要是异步生成的,进来就轮询;
// 「下一步」= 保存输出约束(config) + 信息确认提交生成(confirm),两个真实接口。
const props = defineProps({
  taskId: { type: String, default: '' },
  sourceTaskId: { type: String, default: '' },
  initialAdaptation: { type: Object, default: null },
})
const emit = defineEmits(['confirmed', 'back'])

/** 摘要轮询上限,与 InfoConfirm 原实现保持一致:5s 一次、最多 2 分钟 */
const POLL_INTERVAL = 5000
const POLL_TIMEOUT = 120000

const summary = reactive({ outline: '', background: '', one_liner: '' })
const summaryStatus = ref('pending')
const summaryLoading = ref(false)
const projectName = ref('')
const nameLoading = ref(false)
const confirming = ref(false)

/** 上传时定下的生成参数,这里只读展示 */
const generationParams = ref(null)

const config = reactive({
  output_language: OUTPUT_LANGUAGES[0].value,
  art_style: ART_STYLES[0].value,
  story_mode: STORY_MODES[0].value,
  gameplay: GAMEPLAYS[0].value,
})

const adaptation = reactive({
  target_audience: 'male',
  narrative_perspective: 'third_person',
  genre: undefined,
  time_space: undefined,
  plot: [],
})

const AUDIENCE_OPTIONS = [
  { value: 'male', label: '男性向' },
  { value: 'female', label: '女性向' },
]
const PERSPECTIVE_OPTIONS = [
  { value: 'first_person', label: '第一人称' },
  { value: 'third_person', label: '第三人称' },
]
const GENRE_OPTIONS = [
  { value: 'romance', label: '恋爱' },
  { value: 'mystery', label: '悬疑' },
  { value: 'thriller', label: '惊悚' },
  { value: 'fantasy', label: '幻想' },
]
const TIME_SPACE_OPTIONS = [
  { value: 'modern', label: '现代' },
  { value: 'ancient', label: '古代' },
  { value: 'ancient_style', label: '架空古风' },
  { value: 'near_modern', label: '近现代' },
  { value: 'future', label: '未来' },
  { value: 'fictional', label: '架空世界' },
]
const PLOT_OPTIONS = [
  { value: 'rebirth', label: '重生' },
  { value: 'revenge', label: '复仇' },
  { value: 'counterattack', label: '逆袭' },
  { value: 'sweet_romance', label: '甜宠' },
  { value: 'power_struggle', label: '权谋' },
  { value: 'investigation', label: '探案' },
]

let pollTimer = null
let canceled = false

onMounted(() => {
  if (!props.taskId) return
  if (props.initialAdaptation) applyAdaptation(props.initialAdaptation)
  else if (props.sourceTaskId) loadSourceAdaptation()
  loadGenerationParams()
  loadTaskBasics()
  pollSummary()
})

onUnmounted(stopPoll)

function stopPoll() {
  canceled = true
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

function applyAdaptation(value) {
  if (!value) return
  if (value.target_audience) adaptation.target_audience = value.target_audience
  if (value.narrative_perspective) {
    adaptation.narrative_perspective = value.narrative_perspective
  }
  adaptation.genre = value.genre || undefined
  adaptation.time_space = value.time_space || undefined
  adaptation.plot = Array.isArray(value.plot) ? value.plot.slice(0, 2) : []
}

async function loadSourceAdaptation() {
  try {
    const res = await getSimilarConfigApi(props.sourceTaskId, { skipErrorToast: true })
    const data = res?.data ?? res
    applyAdaptation(data?.adaptation)
  } catch {
    // 上传页已经展示过源配置读取错误；这里保持默认值，不重复打扰用户。
  }
}

function onPlotChange(value) {
  if (value.length <= 2) return
  adaptation.plot = value.slice(0, 2)
  message.warning('热门情节最多选择 2 个')
}

async function loadGenerationParams() {
  try {
    const res = await getTaskGenerationParamsApi(props.taskId)
    const data = res?.data ?? res
    generationParams.value = data
    // 上传时选的风格就是配置项的默认值,避免用户在这里被迫重选一次
    if (data?.art_style) config.art_style = data.art_style
  } catch {
    // 只影响参数回显,不阻断确认流程
  }
}

/** 预填作品名与已存的输出约束:上传时后端已落库一份(起名是异步任务,可能稍后才有) */
async function loadTaskBasics() {
  try {
    const res = await getTaskStatusApi(props.taskId, { skipErrorToast: true })
    const data = res?.data ?? res
    if (data?.project_name && !projectName.value) projectName.value = data.project_name
    if (data?.output_language) config.output_language = data.output_language
    if (data?.story_mode) config.story_mode = data.story_mode
    if (data?.gameplay) config.gameplay = data.gameplay
  } catch {
    // 取不到就让用户自己填,不阻断
  }
}

/** 轮询摘要。ready 才填内容,failed/超时给出可重试的明确状态 */
async function pollSummary(startedAt = Date.now()) {
  if (!props.taskId) return
  canceled = false
  summaryLoading.value = true

  try {
    const res = await getStorySummaryApi(props.taskId, { skipErrorToast: true })
    if (canceled) return
    const data = res?.data ?? res

    if (data?.status === 'ready') {
      summary.outline = data.outline || ''
      summary.background = data.background || ''
      summary.one_liner = data.one_liner || ''
      summaryStatus.value = 'ready'
      summaryLoading.value = false
      return
    }

    if (data?.status === 'failed') {
      summaryStatus.value = 'failed'
      summaryLoading.value = false
      message.error(data?.error || '摘要提炼失败，可点击重新提炼')
      return
    }

    if (Date.now() - startedAt >= POLL_TIMEOUT) {
      summaryStatus.value = 'failed'
      summaryLoading.value = false
      message.error('摘要生成超时，可点击重新提炼')
      return
    }

    pollTimer = setTimeout(() => pollSummary(startedAt), POLL_INTERVAL)
  } catch {
    if (canceled) return
    summaryStatus.value = 'failed'
    summaryLoading.value = false
    message.error('获取剧本摘要失败，可点击重新提炼')
  }
}

/** 重新提炼:先提交 regenerate,再从头轮询 */
async function regenerate() {
  if (!props.taskId) return
  stopPoll()
  summaryStatus.value = 'pending'
  summary.outline = ''
  summary.background = ''
  summary.one_liner = ''

  try {
    await regenerateStorySummaryApi(props.taskId)
    pollSummary()
  } catch {
    summaryStatus.value = 'failed'
    summaryLoading.value = false
  }
}

async function refreshName() {
  if (!props.taskId) return
  nameLoading.value = true
  try {
    const res = await generateProjectNameApi(props.taskId)
    const data = res?.data ?? res
    if (data?.project_name) projectName.value = data.project_name
  } catch {
    // 拦截器已提示(摘要没好时后端回 400 并说明原因)
  } finally {
    nameLoading.value = false
  }
}

/** 下一步:保存输出约束 → 确认并提交生成 */
async function handleNext() {
  if (!props.taskId) {
    message.warning('缺少作品 ID，请重新上传剧本')
    return
  }
  const name = projectName.value.trim()
  if (!name) {
    message.warning('请填写作品名')
    return
  }

  confirming.value = true
  try {
    // config 接口会覆盖全部字段,所以模型/分辨率要按上传时落库的值原样回传,
    // 否则这一步会把用户在上传页选的模型重置成后端默认值。取不到就不传。
    const payload = {
      output_language: config.output_language,
      art_style: config.art_style,
      story_mode: config.story_mode,
      gameplay: config.gameplay,
    }
    const params = generationParams.value
    if (params) {
      ;['text_model', 'image_model', 'audio_model', 'resolution'].forEach((key) => {
        if (params[key]) payload[key] = params[key]
      })
    }
    await saveStoryConfigApi(props.taskId, payload)
    if (config.story_mode === 'multi') {
      await saveStoryAdaptationApi(props.taskId, {
        target_audience: adaptation.target_audience,
        narrative_perspective: adaptation.narrative_perspective,
        genre: adaptation.genre || null,
        time_space: adaptation.time_space || null,
        plot: adaptation.plot,
      })
    }
    await confirmStoryApi(props.taskId, {
      project_name: name,
      source_task_id: props.sourceTaskId || undefined,
    })
    message.success('已提交生成，可继续查看角色与场景')
    emit('confirmed')
  } catch {
    // 「任务已提交生成」这类 detail 由拦截器原样展示
  } finally {
    confirming.value = false
  }
}
</script>

<template>
  <div class="create-panel">
    <div class="create-panel__head">
      <h3>剧本大纲</h3>
      <div class="create-panel__head-actions">
        <a-button size="small" @click="emit('back')">重新上传</a-button>
        <a-button size="small" :loading="summaryLoading" @click="regenerate">
          重新提炼
        </a-button>
      </div>
    </div>

    <div class="create-field">
      <label for="create-project-name">作品名</label>
      <div class="create-field__row">
        <a-input
          id="create-project-name"
          v-model:value="projectName"
          :maxlength="20"
          placeholder="填写作品名，或点右侧按钮由 AI 生成"
        />
        <a-button :loading="nameLoading" @click="refreshName">
          <template #icon><ReloadOutlined /></template>
          生成
        </a-button>
      </div>
    </div>

    <a-spin :spinning="summaryLoading" tip="剧本摘要生成中，约需 1-2 分钟">
      <div v-if="summaryStatus === 'ready'" class="create-summary">
        <section>
          <h4>一句话故事</h4>
          <p>{{ summary.one_liner || '暂无内容' }}</p>
        </section>
        <section>
          <h4>故事背景</h4>
          <p>{{ summary.background || '暂无内容' }}</p>
        </section>
        <section>
          <h4>故事梗概</h4>
          <p>{{ summary.outline || '暂无内容' }}</p>
        </section>
      </div>

      <div v-else-if="summaryStatus === 'failed'" class="create-empty">
        摘要提炼失败，可点击「重新提炼」重试
      </div>

      <div v-else class="create-empty">剧本摘要生成中…</div>
    </a-spin>

    <div class="create-field-grid">
      <div class="create-field">
        <label for="create-language">输出语言</label>
        <a-select id="create-language" v-model:value="config.output_language" :options="OUTPUT_LANGUAGES" />
      </div>
      <div class="create-field">
        <label for="create-style">画面风格</label>
        <a-select id="create-style" v-model:value="config.art_style" :options="ART_STYLES" />
      </div>
      <div class="create-field">
        <label for="create-story-mode">剧情模式</label>
        <a-select id="create-story-mode" v-model:value="config.story_mode" :options="STORY_MODES" />
      </div>
      <div class="create-field">
        <label for="create-gameplay">游戏玩法</label>
        <a-select id="create-gameplay" v-model:value="config.gameplay" :options="GAMEPLAYS" />
      </div>
    </div>

    <section v-if="config.story_mode === 'multi'" class="create-adaptation">
      <div class="create-adaptation__head">
        <h4>多剧情改编选配</h4>
        <span v-if="sourceTaskId">已沿用源项目设置，可修改</span>
      </div>
      <div class="create-field-grid">
        <div class="create-field">
          <label for="create-audience">目标受众</label>
          <a-select id="create-audience" v-model:value="adaptation.target_audience" :options="AUDIENCE_OPTIONS" />
        </div>
        <div class="create-field">
          <label for="create-perspective">叙事视角</label>
          <a-select id="create-perspective" v-model:value="adaptation.narrative_perspective" :options="PERSPECTIVE_OPTIONS" />
        </div>
        <div class="create-field">
          <label for="create-genre">题材偏好</label>
          <a-select id="create-genre" v-model:value="adaptation.genre" allow-clear placeholder="不限制" :options="GENRE_OPTIONS" />
        </div>
        <div class="create-field">
          <label for="create-time-space">时空背景</label>
          <a-select id="create-time-space" v-model:value="adaptation.time_space" allow-clear placeholder="不限制" :options="TIME_SPACE_OPTIONS" />
        </div>
        <div class="create-field create-field--wide">
          <label for="create-plot">热门情节（最多 2 个）</label>
          <a-select
            id="create-plot"
            v-model:value="adaptation.plot"
            mode="multiple"
            allow-clear
            placeholder="不限制"
            :options="PLOT_OPTIONS"
            @change="onPlotChange"
          />
        </div>
      </div>
    </section>

    <dl v-if="generationParams" class="create-meta">
      <div>
        <dt>文本模型</dt>
        <dd>{{ generationParams.text_model || '--' }}</dd>
      </div>
      <div>
        <dt>图像模型</dt>
        <dd>{{ generationParams.image_model || '--' }}</dd>
      </div>
      <div>
        <dt>音频模型</dt>
        <dd>{{ generationParams.audio_model || '--' }}</dd>
      </div>
      <div>
        <dt>分辨率</dt>
        <dd>{{ RESOLUTION_LABELS[generationParams.resolution] || generationParams.resolution || '--' }}</dd>
      </div>
      <div>
        <dt>上传时风格</dt>
        <dd>{{ ART_STYLE_LABELS[generationParams.art_style] || generationParams.art_style || '--' }}</dd>
      </div>
    </dl>

    <div class="create-panel__foot">
      <a-button type="primary" :loading="confirming" @click="handleNext">
        保存并提交生成
      </a-button>
    </div>
  </div>
</template>

<style scoped>
@import '../styles/panel.css';

.create-summary {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  padding: 2px 0 8px;
}

.create-summary section {
  padding: 16px 17px;
  border: 1px solid rgba(145, 158, 205, 0.14);
  border-radius: 13px;
  background: rgba(18, 22, 31, 0.34);
}

.create-summary section:last-child {
  grid-column: 1 / -1;
}

.create-summary h4 {
  margin: 0 0 8px;
  color: #aeb8da;
  font-size: 13px;
  font-weight: 500;
}

.create-summary p {
  margin: 0;
  color: #d9deeb;
  font-size: 14px;
  line-height: 1.75;
  white-space: pre-wrap;
}

.create-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 28px;
  margin: 18px 0 0;
  padding-top: 16px;
  border-top: 1px solid rgba(145, 158, 205, 0.12);
}

.create-meta dt {
  color: #767c8f;
  font-size: 12px;
}

.create-meta dd {
  margin: 2px 0 0;
  color: #d6dae7;
  font-size: 13px;
}

.create-adaptation {
  margin-top: 18px;
  padding: 16px;
  border: 1px solid rgba(145, 158, 205, 0.16);
  border-radius: 14px;
  background: rgba(70, 82, 129, 0.08);
}

.create-adaptation__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}

.create-adaptation__head h4 {
  margin: 0;
  color: #dfe4f5;
  font-size: 14px;
  font-weight: 500;
}

.create-adaptation__head span {
  color: #9099bb;
  font-size: 12px;
}

.create-field--wide {
  grid-column: 1 / -1;
}

@media (max-width: 760px) {
  .create-summary {
    grid-template-columns: 1fr;
  }

  .create-summary section:last-child {
    grid-column: auto;
  }
}
</style>
