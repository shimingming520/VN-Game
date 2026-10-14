<script setup>
import { computed, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { confirmMaterialApi, getTaskCharactersApi, getTaskScenesApi } from '@/api'
import AssetModal from './AssetModal.vue'

const props = defineProps({
  taskId: { type: String, default: '' },
  published: { type: Boolean, default: false },
})

const emit = defineEmits(['confirmed'])

const TABS = [
  { name: '全部角色', key: 'roles' },
  { name: '全部场景', key: 'scenes' },
]

const loading = ref(false)
/** 角色或场景列表接口是否请求失败 */
const hasFetchError = ref(false)
const fetchErrorText = ref('')
const confirmLoading = ref(false)
/** 素材还没生成好(后端返回 pending)时只让刷新,不让往下走 */
const isFreshShow = ref(false)
const activeTab = ref('roles')
const scrollContainer = ref(null)
const assetModalRef = ref(null)

const roles = ref([])
const scenes = ref([])

/** 图片加载状态 / 单图重试次数 */
const imageLoadStateMap = ref({})
const imageRetryMap = ref({})

// 同一会话内复用一个时间戳:每次渲染都换 ?t= 会让浏览器彻底不缓存,列表滚动就一直闪
const imageTimestampStorageKey = `taskCreationNewAssetImageTimestamp:${props.taskId || 'default'}`
const imageTimestamp = ref(getInitialImageTimestamp())

const tabs = TABS
const currentList = computed(() => (activeTab.value === 'roles' ? roles.value : scenes.value))

function getTabCount(key) {
  return key === 'roles' ? roles.value.length : scenes.value.length
}

function unwrap(res) {
  return res?.data ?? res
}

function requestErrorText(error, fallback) {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return `${fallback}：${detail.trim()}`
  return `${fallback}，请稍后重试`
}

/** 从素材节点汇总人物实际出现的集数。 */
function collectEpisodeNos(items) {
  const episodes = new Set()
  for (const item of items ?? []) {
    for (const episode of item?.episode_nos ?? []) {
      if (episode !== null && episode !== undefined && String(episode).trim()) {
        episodes.add(String(episode).trim())
      }
    }
    const nodeMatch = String(item?.node_id ?? '').match(/^chapter_(\d+)(?:_|$)/)
    if (nodeMatch) episodes.add(nodeMatch[1])
  }
  return [...episodes].sort((left, right) => {
    const leftNumber = Number(left)
    const rightNumber = Number(right)
    if (Number.isFinite(leftNumber) && Number.isFinite(rightNumber)) {
      return leftNumber - rightNumber
    }
    return String(left).localeCompare(String(right), 'zh-CN', { numeric: true })
  })
}

/**
 * 角色清单适配:后端按 items[] 平铺(同一角色的每个情绪 × 景别各一条),
 * 前端要「一个 speaker_id 一张主卡,点开看情绪变体,不暴露景别」。
 * 景别只在剧情演绎页调,这里把同情绪的多个景别折叠成一个变体。
 */
function normalizeCharacters(list) {
  return (list ?? []).map((character) => {
    const items = character.items ?? []
    const episodeNos = collectEpisodeNos(items)
    const byEmotion = new Map()
    for (const item of items) {
      const emotion = item.emotion || 'calm'
      const existing = byEmotion.get(emotion)
      // 同情绪多景别时封面取 LS(全景),它最能看清整体形象
      const preferLs = String(item.shot || '').toUpperCase() === 'LS'
      if (!existing || (preferLs && !existing.isLs)) {
        byEmotion.set(emotion, {
          status: emotion,
          image: item.url || '',
          material_id: item.material_id,
          node_id: item.node_id,
          combination: item.combination,
          episode_nos: episodeNos,
          // 提示词按 combination 落库,对该组合的所有情绪/景别一起生效
          positive_prompt: item.prompt || '',
          isLs: preferLs,
        })
      }
    }
    const variants = [...byEmotion.values()]
    const cover = variants.find((v) => v.status === 'calm') ?? variants[0]
    return {
      id: character.speaker_id,
      speaker_id: character.speaker_id,
      name: character.name || character.speaker_id,
      image: cover?.image || '',
      episode_nos: episodeNos,
      status: variants,
    }
  })
}

/** 场景 + 章节封面:两类素材重生成入口不同,用 material_type 区分 */
function normalizeScenes(data) {
  const out = []
  for (const scene of data?.scenes ?? []) {
    const episodeNos = collectEpisodeNos([scene])
    out.push({
      id: scene.scene_id ?? scene.material_id,
      name: scene.name || scene.scene_id || '场景',
      image: scene.url || '',
      material_type: 'scene',
      scene_id: scene.scene_id,
      node_id: scene.node_id,
      material_id: scene.material_id,
      episode_nos: episodeNos,
      positive_prompt: scene.prompt || '',
    })
  }
  for (const bg of data?.backgrounds ?? []) {
    const episodeNos = collectEpisodeNos([bg])
    out.push({
      id: `bg-${bg.node_id ?? bg.material_id}`,
      name: bg.title || bg.name || '章节封面',
      image: bg.url || '',
      material_type: 'background',
      node_id: bg.node_id,
      material_id: bg.material_id,
      episode_nos: episodeNos,
      positive_prompt: bg.prompt || '',
    })
  }
  return out
}

onMounted(() => {
  fetchAllData()
})

async function fetchAllData(forceImageRefresh = false) {
  if (!props.taskId) return
  loading.value = true
  hasFetchError.value = false
  fetchErrorText.value = ''
  isFreshShow.value = false
  if (forceImageRefresh) {
    imageTimestamp.value = Date.now()
    sessionStorage.setItem(imageTimestampStorageKey, String(imageTimestamp.value))
    imageLoadStateMap.value = {}
    imageRetryMap.value = {}
  }
  try {
    const [rolesRes, scenesRes] = await Promise.all([
      getTaskCharactersApi(props.taskId, {}, { skipErrorToast: true }),
      getTaskScenesApi(props.taskId, {}, { skipErrorToast: true }),
    ])
    const rolesData = unwrap(rolesRes)
    const scenesData = unwrap(scenesRes)

    if (rolesData?.status === 'pending') isFreshShow.value = true
    else roles.value = normalizeCharacters(rolesData?.characters)

    if (scenesData?.status === 'pending') isFreshShow.value = true
    else scenes.value = normalizeScenes(scenesData)
  } catch (error) {
    hasFetchError.value = true
    fetchErrorText.value = requestErrorText(error, '读取素材清单失败')
    roles.value = []
    scenes.value = []
  } finally {
    loading.value = false
  }
}

function handleSuccess() {
  handleFresh()
}

function handleFresh() {
  fetchAllData(true)
}

function getInitialImageTimestamp() {
  const stored = Number(sessionStorage.getItem(imageTimestampStorageKey))
  if (stored) return stored
  const initial = Date.now()
  sessionStorage.setItem(imageTimestampStorageKey, String(initial))
  return initial
}

function getTimestampedImageUrl(imageUrl) {
  if (!imageUrl) return ''
  if (!imageTimestamp.value) return imageUrl
  const sep = imageUrl.includes('?') ? '&' : '?'
  return `${imageUrl}${sep}t=${imageTimestamp.value}`
}

function getImageIdentity(item, index) {
  return `${activeTab.value}-${item.id ?? index}-${item.image || ''}`
}

function getListImageUrl(item, index) {
  const url = getTimestampedImageUrl(item.image)
  const retry = imageRetryMap.value[getImageIdentity(item, index)] || 0
  if (!url || !retry) return url
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}retry=${retry}`
}

function getImageStateKey(item, index) {
  return `${getImageIdentity(item, index)}-${getListImageUrl(item, index)}`
}

function getImageLoadState(item, index) {
  return imageLoadStateMap.value[getImageStateKey(item, index)] || 'loading'
}

function handleImageError(item, index) {
  imageLoadStateMap.value[getImageStateKey(item, index)] = 'error'
}

function handleImageRetry(item, index) {
  const key = getImageIdentity(item, index)
  imageRetryMap.value[key] = (imageRetryMap.value[key] || 0) + 1
}

function handleTabChange() {
  if (scrollContainer.value) scrollContainer.value.scrollTop = 0
}

function handleSelect(item) {
  assetModalRef.value?.init(activeTab.value, currentList.value, item.id)
}

async function handleNextStep() {
  if (!props.taskId) {
    message.warning('当前任务不存在，无法确认素材')
    return
  }
  if (props.published) {
    message.warning('作品已发布，不可修改')
    return
  }
  confirmLoading.value = true
  try {
    await confirmMaterialApi(props.taskId)
    emit('confirmed')
  } catch (error) {
    message.error(`确认人物场景素材失败：${error?.message ?? error}`)
  } finally {
    confirmLoading.value = false
  }
}
</script>

<template>
  <section class="create-panel roles-scenes">
    <a-tabs v-model:activeKey="activeTab" type="card" @change="handleTabChange">
      <a-tab-pane v-for="tab in tabs" :key="tab.key" :tab="`${tab.name} (${getTabCount(tab.key)})`" />
    </a-tabs>

    <div v-if="loading" class="roles-scenes__loading">
      <a-spin size="large" />
    </div>

    <div v-else ref="scrollContainer" class="roles-scenes__body">
      <div v-if="hasFetchError" class="roles-scenes__error">
        <a-alert :message="fetchErrorText" type="warning" show-icon />
        <a-button @click="handleFresh">重新加载</a-button>
      </div>

      <p v-else-if="!currentList.length" class="create-empty">
        创作任务处理中，请稍后点击刷新重试…
      </p>

      <div v-else class="roles-scenes__grid">
        <div
          v-for="(item, index) in currentList"
          :key="`${activeTab}-${item.id ?? index}`"
          class="asset-card"
          @click="handleSelect(item)"
        >
          <div class="asset-card__cover" :class="activeTab === 'scenes' ? 'is-scene' : 'is-role'">
            <template v-if="item.image">
              <a-image
                :key="getImageStateKey(item, index)"
                :src="getListImageUrl(item, index)"
                :alt="item.name || (activeTab === 'scenes' ? '场景图片' : '角色图片')"
                :preview="false"
                :wrapper-style="{ height: '100%', width: '100%' }"
                decoding="async"
                loading="lazy"
                @error="handleImageError(item, index)"
              >
                <template #placeholder>
                  <div class="asset-card__mask">图片加载中…</div>
                </template>
              </a-image>

              <div v-if="getImageLoadState(item, index) === 'error'" class="asset-card__mask">
                <span>图片加载失败</span>
                <button type="button" @click.stop="handleImageRetry(item, index)">重新加载</button>
              </div>
            </template>
            <span v-else class="asset-card__empty">暂无图片</span>
          </div>

          <a-typography-paragraph
            :content="item.name"
            :ellipsis="{ rows: 1, css: true, expandable: false }"
            class="asset-card__name"
          />
          <div v-if="activeTab === 'roles'" class="asset-card__meta">
            共 {{ item.status?.length || 0 }} 个形象
          </div>
        </div>
      </div>
    </div>

    <div class="create-panel__foot">
      <a-button :disabled="loading" :loading="loading" @click="handleFresh">刷新</a-button>
      <a-button
        type="primary"
        :disabled="loading || isFreshShow || hasFetchError || confirmLoading || published"
        :loading="confirmLoading"
        @click="handleNextStep"
      >
        下一步
      </a-button>
    </div>

    <AssetModal
      ref="assetModalRef"
      :task-id="taskId"
      :published="published"
      :get-timestamped-image-url="getTimestampedImageUrl"
      @success="handleSuccess"
    />
  </section>
</template>

<style scoped>
@import '../../styles/panel.css';

.roles-scenes__loading {
  display: flex;
  min-height: 320px;
  align-items: center;
  justify-content: center;
}

.roles-scenes__body {
  max-height: 540px;
  overflow-y: auto;
  padding: 2px;
}

.roles-scenes__grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
}

.asset-card {
  overflow: hidden;
  padding: 10px;
  border: 1px solid rgba(145, 158, 205, 0.14);
  border-radius: 14px;
  background: rgba(30, 35, 48, 0.66);
  cursor: pointer;
  transition: transform 0.18s ease, border-color 0.18s ease, background 0.18s ease;
}

.asset-card:hover {
  border-color: rgba(137, 155, 232, 0.48);
  background: rgba(39, 45, 62, 0.78);
  transform: translateY(-2px);
}

.asset-card__cover {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  margin-bottom: 10px;
  border: 1px solid rgba(145, 158, 205, 0.16);
  border-radius: 10px;
  background: rgba(11, 14, 21, 0.72);
  transition: border-color 0.2s, box-shadow 0.2s;
}

.asset-card:hover .asset-card__cover {
  border-color: rgba(137, 155, 232, 0.48);
  box-shadow: 0 0 0 3px rgba(113, 132, 224, 0.09);
}

.asset-card__cover.is-role {
  aspect-ratio: 3 / 4;
}

.asset-card__cover.is-scene {
  aspect-ratio: 16 / 9;
}

.asset-card__cover :deep(.ant-image),
.asset-card__cover :deep(.ant-image-img) {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: top;
}

.asset-card__mask {
  position: absolute;
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: center;
  justify-content: center;
  inset: 0;
  background: #10121aee;
  color: #8a91a8;
  font-size: 12px;
}

.asset-card__mask button {
  padding: 3px 12px;
  border: 1px solid #7166f0;
  border-radius: 999px;
  background: transparent;
  color: #a99df8;
  cursor: pointer;
  font-size: 12px;
}

.asset-card__empty {
  color: #767c8f;
  font-size: 13px;
}

.asset-card__name {
  margin-bottom: 0 !important;
  color: #e6e9f2;
  font-size: 14px;
  font-weight: 500;
}

.asset-card__meta {
  margin-top: 3px;
  color: #858da2;
  font-size: 12px;
}

.roles-scenes__error {
  display: grid;
  min-height: 260px;
  place-content: center;
  justify-items: center;
  gap: 14px;
}
</style>
