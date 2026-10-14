<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { message, Modal } from 'ant-design-vue'
import {
  getPipelinesApi,
  createPipelineApi,
  updatePipelineApi,
  deletePipelineApi,
  getImplsApi,
  getCreationModesApi,
  updateCreationModeApi,
} from '@/api'
import { isAdmin } from '@/store/auth'

const admin = computed(() => isAdmin())

/** 能力中文名(与创作侧 7 阶段对应,顺序即执行顺序) */
const CAPABILITY_LABELS = {
  story_planner: '文本处理',
  info_extractor: '信息提取',
  char_designer: '角色设计',
  scene_painter: '场景绘制',
  audio_producer: '配音生成',
  sound_arranger: '音效编排',
  game_builder: '游戏打包',
}

/** 卡片顶部色条:走龙族三主色(金/青/玫) */
const PRESET_COLORS = {
  default: 'linear-gradient(90deg, #f5d089, #e8b64c)',
  recommended: 'linear-gradient(90deg, #e8b64c, #d4237a)',
  'group-a': 'linear-gradient(90deg, #7ceee9, #35d6d0)',
  'group-b': 'linear-gradient(90deg, #d4237a, #7262fd)',
}
const CUSTOM_COLOR = 'linear-gradient(90deg, #35d6d0, #e8b64c)'

const loading = ref(false)
const dataSource = ref([])

/** impls 按 capability 分组:{ capability: [impl, ...] } */
const implGroups = ref({})

/** 弹窗里要渲染的能力列表:已注册的 capability 优先按固定顺序排 */
const capabilityList = computed(() => {
  const known = Object.keys(CAPABILITY_LABELS).filter((c) => implGroups.value[c])
  const extra = Object.keys(implGroups.value).filter((c) => !CAPABILITY_LABELS[c])
  return [...known, ...extra]
})

function cardColor(record) {
  return PRESET_COLORS[record.name] || CUSTOM_COLOR
}

function implCount(implMap) {
  return Object.keys(implMap || {}).length
}

/** 详情:按 7 阶段顺序列出每个能力 → 指定实现或"自动选择" */
function implDetail(implMap) {
  return Object.keys(CAPABILITY_LABELS).map((cap) => ({
    capability: cap,
    label: CAPABILITY_LABELS[cap],
    impl_id: (implMap || {})[cap] || '',
  }))
}

async function fetchList() {
  loading.value = true
  try {
    dataSource.value = await getPipelinesApi()
  } finally {
    loading.value = false
  }
}

async function fetchImpls() {
  const impls = await getImplsApi()
  const groups = {}
  for (const impl of impls) {
    ;(groups[impl.capability] ||= []).push(impl)
  }
  implGroups.value = groups
}

// ==================== 新建/编辑弹窗 ====================
const modalOpen = ref(false)
const modalSaving = ref(false)
const isEdit = ref(false)

const form = reactive({
  name: '',
  display_name: '',
  description: '',
  impl_map: {}, // { capability: impl_id | undefined }
})

function openCreate() {
  isEdit.value = false
  form.name = ''
  form.display_name = ''
  form.description = ''
  form.impl_map = {}
  modalOpen.value = true
}

function openEdit(record) {
  isEdit.value = true
  form.name = record.name
  form.display_name = record.display_name
  form.description = record.description
  form.impl_map = { ...record.impl_map }
  modalOpen.value = true
}

async function handleSave() {
  if (!isEdit.value && !form.name.trim()) {
    message.warning('请填写管线名')
    return
  }
  const implMap = {}
  for (const [cap, impl] of Object.entries(form.impl_map)) {
    if (impl) implMap[cap] = impl
  }
  modalSaving.value = true
  try {
    if (isEdit.value) {
      await updatePipelineApi(form.name, {
        display_name: form.display_name,
        description: form.description,
        impl_map: implMap,
      })
      message.success('管线已更新')
    } else {
      await createPipelineApi({
        name: form.name.trim(),
        display_name: form.display_name,
        description: form.description,
        impl_map: implMap,
      })
      message.success('管线已创建')
    }
    modalOpen.value = false
    fetchList()
  } finally {
    modalSaving.value = false
  }
}

function handleDelete(record) {
  Modal.confirm({
    title: `确认删除管线「${record.display_name || record.name}」?`,
    content: '删除后使用该管线名创建任务将失败',
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      await deletePipelineApi(record.name)
      message.success('已删除')
      fetchList()
    },
  })
}

// ==================== 创作模式 → 管线映射(仅管理员可改) ====================
const modes = ref([])
const modesLoading = ref(false)
/** 正在保存的 mode,用于单行 loading */
const savingMode = ref('')

const MODE_HINTS = {
  efficiency: '优先出片速度,适合快速验证',
  balanced: '速度与质量折中,日常首选',
  quality: '优先成品质量,耗时与用量更高',
}

async function fetchModes() {
  modesLoading.value = true
  try {
    modes.value = await getCreationModesApi()
  } finally {
    modesLoading.value = false
  }
}

/** 某管线被哪些创作模式引用,卡片上打标 */
function modesOf(pipelineName) {
  return modes.value.filter((m) => m.pipeline_name === pipelineName)
}

async function handleModeChange(mode, pipelineName) {
  savingMode.value = mode
  try {
    await updateCreationModeApi(mode, pipelineName)
    message.success('创作模式已更新')
    fetchModes()
  } finally {
    savingMode.value = ''
  }
}

onMounted(() => {
  fetchList()
  fetchImpls()
  fetchModes()
})
</script>

<template>
  <div class="pipelines">
    <div class="dr-page-head pipelines__header">
      <div>
        <div class="dr-page-title">管线管理</div>
        <div class="dr-page-desc">
          管线 = 各生成步骤所用实现的组合;创建任务时按管线名记录任务与 Token 用量
        </div>
      </div>
      <a-button type="primary" @click="openCreate">新建管线</a-button>
    </div>

    <a-card :bordered="false" class="pipelines__modes">
      <div class="dr-group-title">
        创作模式配置
        <a-tag v-if="!admin" color="default">仅超级管理员可修改</a-tag>
      </div>
      <div class="pipelines__modes-desc">
        创造平台按模式发起创作,平台在此把每档模式落到具体管线
      </div>
      <a-spin :spinning="modesLoading">
        <div class="pipelines__modes-grid">
          <div v-for="m in modes" :key="m.mode" class="pipelines__mode-item">
            <div class="pipelines__mode-head">
              <span class="pipelines__mode-name">{{ m.label }}</span>
              <a-tooltip v-if="!m.pipeline_exists" title="该管线已不存在,请重新指定">
                <a-tag color="error">失效</a-tag>
              </a-tooltip>
            </div>
            <div class="pipelines__mode-hint">{{ MODE_HINTS[m.mode] }}</div>
            <a-select
              :value="m.pipeline_name"
              :disabled="!admin"
              :loading="savingMode === m.mode"
              style="width: 100%"
              @change="(val) => handleModeChange(m.mode, val)"
            >
              <a-select-option v-for="p in dataSource" :key="p.name" :value="p.name">
                {{ p.display_name || p.name }}
              </a-select-option>
            </a-select>
          </div>
        </div>
      </a-spin>
    </a-card>

    <a-spin :spinning="loading">
      <div class="pipelines__grid">
        <a-card
          v-for="record in dataSource"
          :key="record.name"
          class="pipelines__card"
          :bordered="false"
        >
          <div class="pipelines__card-bar" :style="{ background: cardColor(record) }" />
          <div class="pipelines__card-head">
            <div class="pipelines__card-titles">
              <span class="pipelines__card-name">{{ record.display_name || record.name }}</span>
              <span class="pipelines__card-code">{{ record.name }}</span>
            </div>
            <a-tag :color="record.is_preset ? 'blue' : 'cyan'" class="pipelines__card-tag">
              {{ record.is_preset ? '预置' : '自定义' }}
            </a-tag>
          </div>

          <div class="pipelines__card-desc">{{ record.description || '—' }}</div>

          <div class="pipelines__card-impl">
            <span class="pipelines__impl-summary">
              {{ implCount(record.impl_map)
                ? `已指定 ${implCount(record.impl_map)} 个能力实现`
                : '全部自动选择' }}
            </span>
            <a-popover placement="bottom" trigger="click">
              <template #content>
                <div class="pipelines__impl-detail">
                  <div
                    v-for="entry in implDetail(record.impl_map)"
                    :key="entry.capability"
                    class="pipelines__impl-row"
                  >
                    <span class="pipelines__impl-label">{{ entry.label }}</span>
                    <span
                      class="pipelines__impl-value"
                      :class="{ 'pipelines__impl-value--auto': !entry.impl_id }"
                    >
                      {{ entry.impl_id || '自动选择' }}
                    </span>
                  </div>
                </div>
              </template>
              <a-button type="link" size="small">详情</a-button>
            </a-popover>
          </div>

          <div class="pipelines__card-foot">
            <span class="pipelines__card-modes">
              <a-tag
                v-for="m in modesOf(record.name)"
                :key="m.mode"
                color="gold"
                class="pipelines__mode-tag"
              >
                {{ m.label }}
              </a-tag>
            </span>
            <a-space size="small">
              <a-tooltip :title="record.is_preset && !admin ? '预置管线仅超级管理员可编辑' : ''">
                <a-button
                  type="text"
                  size="small"
                  :disabled="record.is_preset && !admin"
                  @click="openEdit(record)"
                >
                  编辑
                </a-button>
              </a-tooltip>
              <a-button
                v-if="!record.is_preset"
                type="text"
                size="small"
                danger
                @click="handleDelete(record)"
              >
                删除
              </a-button>
            </a-space>
          </div>
        </a-card>
      </div>
      <a-empty v-if="!loading && !dataSource.length" description="暂无管线" />
    </a-spin>

    <a-modal
      v-model:open="modalOpen"
      :title="isEdit ? `编辑管线:${form.name}` : '新建管线'"
      :confirm-loading="modalSaving"
      width="620px"
      @ok="handleSave"
    >
      <a-form layout="vertical">
        <a-row :gutter="16">
          <a-col v-if="!isEdit" :span="12">
            <a-form-item label="管线名(唯一标识)" required>
              <a-input v-model:value="form.name" placeholder="如 group-c" />
            </a-form-item>
          </a-col>
          <a-col :span="12">
            <a-form-item label="展示名">
              <a-input v-model:value="form.display_name" placeholder="如 三组" />
            </a-form-item>
          </a-col>
          <a-col :span="isEdit ? 12 : 24">
            <a-form-item label="说明">
              <a-input v-model:value="form.description" placeholder="可选" />
            </a-form-item>
          </a-col>
        </a-row>

        <a-divider orientation="left" style="font-size: 13px; margin: 4px 0 16px">
          实现组合(不选 = 该步自动选择健康实现)
        </a-divider>
        <a-empty
          v-if="!capabilityList.length"
          description="注册中心暂无已注册实现"
          :image-style="{ height: '48px' }"
        />
        <a-row :gutter="16">
          <a-col v-for="cap in capabilityList" :key="cap" :span="12">
            <a-form-item
              :label="CAPABILITY_LABELS[cap] || cap"
              class="pipelines__impl-item"
            >
              <a-select
                v-model:value="form.impl_map[cap]"
                placeholder="自动选择"
                allow-clear
                style="width: 100%"
              >
                <a-select-option
                  v-for="impl in implGroups[cap]"
                  :key="impl.impl_id"
                  :value="impl.impl_id"
                  :disabled="!impl.enabled"
                >
                  {{ impl.impl_id }}{{ impl.enabled ? '' : '(已禁用)' }}
                </a-select-option>
              </a-select>
            </a-form-item>
          </a-col>
        </a-row>
      </a-form>
    </a-modal>
  </div>
</template>

<style scoped>
.pipelines__header {
  margin-bottom: 20px;
}

.pipelines__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 16px;
}

.pipelines__card {
  position: relative;
  overflow: hidden;
  border: 1px solid var(--dr-border);
  border-radius: 12px;
  box-shadow: 0 10px 26px rgba(0, 0, 0, 0.35);
  transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
}

.pipelines__card:hover {
  transform: translateY(-2px);
  border-color: rgba(232, 182, 76, 0.45);
  box-shadow: 0 14px 34px rgba(0, 0, 0, 0.45), var(--dr-glow-gold);
}

.pipelines__card-bar {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 4px;
}

.pipelines__card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.pipelines__card-titles {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}

.pipelines__card-name {
  font-family: 'Noto Serif SC', 'Source Han Serif SC', 'Songti SC', 'SimSun', serif;
  font-size: 16px;
  font-weight: 700;
  color: var(--dr-text);
  letter-spacing: 1px;
}

.pipelines__card-code {
  color: var(--dr-text-3);
  font-size: 12px;
  font-family: monospace;
}

.pipelines__card-tag {
  margin-right: 0;
}

.pipelines__card-desc {
  color: var(--dr-text-2);
  font-size: 13px;
  min-height: 38px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.pipelines__card-impl {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 8px 0;
  padding: 6px 10px;
  background: rgba(53, 214, 208, 0.07);
  border: 1px solid rgba(53, 214, 208, 0.16);
  border-radius: 8px;
}

.pipelines__impl-summary {
  color: var(--dr-cyan-2);
  font-size: 13px;
}

.pipelines__impl-detail {
  min-width: 320px;
}

.pipelines__impl-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 4px 0;
  border-bottom: 1px dashed var(--dr-border);
}

.pipelines__impl-row:last-child {
  border-bottom: none;
}

.pipelines__impl-label {
  color: var(--dr-text-2);
  flex-shrink: 0;
}

.pipelines__impl-value {
  color: var(--dr-gold-2);
  font-family: monospace;
  font-size: 12px;
}

.pipelines__impl-value--auto {
  color: var(--dr-text-3);
}

.pipelines__card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: 10px;
  border-top: 1px solid var(--dr-border);
}

.pipelines__modes {
  margin-bottom: 20px;
}

.pipelines__modes-desc {
  margin: -4px 0 14px;
  color: var(--dr-text-3);
  font-size: 12.5px;
}

.pipelines__modes-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 14px;
}

.pipelines__mode-item {
  padding: 12px 14px;
  border: 1px solid var(--dr-border);
  border-radius: 10px;
  background: rgba(232, 182, 76, 0.05);
}

.pipelines__mode-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.pipelines__mode-name {
  font-family: 'Noto Serif SC', 'Source Han Serif SC', 'Songti SC', 'SimSun', serif;
  font-size: 15px;
  font-weight: 700;
  color: var(--dr-gold-2);
  letter-spacing: 1px;
}

.pipelines__mode-hint {
  margin: 4px 0 10px;
  min-height: 32px;
  color: var(--dr-text-3);
  font-size: 12px;
}

.pipelines__card-modes {
  display: flex;
  gap: 4px;
  min-width: 0;
}

.pipelines__mode-tag {
  margin-right: 0;
}

.pipelines__impl-item {
  margin-bottom: 10px;
}
</style>
