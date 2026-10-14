<script setup>
import { ref, reactive, computed, watch, createVNode, h } from 'vue'
import { useRoute } from 'vue-router'
import dayjs from 'dayjs'
import { message, Modal } from 'ant-design-vue'
import { DownloadOutlined, ExclamationCircleOutlined } from '@ant-design/icons-vue'
import { getAssetsApi, assetDownloadUrl, cleanAssetsApi } from '@/api'

const CATEGORY_META = {
  character: { label: '角色', kind: 'image' },
  scene: { label: '场景', kind: 'image' },
  bgm: { label: 'BGM', kind: 'audio' },
  sfx: { label: '音效', kind: 'audio' },
}

const route = useRoute()
const category = computed(() => route.params.category)
const meta = computed(() => CATEGORY_META[category.value] || { label: category.value, kind: 'image' })
const isImage = computed(() => meta.value.kind === 'image')

const loading = ref(false)
const items = ref([])
const total = ref(0)

const filters = reactive({
  name: '',
  timeRange: [],
})

const pagination = reactive({
  current: 1,
  pageSize: 24,
})

/** 按日期(YYYY-MM-DD)分组当前页素材,图片网格用 */
const dateGroups = computed(() => {
  const groups = []
  const index = {}
  for (const item of items.value) {
    const date = dayjs(item.last_modified).format('YYYY-MM-DD')
    if (index[date] === undefined) {
      index[date] = groups.length
      groups.push({ date, items: [] })
    }
    groups[index[date]].items.push(item)
  }
  return groups
})

function formatSize(bytes) {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${bytes} B`
}

async function fetchList() {
  if (!CATEGORY_META[category.value]) return
  loading.value = true
  try {
    const [start_time, end_time] = filters.timeRange || []
    const res = await getAssetsApi({
      category: category.value,
      name: filters.name || undefined,
      start_time: start_time ? dayjs(start_time).format('YYYY-MM-DD HH:mm:ss') : undefined,
      end_time: end_time ? dayjs(end_time).format('YYYY-MM-DD HH:mm:ss') : undefined,
      page: pagination.current,
      page_size: pagination.pageSize,
    })
    items.value = res.items
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
  filters.name = ''
  filters.timeRange = []
  handleSearch()
}

function handlePageChange(page, pageSize) {
  pagination.current = page
  pagination.pageSize = pageSize
  fetchList()
}

/** 下载:走后端代理端点强制 attachment */
function handleDownload(item) {
  const a = document.createElement('a')
  a.href = assetDownloadUrl(item.key)
  a.download = item.name
  a.click()
}

// ==================== 按时间范围清理本地素材,先预览再确认 ====================
const cleaning = ref(false)

async function handleClean() {
  const [start_time, end_time] = filters.timeRange || []
  const payload = {
    category: category.value,
    start_time: start_time ? dayjs(start_time).format('YYYY-MM-DD HH:mm:ss') : undefined,
    end_time: end_time ? dayjs(end_time).format('YYYY-MM-DD HH:mm:ss') : undefined,
  }
  cleaning.value = true
  let preview
  try {
    preview = await cleanAssetsApi({ ...payload, dry_run: true })
  } finally {
    cleaning.value = false
  }
  if (!preview.count) {
    message.info('该时间范围内没有可清理的素材')
    return
  }
  const rangeText = start_time || end_time
    ? `${payload.start_time || '最早'} ~ ${payload.end_time || '现在'}`
    : '全部时间(未选时间范围,将清空该分类!)'
  Modal.confirm({
    title: `确认清理「${meta.value.label}」素材?`,
    icon: createVNode(ExclamationCircleOutlined),
    content: `时间范围:${rangeText},共 ${preview.count} 个文件。将直接从本地存储删除,不可恢复!`,
    okText: `删除 ${preview.count} 个`,
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      const res = await cleanAssetsApi({ ...payload, dry_run: false })
      message.success(`已清理 ${res.count} 个素材`)
      pagination.current = 1
      fetchList()
    },
  })
}

/** 切换分类时重置筛选并重新拉取 */
watch(
  category,
  () => {
    filters.name = ''
    filters.timeRange = []
    pagination.current = 1
    pagination.pageSize = isImage.value ? 24 : 20
    fetchList()
  },
  { immediate: true },
)
</script>

<template>
  <div class="assets">
    <div class="dr-page-head">
      <div>
        <div class="dr-page-title">素材库 · {{ meta.label }}</div>
        <div class="dr-page-desc">全平台产出素材归档 · 按日期分组,可预览下载与按期清理</div>
      </div>
    </div>

    <a-card :bordered="false" class="assets__filter">
      <a-space wrap>
        <a-input
          v-model:value="filters.name"
          placeholder="文件名模糊查询"
          style="width: 200px"
          allow-clear
          @press-enter="handleSearch"
        />
        <a-range-picker v-model:value="filters.timeRange" show-time />
        <a-button type="primary" @click="handleSearch">查询</a-button>
        <a-button @click="handleReset">重置</a-button>
        <a-button danger :loading="cleaning" @click="handleClean">
          按时间范围清理(本地)
        </a-button>
      </a-space>
    </a-card>

    <a-card :bordered="false" class="assets__content">
      <a-spin :spinning="loading">
        <a-empty v-if="!loading && !items.length" description="暂无素材" />

        <!-- 图片类:按日期分组网格 + 预览 -->
        <template v-if="isImage">
          <div v-for="group in dateGroups" :key="group.date" class="assets__group">
            <div class="dr-group-title">{{ group.date }}</div>
            <a-image-preview-group>
              <div class="assets__grid">
                <div v-for="item in group.items" :key="item.key" class="assets__cell">
                  <a-image :src="item.url" :alt="item.name" class="assets__img" />
                  <a-tooltip :title="`${item.name}(${formatSize(item.size)})`">
                    <div class="assets__name">{{ item.name }}</div>
                  </a-tooltip>
                  <a-button
                    type="link"
                    size="small"
                    :icon="h(DownloadOutlined)"
                    @click="handleDownload(item)"
                  >
                    下载
                  </a-button>
                </div>
              </div>
            </a-image-preview-group>
          </div>
        </template>

        <!-- 音频类:列表 + 原生播放器 -->
        <template v-else>
          <div v-for="item in items" :key="item.key" class="assets__audio-row">
            <div class="assets__audio-info">
              <a-tooltip :title="item.key">
                <span class="assets__audio-name">{{ item.name }}</span>
              </a-tooltip>
              <span class="assets__audio-meta">
                {{ formatSize(item.size) }} · {{ dayjs(item.last_modified).format('YYYY-MM-DD HH:mm') }}
              </span>
            </div>
            <div class="assets__audio-actions">
              <audio :src="item.url" controls preload="none" class="assets__audio-player" />
              <a-button
                type="link"
                size="small"
                :icon="h(DownloadOutlined)"
                @click="handleDownload(item)"
              >
                下载
              </a-button>
            </div>
          </div>
        </template>

        <div class="assets__pagination">
          <a-pagination
            :current="pagination.current"
            :page-size="pagination.pageSize"
            :total="total"
            :page-size-options="['12', '24', '48', '96']"
            show-size-changer
            :show-total="(t) => `共 ${t} 条`"
            @change="handlePageChange"
          />
        </div>
      </a-spin>
    </a-card>
  </div>
</template>

<style scoped>
.assets__filter {
  margin-bottom: 16px;
}

.assets__group {
  margin-bottom: 20px;
}

.assets__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 12px;
}

.assets__cell {
  text-align: center;
}

.assets__cell :deep(.ant-image) {
  width: 100%;
}

.assets__cell :deep(.ant-image-img) {
  width: 100%;
  height: 140px;
  object-fit: cover;
  border: 1px solid var(--dr-border);
  border-radius: 8px;
  background: #0f1420;
  transition: border-color 0.18s ease, box-shadow 0.18s ease;
}

.assets__cell:hover :deep(.ant-image-img) {
  border-color: rgba(232, 182, 76, 0.5);
  box-shadow: var(--dr-glow-gold);
}

.assets__name {
  margin-top: 4px;
  font-size: 12px;
  color: var(--dr-text-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.assets__audio-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 8px;
  padding: 10px 14px;
  border: 1px solid var(--dr-border);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.02);
  transition: border-color 0.18s ease, background 0.18s ease;
}

.assets__audio-row:hover {
  border-color: rgba(53, 214, 208, 0.4);
  background: rgba(53, 214, 208, 0.05);
}

.assets__audio-info {
  min-width: 0;
  flex: 1;
  display: flex;
  flex-direction: column;
}

.assets__audio-name {
  font-size: 14px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.assets__audio-meta {
  font-size: 12px;
  color: var(--dr-text-3);
}

.assets__audio-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.assets__audio-player {
  height: 36px;
  width: 320px;
  /* 原生播放器是浅色控件,反色压进深色底 */
  filter: invert(0.88) hue-rotate(180deg) saturate(0.85);
}

.assets__pagination {
  margin-top: 16px;
  text-align: right;
}
</style>
