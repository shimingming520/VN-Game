<script setup>
import { computed, ref } from 'vue'
import { message, Modal } from 'ant-design-vue'

const props = defineProps({
  /** 父级提供的时间戳化图片地址方法,保证同一会话内 URL 稳定不闪 */
  getTimestampedImageUrl: { type: Function, required: true },
  published: { type: Boolean, default: false },
})

const emit = defineEmits(['select'])

const open = ref(false)
const sourceType = ref('history')
const targetType = ref('roles')
const selectedId = ref('')
const imageOptions = ref([])
const imageLoadStateMap = ref({})
const imageRetryMap = ref({})

const modalTitle = computed(() => (sourceType.value === 'history' ? '历史生图' : '素材库'))
const emptyDescription = computed(
  () => `暂无${targetType.value === 'roles' ? '人物' : '场景'}图片`,
)

function init(params) {
  const activeItem = (params.items ?? []).find((item) => item.isActive)
  imageLoadStateMap.value = {}
  imageRetryMap.value = {}
  sourceType.value = params.sourceType
  targetType.value = params.targetType
  imageOptions.value = params.items ?? []
  selectedId.value = activeItem?.id || params.selectedId || params.items?.[0]?.id || ''
  open.value = true
}

function getImageIdentity(item) {
  return `${item.id}-${item.image || ''}`
}

function getImageUrl(item) {
  const url = props.getTimestampedImageUrl(item.image)
  const retry = imageRetryMap.value[getImageIdentity(item)] || 0
  if (!url || !retry) return url
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}retry=${retry}`
}

function getImageStateKey(item) {
  return `${getImageIdentity(item)}-${getImageUrl(item)}`
}

function getImageLoadState(item) {
  return imageLoadStateMap.value[getImageStateKey(item)] || 'loading'
}

function handleImageError(item) {
  imageLoadStateMap.value[getImageStateKey(item)] = 'error'
}

function handleImageRetry(item) {
  const key = getImageIdentity(item)
  imageRetryMap.value[key] = (imageRetryMap.value[key] || 0) + 1
}

function handleCancel() {
  open.value = false
}

function handleSelect(item) {
  if (props.published) {
    message.warning('作品已发布，不可修改')
    return
  }
  Modal.confirm({
    title: '确认激活',
    content: '是否激活该版本？',
    okText: '确认激活',
    cancelText: '取消',
    onOk() {
      emit('select', item)
    },
  })
}

/** 激活成功后由父级回调置选中态 */
function setSelectedId(imageId) {
  selectedId.value = imageId
}

defineExpose({ init, setSelectedId })
</script>

<template>
  <a-modal
    :open="open"
    :title="modalTitle"
    :width="840"
    centered
    destroy-on-close
    :footer="null"
    :mask-closable="false"
    @cancel="handleCancel"
  >
    <div class="image-select__body">
      <a-empty v-if="!imageOptions.length" :description="emptyDescription" />

      <div v-else class="image-select__grid">
        <div
          v-for="item in imageOptions"
          :key="item.id"
          role="button"
          tabindex="0"
          class="image-select__card"
          :class="{ 'is-selected': selectedId === item.id }"
          @click="handleSelect(item)"
          @keydown.enter.prevent="handleSelect(item)"
          @keydown.space.prevent="handleSelect(item)"
        >
          <div
            class="image-select__cover"
            :class="targetType === 'scenes' ? 'is-scene' : 'is-role'"
          >
            <template v-if="item.image">
              <a-image
                :key="getImageStateKey(item)"
                :src="getImageUrl(item)"
                :alt="`${item.name}图片`"
                :preview="false"
                :wrapper-style="{ height: '100%', width: '100%' }"
                decoding="async"
                loading="lazy"
                @error="handleImageError(item)"
              >
                <template #placeholder>
                  <div class="image-select__mask">图片加载中…</div>
                </template>
              </a-image>

              <div v-if="getImageLoadState(item) === 'error'" class="image-select__mask">
                <span>图片加载失败</span>
                <button type="button" @click.stop="handleImageRetry(item)">重新加载</button>
              </div>
            </template>
            <span v-else class="image-select__empty">{{ item.name }}</span>
          </div>

          <span class="image-select__name" :title="item.name">{{ item.name }}</span>
          <span v-if="selectedId === item.id" class="image-select__badge">✓</span>
        </div>
      </div>
    </div>
  </a-modal>
</template>

<style scoped>
.image-select__body {
  max-height: 560px;
  min-height: 320px;
  overflow-y: auto;
  padding: 4px 6px 12px 0;
}

.image-select__grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 18px 16px;
}

.image-select__card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 8px;
  cursor: pointer;
  outline: none;
  text-align: center;
}

.image-select__cover {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  border: 2px solid #2a2d3a;
  border-radius: 8px;
  background: #10121a;
  transition: border-color 0.2s, box-shadow 0.2s;
}

.image-select__cover.is-role {
  aspect-ratio: 9 / 16;
}

.image-select__cover.is-scene {
  aspect-ratio: 16 / 9;
}

.image-select__card:hover .image-select__cover {
  border-color: #7166f0a8;
}

.image-select__card.is-selected .image-select__cover {
  border-color: #7166f0;
  box-shadow: 0 0 0 2px #7166f02e;
}

.image-select__cover :deep(.ant-image),
.image-select__cover :deep(.ant-image-img) {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: top;
}

.image-select__mask {
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

.image-select__mask button {
  padding: 3px 12px;
  border: 1px solid #7166f0;
  border-radius: 999px;
  background: transparent;
  color: #a99df8;
  cursor: pointer;
  font-size: 12px;
}

.image-select__empty {
  padding: 0 8px;
  color: #767c8f;
  font-size: 14px;
  font-weight: 600;
}

.image-select__name {
  overflow: hidden;
  color: #e7eaf5;
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.image-select__badge {
  position: absolute;
  top: -8px;
  right: -8px;
  display: flex;
  width: 26px;
  height: 26px;
  align-items: center;
  justify-content: center;
  border-radius: 999px;
  background: #7166f0;
  color: #fff;
  font-size: 14px;
}
</style>
