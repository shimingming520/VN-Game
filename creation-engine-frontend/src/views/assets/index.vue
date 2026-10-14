<script setup>
import { computed, ref, watch, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { getAssetsApi, hideAssetApi, saveAssetApi, getFoldersApi, createFolderApi } from '@/api'
import { prettifyAudioName, warmAudioNames } from '@/utils/audioName'

// 与 demo 素材工作区 1:1:标题「素材」+ 分类 tab(角色/场景/BGM/音效)
// 每个素材仅两个操作:删除(对本用户软删除)、保存到我的资产。无筛选/搜索/分页。
const CATEGORIES = [
  { id: 'character', label: '角色', kind: 'image' },
  { id: 'scene', label: '场景', kind: 'image' },
  { id: 'music', label: 'BGM', kind: 'audio' },
  { id: 'sound', label: '音效', kind: 'audio' },
]

const route = useRoute()
const router = useRouter()

const activeCategory = computed(() => {
  const c = route.params.category
  return CATEGORIES.some((x) => x.id === c) ? c : 'character'
})
const isImage = computed(() => CATEGORIES.find((c) => c.id === activeCategory.value)?.kind === 'image')

const loading = ref(false)
const items = ref([])
const playingId = ref(null)

// 保存到我的资产:选择目标文件夹
const folders = ref([])
const saveModal = ref({ open: false, item: null, folderId: null })

const folderOptions = computed(() => [
  { value: null, label: '未分类' },
  ...folders.value.map((f) => ({ value: f.id, label: f.name })),
])

async function loadFolders() {
  try {
    folders.value = await getFoldersApi()
  } catch {
    folders.value = []
  }
}

async function fetchList() {
  loading.value = true
  try {
    const res = await getAssetsApi({ category: activeCategory.value, page: 1, page_size: 200 })
    items.value = res.items || []
    // 音频分类(BGM/音效)送后端 LLM 翻译文件名
    if (!isImage.value) warmAudioNames(items.value.map((i) => i.name))
  } finally {
    loading.value = false
  }
}

function switchCategory(id) {
  if (id === activeCategory.value) return
  router.push(`/assets/${id}`)
}

async function handleHide(item) {
  try {
    await hideAssetApi(item.key)
    items.value = items.value.filter((i) => i.key !== item.key)
    message.success(`已删除“${item.name}”`)
  } catch {
    message.error('删除失败')
  }
}

function handleSave(item) {
  saveModal.value = { open: true, item, folderId: null }
}

async function confirmSave() {
  const { item, folderId } = saveModal.value
  try {
    await saveAssetApi(item.key, folderId)
    saveModal.value.open = false
    message.success(`已保存“${item.name}”到我的资产`)
  } catch {
    message.error('保存失败')
  }
}

async function createFolderInline() {
  const input = window.prompt('新建文件夹', '未命名文件夹')
  if (input === null) return
  try {
    const folder = await createFolderApi(input.trim() || '未命名文件夹')
    folders.value.unshift(folder)
    saveModal.value.folderId = folder.id
    message.success('已创建文件夹')
  } catch {
    message.error('创建文件夹失败')
  }
}

watch(activeCategory, fetchList, { immediate: true })
onMounted(loadFolders)
</script>

<template>
  <section class="material-workspace">
    <div class="material-workspace-heading">
      <h2>素材</h2>
    </div>

    <div class="generation-history material-history" :class="{ 'is-empty': !items.length }">
      <div class="generation-history-toolbar">
        <div class="generation-category-tabs" role="tablist" aria-label="生成内容分类">
          <button
            v-for="c in CATEGORIES"
            :key="c.id"
            type="button"
            :class="{ active: activeCategory === c.id }"
            :aria-selected="activeCategory === c.id"
            @click="switchCategory(c.id)"
          >
            {{ c.label }}
          </button>
        </div>
      </div>

      <a-spin :spinning="loading">
        <template v-if="items.length">
          <!-- 图片素材:网格卡片 -->
          <div v-if="isImage" class="asset-history-grid" :class="{ 'scene-grid': activeCategory === 'scene' }">
            <article
              v-for="item in items"
              :key="item.key"
              class="asset-history-card"
              :class="{ landscape: activeCategory === 'scene' }"
            >
              <img :src="item.url" :alt="item.name" />
              <div class="asset-history-actions">
                <button type="button" @click="handleHide(item)">删除</button>
                <button type="button" @click="handleSave(item)">保存到我的资产</button>
              </div>
            </article>
          </div>

          <!-- 音频素材:行列表 -->
          <div v-else class="audio-history-list">
            <article v-for="item in items" :key="item.key" class="audio-history-row">
              <div class="audio-history-name">
                <strong>{{ prettifyAudioName(item.name) }}</strong>
                <span class="audio-raw-name">{{ item.name }}</span>
              </div>
              <div class="audio-history-player">
                <audio :src="item.url" controls preload="none" />
              </div>
              <div class="audio-history-actions">
                <button type="button" @click="handleHide(item)">删除</button>
                <button type="button" @click="handleSave(item)">保存到我的资产</button>
              </div>
            </article>
          </div>
        </template>

        <div v-else-if="!loading" class="generation-history-empty">暂无素材内容</div>
      </a-spin>
    </div>

    <!-- 保存到我的资产:选文件夹 -->
    <a-modal
      v-model:open="saveModal.open"
      title="保存到我的资产"
      ok-text="保存"
      cancel-text="取消"
      @ok="confirmSave"
    >
      <div style="display: flex; gap: 8px; align-items: center">
        <a-select
          v-model:value="saveModal.folderId"
          style="flex: 1"
          :options="folderOptions"
          placeholder="选择目标文件夹"
        />
        <a-button @click="createFolderInline">新建文件夹</a-button>
      </div>
    </a-modal>
  </section>
</template>

<style scoped>
/* 场景为横版素材,减少每行列数让卡片更宽,避免竖版留白 */
.asset-history-grid.scene-grid {
  grid-template-columns: repeat(4, minmax(0, 1fr));
}
/* 音频原始文件名(中文名下方小字) */
.audio-raw-name {
  display: block;
  margin-top: 2px;
  color: #6f7582;
  font-size: 11px;
}
</style>
