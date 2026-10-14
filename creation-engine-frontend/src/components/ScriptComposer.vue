<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  getCreationModesApi,
  getModelOptionsApi,
  getRawNovelApi,
  getSimilarConfigApi,
  uploadNovelsBatchApi,
  uploadStoryApi,
} from '@/api'
import {
  ART_STYLES,
  OUTPUT_LANGUAGES,
  RESOLUTION_OPTIONS,
  STORY_MODES,
} from '@/views/create/constants'

const props = defineProps({
  showSample: { type: Boolean, default: true },
  sourceTaskId: { type: String, default: '' },
})

const emit = defineEmits(['uploaded', 'submitted-batch'])

const BATCH_KEY_STORAGE = 'creation-engine:pending-batch-idempotency-key'

function loadPendingBatchKey() {
  try {
    return globalThis.sessionStorage?.getItem(BATCH_KEY_STORAGE) || ''
  } catch {
    return ''
  }
}

function persistPendingBatchKey(value) {
  try {
    if (value) globalThis.sessionStorage?.setItem(BATCH_KEY_STORAGE, value)
    else globalThis.sessionStorage?.removeItem(BATCH_KEY_STORAGE)
  } catch {
    // 禁用存储的浏览器仍可在当前页面内复用 ref 中的 key。
  }
}

const MAX_FILES = 50
const MAX_SIZE = 5 * 1024 * 1024
const GAME_TYPES = [
  { label: '视觉小说', gameplay: 'interactive_story' },
  { label: '互动影游', gameplay: 'free_growth', disabled: true },
]

const files = ref([])
const fileInput = ref(null)
const gameType = ref(GAME_TYPES[0].label)
const openMenu = ref(null)
const submitting = ref(false)
// 一次批量提交只生成一个幂等键。网络超时后文件仍保留，用户再次点击会复用
// 同一个 key；只有确认提交成功后才清空，避免 ACK 丢失时重复创建一批任务。
const batchIdempotencyKey = ref(loadPendingBatchKey())
const sampleRemoved = ref(false)
const modelOptions = ref(null)
const textModel = ref('')
const imageModel = ref('')
const audioModel = ref('')
const resolution = ref('')
const visualStyle = ref(ART_STYLES[0].value)
const outputLanguage = ref(OUTPUT_LANGUAGES[0].value)
const storyMode = ref(STORY_MODES[0].value)
const modes = ref([])
const createMode = ref('')
const sourcePipelineName = ref('')
const similarConfig = ref(null)
const similarLoading = ref(false)
const similarLoaded = ref(false)
const similarError = ref('')
const popoverRect = ref(null)
const triggerRefs = {}

const isBatch = computed(() => files.value.length > 1)
const canUpload = computed(
  () =>
    !submitting.value &&
    !similarLoading.value,
)
const uploadTitle = computed(() => {
  if (similarLoading.value) return '正在载入源项目'
  if (submitting.value) return '正在提交作品'
  return '上传剧本，最多 50 部作品'
})
const currentGameType = computed(
  () => GAME_TYPES.find((type) => type.label === gameType.value) || GAME_TYPES[0],
)
const canStart = computed(
  () =>
    files.value.length > 0 &&
    !submitting.value &&
    !similarLoading.value &&
    !currentGameType.value.disabled,
)
const startTitle = computed(() => {
  if (similarLoading.value) return '正在载入源项目'
  if (!files.value.length) return '请先上传剧本'
  if (currentGameType.value.disabled) return '互动影游暂未开放'
  return '开始创建'
})
const showSampleRow = computed(
  () => props.showSample && !props.sourceTaskId && !files.value.length && !sampleRemoved.value,
)
const artStyleLabel = computed(
  () => ART_STYLES.find((s) => s.value === visualStyle.value)?.label || visualStyle.value,
)
const resolutionLabel = computed(
  () => RESOLUTION_OPTIONS.find((r) => r.value === resolution.value)?.label || '默认',
)
const createModeLabel = computed(
  () => {
    const selected = modes.value.find((m) => m.mode === createMode.value)
    if (sourcePipelineName.value) return selected?.label || '沿用原创建模式'
    return selected?.label || '默认模式'
  },
)
const modelSummary = computed(
  () => [textModel.value, imageModel.value, audioModel.value].filter(Boolean).join(' · ') || '后端默认',
)
const outputLanguageLabel = computed(
  () => OUTPUT_LANGUAGES.find((item) => item.value === outputLanguage.value)?.label || outputLanguage.value,
)
const storyModeLabel = computed(
  () => STORY_MODES.find((item) => item.value === storyMode.value)?.label || storyMode.value,
)

function notifyUploadDisabled(event) {
  if (canUpload.value) return
  event?.preventDefault()
  event?.stopPropagation()
  message.warning(uploadTitle.value)
}

function notifyStartDisabled(event) {
  if (canStart.value) return
  event?.preventDefault()
  event?.stopPropagation()
  message.warning(startTitle.value)
}
const similarSummaryItems = computed(() => [
  `游戏类型：${gameType.value}`,
  `创建模式：${createModeLabel.value}`,
  `文本模型：${textModel.value || '默认'}`,
  `图像模型：${imageModel.value || '默认'}`,
  `音频模型：${audioModel.value || '默认'}`,
  `分辨率：${resolutionLabel.value}`,
  `视觉风格：${artStyleLabel.value}`,
  `输出语言：${outputLanguageLabel.value}`,
  `剧情模式：${storyModeLabel.value}`,
])
const popoverStyle = computed(() => {
  if (!popoverRect.value) return {}
  return {
    top: `${popoverRect.value.top}px`,
    left: `${popoverRect.value.left}px`,
  }
})

function setTriggerRef(name, element) {
  if (element) triggerRefs[name] = element
}

async function positionPopover(name) {
  const trigger = triggerRefs[name]
  if (!trigger) return
  const rect = trigger.getBoundingClientRect()
  popoverRect.value = {
    top: rect.bottom + 8,
    left: Math.min(rect.left, window.innerWidth - 253),
  }
  await nextTick()
  const popover = document.querySelector('.floating-config-popover')
  if (!popover) return
  const height = popover.getBoundingClientRect().height
  if (popoverRect.value.top + height > window.innerHeight - 8) {
    popoverRect.value = {
      ...popoverRect.value,
      top: Math.max(8, rect.top - height - 8),
    }
  }
}

function toggleMenu(name) {
  if (openMenu.value === name) {
    openMenu.value = null
    popoverRect.value = null
    return
  }
  openMenu.value = name
  positionPopover(name)
}

function openOnHover(name) {
  if (openMenu.value === name) return
  openMenu.value = name
  positionPopover(name)
}

function closeMenu() {
  openMenu.value = null
  popoverRect.value = null
}

function onPointerDown(event) {
  if (!openMenu.value) return
  if (event.target.closest?.('.config-control, .floating-config-popover')) return
  closeMenu()
}

function repositionPopover() {
  if (openMenu.value) positionPopover(openMenu.value)
}

function pickFiles(list) {
  if (!canUpload.value) return
  const arr = Array.from(list || [])
  if (!arr.length) return
  const valid = arr.filter(
    (file) => file.name.toLowerCase().endsWith('.txt') && file.size <= MAX_SIZE,
  )
  if (valid.length !== arr.length) message.warning('仅支持上传 5MB 以内的 .txt 剧本文件')
  if (!valid.length) return

  const merged = props.sourceTaskId ? valid : [...files.value, ...valid]
  if (merged.length > MAX_FILES) message.warning('单次最多支持50部作品上传')
  files.value = merged.length > 1 ? merged.slice(0, MAX_FILES) : valid.slice(0, 1)
  sampleRemoved.value = false
  if (fileInput.value) fileInput.value.value = ''
}

function removeFile(file) {
  files.value = files.value.filter((item) => item !== file)
}

function switchMode(mode) {
  if (mode === 'single' && files.value.length > 1) files.value = files.value.slice(0, 1)
}

function selectGameType(type) {
  if (type.disabled) {
    message.info('互动影游暂未开放')
    return
  }
  gameType.value = type.label
  closeMenu()
}

function loadSample() {
  if (!canUpload.value) {
    message.warning(uploadTitle.value)
    return
  }
  const text = [
    '雾港回声',
    '',
    '第一章 潮声',
    '雾从港口漫上来的时候,林昭正把最后一箱货推进仓库。',
    '她听见身后有人叫她的名字,声音混在浪里,听不清是谁。',
    '"你还在等那艘船?"老陈靠在门框上,烟头明明灭灭。',
    '林昭没有回头。她知道那艘船三年前就沉了,可每个雾天,她还是会来。',
    '',
    '第二章 回声',
    '第二天清晨,港口的广播里念出一串陌生的编号。',
    '那是三年前那艘船的编号。',
  ].join('\n')
  files.value = [new File([text], '示范剧本-雾港回声.txt', { type: 'text/plain' })]
  message.success('已载入示范剧本')
}

function dismissSample() {
  sampleRemoved.value = true
}

async function loadOptions() {
  try {
    const res = await getModelOptionsApi({ skipErrorToast: true })
    const data = res?.data ?? res
    modelOptions.value = data
    textModel.value = data?.text_model?.default || ''
    imageModel.value = data?.image_model?.default || ''
    audioModel.value = data?.audio_model?.default || ''
    resolution.value = data?.resolution?.default || ''
  } catch {
    modelOptions.value = null
  }
}

async function loadModes() {
  try {
    const res = await getCreationModesApi({ skipErrorToast: true })
    const rows = Array.isArray(res) ? res : res?.data || []
    modes.value = rows
    const quality = rows.find((mode) => mode.mode === 'quality' && mode.available !== false)
    createMode.value = (quality || rows.find((mode) => mode.available) || rows[0])?.mode || ''
  } catch {
    modes.value = []
  }
}

function selectCreationMode(mode) {
  createMode.value = mode
  // 用户主动选择后按当前“模式 → 管线”映射提交，不再强制沿用源项目的旧管线。
  sourcePipelineName.value = ''
  closeMenu()
}

function applySimilarConfig(data) {
  similarConfig.value = data
  if (data?.output_language) outputLanguage.value = data.output_language
  if (data?.art_style) visualStyle.value = data.art_style
  if (data?.story_mode) storyMode.value = data.story_mode
  if (data?.text_model) textModel.value = data.text_model
  if (data?.image_model) imageModel.value = data.image_model
  if (data?.audio_model) audioModel.value = data.audio_model
  if (data?.resolution) resolution.value = data.resolution

  const matchedType = GAME_TYPES.find((type) => type.gameplay === data?.gameplay)
  if (matchedType) gameType.value = matchedType.label

  sourcePipelineName.value = data?.pipeline_name || ''
  if (sourcePipelineName.value) {
    const matchedMode = modes.value.find((mode) => mode.pipeline_name === sourcePipelineName.value)
    createMode.value = matchedMode?.mode || ''
  }
}

async function loadSimilarProject() {
  if (!props.sourceTaskId || similarLoading.value) return
  similarLoading.value = true
  similarLoaded.value = false
  similarError.value = ''
  try {
    const configRes = await getSimilarConfigApi(props.sourceTaskId, { skipErrorToast: true })
    const configData = configRes?.data ?? configRes
    applySimilarConfig(configData)

    const rawRes = await getRawNovelApi(props.sourceTaskId, { skipErrorToast: true })
    const blob = rawRes?.data ?? rawRes
    if (!(blob instanceof Blob)) throw new Error('源项目原始文本格式异常')
    const filename = configData?.novel_filename || `${props.sourceTaskId}.txt`
    files.value = [new File([blob], filename, { type: blob.type || 'text/plain' })]
    sampleRemoved.value = false
    similarLoaded.value = true
    message.success('已载入源项目文本和全部选配，可直接修改后创建')
  } catch (error) {
    similarError.value =
      error?.response?.data?.detail || error?.message || '源项目内容载入失败，请重试'
    message.error(similarError.value)
  } finally {
    similarLoading.value = false
  }
}

function appendFields(formData) {
  formData.append('output_language', outputLanguage.value)
  formData.append('art_style', visualStyle.value)
  formData.append('story_mode', storyMode.value)
  const gameplay = GAME_TYPES.find((type) => type.label === gameType.value)?.gameplay
  if (gameplay) formData.append('gameplay', gameplay)
  if (resolution.value) formData.append('resolution', resolution.value)
  if (sourcePipelineName.value) formData.append('pipeline_name', sourcePipelineName.value)
  else if (createMode.value) formData.append('mode', createMode.value)
  if (textModel.value) formData.append('text_model', textModel.value)
  if (imageModel.value) formData.append('image_model', imageModel.value)
  if (audioModel.value) formData.append('audio_model', audioModel.value)
}

function newBatchIdempotencyKey() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  return `batch-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

async function submit() {
  if (submitting.value) return
  if (!files.value.length) {
    message.warning('请先上传剧本')
    return
  }

  submitting.value = true
  try {
    const formData = new FormData()
    appendFields(formData)
    if (isBatch.value) {
      if (!batchIdempotencyKey.value) {
        batchIdempotencyKey.value = newBatchIdempotencyKey()
        persistPendingBatchKey(batchIdempotencyKey.value)
      }
      formData.append('batch_id', batchIdempotencyKey.value)
      files.value.forEach((file) => formData.append('novels', file))
      const res = await uploadNovelsBatchApi(formData, batchIdempotencyKey.value)
      const data = res?.data ?? res
      const count = Number(data?.total ?? data?.count ?? files.value.length)
      message.success(data?.message || `已提交 ${count} 本作品，可在「我的项目」查看进度`)
      files.value = []
      batchIdempotencyKey.value = ''
      persistPendingBatchKey('')
      emit('submitted-batch')
      return
    }

    formData.append('novel', files.value[0])
    const res = await uploadStoryApi(formData)
    const data = res?.data ?? res
    if (!data?.task_id) {
      message.error('上传成功但未返回作品 ID，请到我的项目中查看')
      return
    }
    files.value = []
    message.success('上传成功，正在提炼剧本摘要')
    emit('uploaded', {
      ...data,
      mode: 'single',
      taskId: data.task_id,
      sourceTaskId: props.sourceTaskId,
      adaptation: similarConfig.value?.adaptation || null,
    })
  } catch {
    // 失败提示由响应拦截器统一给出
  } finally {
    submitting.value = false
  }
}

onMounted(async () => {
  window.addEventListener('pointerdown', onPointerDown)
  window.addEventListener('resize', repositionPopover)
  window.addEventListener('scroll', repositionPopover, true)
  await Promise.all([loadOptions(), loadModes()])
  if (props.sourceTaskId) await loadSimilarProject()
})

onBeforeUnmount(() => {
  window.removeEventListener('pointerdown', onPointerDown)
  window.removeEventListener('resize', repositionPopover)
  window.removeEventListener('scroll', repositionPopover, true)
})
</script>

<template>
  <div class="creator-card script-composer">
    <div class="creator-tabs script-tabs-region">
      <button class="active" type="button">上传剧本</button>
      <button class="disabled" aria-disabled="true" :tabindex="-1">AI生成剧本 <span>BETA</span></button>
      <button class="disabled" aria-disabled="true" :tabindex="-1">画布 <span>BETA</span></button>
    </div>

    <div class="tab-panel script-content-region">
      <div
        class="drop-area"
        :class="{ 'is-upload-disabled': !canUpload }"
        :aria-disabled="!canUpload"
        :title="!canUpload ? uploadTitle : ''"
        @dragover.prevent
        @drop.prevent="pickFiles($event.dataTransfer.files)"
      >
        <input
          ref="fileInput"
          type="file"
          accept=".txt,text/plain"
          :multiple="!sourceTaskId"
          :disabled="!canUpload"
          hidden
          @change="pickFiles($event.target.files)"
        />

        <div v-if="sourceTaskId" class="similar-source-state">
          <span v-if="similarLoading" class="similar-source-state__loading">正在读取源项目文本和选配…</span>
          <template v-else-if="similarError">
            <span class="similar-source-state__error">{{ similarError }}</span>
            <button type="button" @click="loadSimilarProject">重新载入</button>
          </template>
          <template v-else-if="similarLoaded">
            <strong>已载入源项目</strong>
            <div class="similar-source-state__items">
              <span v-for="item in similarSummaryItems" :key="item">{{ item }}</span>
            </div>
          </template>
        </div>

        <div v-if="files.length" class="uploaded-list">
          <div v-for="file in files" :key="`${file.name}-${file.size}`" class="uploaded-chip">
            <div>
              <b>{{ file.name }}</b>
              <span>{{ Math.max(file.size / 1024, 0.1).toFixed(1) }} KB</span>
            </div>
            <button type="button" :aria-label="`移除 ${file.name}`" @click="removeFile(file)">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <path d="m6 6 12 12M18 6 6 18" />
              </svg>
            </button>
          </div>
        </div>

        <div v-if="showSampleRow" class="sample-script-row">
          <button
            class="sample-script"
            type="button"
            :disabled="!canUpload"
            :title="canUpload ? '点击载入示范剧本' : uploadTitle"
            @click="loadSample"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <path d="M6 2.8h8l4 4V21H6z" /><path d="M14 2.8V7h4M9 12h6M9 15.5h6" />
            </svg>
            <span>示范剧本-雾港回声.txt</span>
          </button>
          <button class="sample-dismiss" type="button" aria-label="移除示范剧本" @click.stop="dismissSample">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <path d="m6 6 12 12M18 6 6 18" />
            </svg>
          </button>
        </div>

        <div class="creator-controls">
          <a-tooltip :title="uploadTitle" placement="top" :auto-adjust-overflow="true">
            <div
              class="config-control upload-control"
              :class="{ 'is-disabled': !canUpload }"
              @click="notifyUploadDisabled"
            >
              <button
                type="button"
                :disabled="!canUpload"
                :aria-disabled="!canUpload"
                :aria-label="uploadTitle"
                @click="fileInput?.click()"
              >
                <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M6 2.8h8l4 4V21H6z" /><path d="M14 2.8V7h4M9 14h6M12 11v6" />
                </svg>
              </button>
            </div>
          </a-tooltip>

          <div :ref="(el) => setTriggerRef('type', el?.querySelector('button'))" class="config-control type-control" :class="{ 'menu-open': openMenu === 'type' }" @mouseenter="openOnHover('type')">
            <button type="button" aria-label="游戏类型" :data-tooltip="gameType" @click="toggleMenu('type')">
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 8h8a5 5 0 0 1 4.7 6.7l-1 2.8a2 2 0 0 1-3.2.8L14.8 17H9.2l-1.7 1.3a2 2 0 0 1-3.2-.8l-1-2.8A5 5 0 0 1 8 8Z" /><path d="M8 11v4M6 13h4M16.5 12h.01M18.5 14h.01" /></svg>
            </button>
          </div>

          <div :ref="(el) => setTriggerRef('model', el?.querySelector('button'))" class="config-control model-control" :class="{ 'menu-open': openMenu === 'model' }" @mouseenter="openOnHover('model')">
            <button type="button" aria-label="模型选型" :data-tooltip="modelSummary" @click="toggleMenu('model')">
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3 8 4.5v9L12 21l-8-4.5v-9L12 3Z" /><path d="m4 7.5 8 4.5 8-4.5M12 12v9" /></svg>
            </button>
          </div>

          <div :ref="(el) => setTriggerRef('more', el?.querySelector('button'))" class="config-control more-control" :class="{ 'menu-open': openMenu === 'more' }" @mouseenter="openOnHover('more')">
            <button type="button" aria-label="更多配置" :data-tooltip="`${resolutionLabel} · ${artStyleLabel} · ${outputLanguageLabel} · ${storyModeLabel}`" @click="toggleMenu('more')">
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h10M18 7h2M4 17h2M10 17h10" /><circle cx="16" cy="7" r="2" /><circle cx="8" cy="17" r="2" /></svg>
            </button>
          </div>
        </div>

        <div class="create-actions">
          <div :ref="(el) => setTriggerRef('creation-mode', el?.querySelector('button'))" class="config-control create-mode-control" :class="{ 'menu-open': openMenu === 'creation-mode' }" @mouseenter="openOnHover('creation-mode')">
            <button type="button" aria-label="创建模式" :data-tooltip="createModeLabel" @click="toggleMenu('creation-mode')">
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 15a8 8 0 1 1 16 0" /><path d="m12 12 4-3" /><path d="M6 18h12" /></svg>
            </button>
          </div>
          <a-tooltip :title="!canStart ? startTitle : ''" placement="top">
            <div
              class="start-create-wrap"
              :class="{ 'is-disabled': !canStart }"
              @click="notifyStartDisabled"
            >
              <button
                class="start-create"
                :class="{ muted: !canStart, 'is-disabled': !canStart, 'is-enabled': canStart }"
                :disabled="!canStart"
                :aria-disabled="!canStart"
                :aria-label="startTitle"
                type="button"
                @click="submit"
              >
                <svg v-if="canStart" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M6.5 10.5 12 5l5.5 5.5" /></svg>
                <svg v-else width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="10" width="14" height="10" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3" /></svg>
              </button>
            </div>
          </a-tooltip>
        </div>
      </div>
    </div>
  </div>

  <Teleport to="body">
    <div v-if="openMenu === 'type'" class="config-popover floating-config-popover" :style="popoverStyle">
      <p>游戏类型 · {{ gameType }}</p>
      <button
        v-for="type in GAME_TYPES"
        :key="type.label"
        :class="{ selected: gameType === type.label, disabled: type.disabled }"
        :disabled="type.disabled"
        :title="type.disabled ? '暂未开放' : ''"
        @click="selectGameType(type)"
      >
        {{ type.label }}{{ type.disabled ? '（暂未开放）' : '' }}
      </button>
    </div>
    <div v-if="openMenu === 'model'" class="config-popover floating-config-popover" :style="popoverStyle">
      <p>模型选型 · {{ modelSummary }}</p>
      <template v-if="modelOptions">
        <label>文本模型<select v-model="textModel"><option v-for="model in modelOptions.text_model?.options || []" :key="model" :value="model">{{ model }}</option></select></label>
        <label>图像模型<select v-model="imageModel"><option v-for="model in modelOptions.image_model?.options || []" :key="model" :value="model">{{ model }}</option></select></label>
        <label>音频模型<select v-model="audioModel"><option v-for="model in modelOptions.audio_model?.options || []" :key="model" :value="model">{{ model }}</option></select></label>
      </template>
      <p v-else class="config-popover__hint">模型清单暂不可用，将使用后端默认模型</p>
    </div>
    <div v-if="openMenu === 'more'" class="config-popover floating-config-popover" :style="popoverStyle">
      <p>更多配置 · {{ resolutionLabel }} · {{ artStyleLabel }}</p>
      <label>分辨率<select v-model="resolution"><option v-for="item in RESOLUTION_OPTIONS" :key="item.value" :value="item.value">{{ item.label }}</option></select></label>
      <label>视觉风格<select v-model="visualStyle"><option v-for="style in ART_STYLES" :key="style.value" :value="style.value">{{ style.label }}</option></select></label>
      <label>输出语言<select v-model="outputLanguage"><option v-for="item in OUTPUT_LANGUAGES" :key="item.value" :value="item.value">{{ item.label }}</option></select></label>
      <label>剧情模式<select v-model="storyMode"><option v-for="item in STORY_MODES" :key="item.value" :value="item.value">{{ item.label }}</option></select></label>
    </div>
    <div v-if="openMenu === 'creation-mode'" class="config-popover floating-config-popover" :style="popoverStyle">
      <p>创建模式 · {{ createModeLabel }}</p>
      <button v-for="mode in modes" :key="mode.mode" :class="{ selected: createMode === mode.mode }" :disabled="mode.available === false" @click="selectCreationMode(mode.mode)">{{ mode.label || mode.mode }}{{ mode.available === false ? '(未配置)' : '' }}</button>
      <p v-if="!modes.length" class="config-popover__hint">模式清单暂不可用，将使用默认管线</p>
    </div>
  </Teleport>
</template>

<style scoped>
.sample-script-row {
  position: absolute;
  top: 12px;
  left: 14px;
  right: 14px;
  display: flex;
  align-items: center;
  gap: 4px;
}

.sample-script-row .sample-script {
  position: static;
  min-width: 0;
  max-width: none;
  flex: 0 1 auto;
}

.sample-script-row .sample-script span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sample-dismiss {
  flex: 0 0 auto;
  display: grid;
  place-items: center;
  padding: 4px;
  border-radius: 6px;
  background: transparent;
  color: #8a8a90;
}

.sample-dismiss:hover {
  color: #303033;
}

.sample-script:disabled {
  cursor: not-allowed;
  filter: none;
  opacity: 0.42;
}

.uploaded-list {
  max-height: 168px;
  overflow-y: auto;
}

.similar-source-state {
  position: absolute;
  z-index: 2;
  top: 48px;
  right: 14px;
  left: 14px;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  color: #aeb6ce;
  font-size: 11px;
}

.similar-source-state strong {
  flex: 0 0 auto;
  color: #dce3ff;
  font-size: 11px;
  font-weight: 500;
}

.similar-source-state__items {
  min-width: 0;
  display: flex;
  gap: 6px;
  overflow-x: auto;
  scrollbar-width: thin;
}

.similar-source-state__items span {
  flex: 0 0 auto;
  padding: 4px 7px;
  border: 1px solid rgba(148, 160, 216, 0.24);
  border-radius: 999px;
  background: rgba(98, 111, 172, 0.12);
  color: #bfc8e7;
}

.similar-source-state__loading {
  color: #aeb9e9;
}

.similar-source-state__error {
  min-width: 0;
  overflow: hidden;
  color: #ffaaaa;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.similar-source-state > button {
  flex: 0 0 auto;
  padding: 4px 8px;
  border-radius: 6px;
  background: rgba(111, 126, 208, 0.24);
  color: #dce3ff;
}

.floating-config-popover {
  position: fixed;
  z-index: 9999;
  bottom: auto;
  visibility: visible;
  opacity: 1;
}

.floating-config-popover label {
  display: grid;
  gap: 4px;
  margin-top: 8px;
}

.floating-config-popover select {
  width: 100%;
  min-width: 0;
}

.floating-config-popover > button.disabled,
.floating-config-popover > button:disabled {
  cursor: not-allowed;
  opacity: 0.45;
  filter: none;
}

.drop-area.is-upload-disabled {
  cursor: not-allowed;
}

.drop-area.is-upload-disabled::after {
  position: absolute;
  z-index: 2;
  border-radius: inherit;
  background: rgba(9, 11, 17, 0.24);
  content: '';
  inset: 0;
  pointer-events: none;
}

/* 禁用遮罩和卡片描边不能压住上传/开始按钮的悬浮提示。 */
.creator-controls,
.create-actions {
  z-index: 8;
}

.upload-control.is-disabled > button,
.upload-control.is-disabled > button:hover {
  cursor: not-allowed;
  filter: none;
  opacity: 0.42;
  pointer-events: none;
}

.start-create-wrap {
  position: relative;
  display: inline-flex;
  border-radius: 50%;
}

.start-create-wrap.is-disabled {
  cursor: not-allowed;
}

.start-create-wrap.is-disabled .start-create {
  cursor: not-allowed;
  filter: none;
  pointer-events: none;
}

</style>
