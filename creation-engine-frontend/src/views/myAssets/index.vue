<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import dayjs from 'dayjs'
import { message } from 'ant-design-vue'
import { prettifyAudioName, warmAudioNames } from '@/utils/audioName'
import {
  getMyAssetsApi,
  getFoldersApi,
  createFolderApi,
  deleteFolderApi,
  getSavedMaterialsApi,
  moveSavedMaterialApi,
  copySavedMaterialApi,
  deleteSavedMaterialApi,
  saveAssetApi,
  getAssetsApi,
} from '@/api'

const TABS = [
  { id: 'assets', label: '资产' },
  { id: 'history', label: '生成历史' },
  { id: 'canvas', label: '画布' },
]

const activeTab = ref('assets')
const loading = ref(false)
const tasks = ref([])
const userFolders = ref([]) // [{id, name, created_at}]
const savedMaterials = ref([]) // [{id, folder_id, key, name, url}]
const openMenuId = ref(null)

// 视图:grid=文件夹网格, folder=打开某文件夹
const view = ref('grid')
const openedFolder = ref(null) // {id, name} id=null 表示未分类

// 移动/复制弹窗
const transferModal = ref({ open: false, mode: 'move', material: null, targetId: null })

function isImage(k) {
  return /\.(png|jpe?g|webp|gif|bmp)$/i.test(k || '')
}

// 与后端 CATEGORY_PREFIX 对应:按 key 前缀归类已保存素材
const MATERIAL_CATEGORIES = [
  { id: 'character', label: '角色', prefix: 'image-backups/character/' },
  { id: 'scene', label: '场景', prefix: 'image-backups/sence/' },
  { id: 'music', label: 'BGM', prefix: 'sound-backups/bgm/' },
  { id: 'sound', label: '音效', prefix: 'sound-backups/sfx/' },
]
function categoryOf(key) {
  const hit = MATERIAL_CATEGORIES.find((c) => (key || '').startsWith(c.prefix))
  return hit ? hit.id : (historyCategoryOf(key) || 'character')
}
const folderCategory = ref('character')
function formatSize(b) {
  if (b >= 1024 * 1024) return `${(b / 1024 / 1024).toFixed(1)} MB`
  if (b >= 1024) return `${(b / 1024).toFixed(0)} KB`
  return `${b} B`
}

// 已保存素材按 folder_id 分组
const savedByFolder = computed(() => {
  const map = new Map()
  for (const m of savedMaterials.value) {
    const k = m.folder_id ?? '__none__'
    if (!map.has(k)) map.set(k, [])
    map.get(k).push(m)
  }
  return map
})

// 资产网格里的文件夹卡片
const folders = computed(() => {
  const list = []

  // 未分类(folder_id 为空的已保存素材)
  const none = savedByFolder.value.get('__none__') || []
  if (none.length) {
    list.push({
      id: '__none__',
      realId: null,
      name: '未分类',
      date: '',
      count: none.length,
      images: none.filter((m) => isImage(m.key)).slice(0, 3).map((m) => m.url),
      kind: 'saved',
      removable: false,
    })
  }

  // 用户自建文件夹
  for (const f of userFolders.value) {
    const inFolder = savedByFolder.value.get(f.id) || []
    list.push({
      id: `folder-${f.id}`,
      realId: f.id,
      name: f.name,
      date: f.created_at ? dayjs(f.created_at).format('YYYY-MM-DD') : '',
      count: inFolder.length,
      images: inFolder.filter((m) => isImage(m.key)).slice(0, 3).map((m) => m.url),
      kind: 'saved',
      removable: true,
    })
  }

  // 生产任务不再以文件夹形式出现在"资产"tab,统一在"生成历史"里展示
  return list
})

// 打开的文件夹里的内容
const openedMaterials = computed(() => {
  if (!openedFolder.value || openedFolder.value.kind !== 'saved') return []
  const key = openedFolder.value.realId ?? '__none__'
  return savedByFolder.value.get(key) || []
})
// 文件夹内按分类统计与筛选
const folderCategoryCounts = computed(() => {
  const counts = { character: 0, scene: 0, music: 0, sound: 0 }
  for (const m of openedMaterials.value) counts[categoryOf(m.key)]++
  return counts
})
const openedMaterialsInCategory = computed(() =>
  openedMaterials.value.filter((m) => categoryOf(m.key) === folderCategory.value)
)
const isSceneCategory = computed(() => folderCategory.value === 'scene')
const isImageCategory = computed(() => folderCategory.value === 'character' || folderCategory.value === 'scene')

// 可作为移动/复制目标的文件夹(排除当前所在夹)
const targetFolderOptions = computed(() => {
  const opts = [{ value: null, label: '未分类' }]
  for (const f of userFolders.value) opts.push({ value: f.id, label: f.name })
  const cur = openedFolder.value?.realId ?? null
  return opts.filter((o) => o.value !== cur)
})

// 生成历史:全部任务文件平铺
const historyFiles = computed(() => {
  const out = []
  for (const t of tasks.value) {
    for (const f of t.files || []) out.push({ ...f, project: t.project_name || t.task_id })
  }
  return out
})

// 生成历史分类:按产出文件的 OSS 路径段判定(与素材库前缀不同,单独匹配)
const HISTORY_CATEGORIES = [
  { id: 'character', label: '角色', kind: 'image' },
  { id: 'scene', label: '场景', kind: 'image' },
  { id: 'music', label: 'BGM', kind: 'audio' },
  { id: 'sound', label: '音效', kind: 'audio' },
]
// 游戏包工程里的样板文件(引擎/脚本/样式/字体/字幕/应用图标等)不算产出素材,排除
// 排除:引擎/脚本/样式/字体/字幕/应用图标/配音(voices)/环境底音(ambience)/视频等,都不算展示素材
function isBoilerplate(key) {
  const k = (key || '').toLowerCase()
  if (/\/(engine|js|style|styles|fonts|ui|gallery|videos|subtitles?|icons?|images|voices?|ambience)\//.test(k)) return true
  if (/-subtitle\//.test(k) || /-audio\//.test(k)) return true
  if (/\.(js|css|html|json|map|ttf|woff2?|otf|ico|icns|txt|srt|vtt|gitignore)$/.test(k)) return true
  return false
}
// 按真实产出目录归类(OSS: {task_id}/chapter_x/assets/{music,scenes,characters,sounds})
// 四类与文件夹/素材库一致:角色/场景/BGM(music)/音效(sounds,含sfx)。ambience/voices 已在上面排除。
function historyCategoryOf(key) {
  const k = (key || '').toLowerCase()
  if (/\/characters?\//.test(k) || k.includes('image-backups/character')) return 'character'
  if (/\/(scenes?|sence|backgrounds?|cg)\//.test(k) || k.includes('image-backups/sence')) return 'scene'
  if (/\/music\//.test(k) || /\/bgm\//.test(k)) return 'music'
  if (/\/(sounds?|sfx)\//.test(k)) return 'sound'
  return null
}
const historyCategory = ref('character')
const historyByCategory = computed(() => {
  const map = { character: [], scene: [], music: [], sound: [] }
  for (const f of historyFiles.value) {
    if (isBoilerplate(f.key)) continue
    const cat = historyCategoryOf(f.key)
    if (cat && map[cat]) map[cat].push(f)
  }
  return map
})
const historyTabs = computed(() =>
  HISTORY_CATEGORIES.filter((c) => historyByCategory.value[c.id].length)
)
const historyCurrentKind = computed(
  () => HISTORY_CATEGORIES.find((c) => c.id === historyCategory.value)?.kind || 'file'
)
// 生成历史时间搜索(range 为 dayjs 数组)
const historyRange = ref([])
function fileTime(f) {
  // last_modified 为 epoch 秒
  return f.last_modified ? dayjs.unix(f.last_modified) : null
}
const historyCurrentFiles = computed(() => {
  let list = historyByCategory.value[historyCategory.value] || []
  if (historyRange.value?.length === 2) {
    const [s, e] = historyRange.value
    const start = s.startOf('day')
    const end = e.endOf('day')
    list = list.filter((f) => {
      const t = fileTime(f)
      return t && !t.isBefore(start) && !t.isAfter(end)
    })
  }
  return list
})
// 当前分类文件按日期分组:今天 / 近7天 / 更早,组内按时间倒序
// 按具体日期分组(如 2026-07-20),日期倒序,组内按时间倒序
const historyGroups = computed(() => {
  const map = new Map() // 'YYYY-MM-DD' -> files[]
  for (const f of historyCurrentFiles.value) {
    const t = fileTime(f)
    const key = t ? t.format('YYYY-MM-DD') : '未知时间'
    if (!map.has(key)) map.set(key, [])
    map.get(key).push(f)
  }
  return [...map.entries()]
    .sort((a, b) => (a[0] < b[0] ? 1 : a[0] > b[0] ? -1 : 0)) // 日期字符串倒序,未知时间排最后
    .map(([label, files]) => ({
      key: label,
      label,
      files: files.sort((a, b) => (b.last_modified || 0) - (a.last_modified || 0)),
    }))
})
// 切到生成历史时,自动定位到第一个有内容的分类
watch([() => activeTab.value, historyTabs], () => {
  if (activeTab.value !== 'history') return
  if (!historyTabs.value.some((c) => c.id === historyCategory.value)) {
    historyCategory.value = historyTabs.value[0]?.id || 'character'
  }
}, { immediate: true })

async function fetchAll() {
  loading.value = true
  try {
    const [assetsRes, foldersRes, savedRes] = await Promise.all([
      getMyAssetsApi(),
      getFoldersApi(),
      getSavedMaterialsApi(),
    ])
    tasks.value = assetsRes.items || []
    userFolders.value = foldersRes || []
    savedMaterials.value = savedRes || []
    warmAudio()
  } finally {
    loading.value = false
  }
}

// 把列表里的音频名(BGM/音效)送后端 LLM 翻译,结果到达后模板自动刷新
function warmAudio() {
  const names = []
  for (const t of tasks.value) {
    for (const f of t.files || []) {
      if (isBoilerplate(f.key)) continue
      const cat = historyCategoryOf(f.key)
      if (cat === 'music' || cat === 'sound') names.push(f.name)
    }
  }
  for (const m of savedMaterials.value) {
    if (!isImage(m.key)) names.push(m.name)
  }
  if (names.length) warmAudioNames(names)
}

// 新建文件夹弹窗(参照 1.html:图片/音频分区,每区可从生成历史/素材添加)
// selected: 已选素材对象 [{key,name,url,isImg}],去重按 key
const folderModal = ref({ open: false, name: '', selected: [], saving: false })
const NAME_MAX = 20

// 素材选择子弹窗:type=image|audio,source=history|material
const picker = ref({ open: false, type: 'image', source: 'history', category: 'character', items: [], loading: false, checked: [] })

function keyIsImage(k) {
  return isImage(k)
}

// 已选按图片/音频分组
const selectedImages = computed(() => folderModal.value.selected.filter((s) => s.isImg))
const selectedAudios = computed(() => folderModal.value.selected.filter((s) => !s.isImg))
const nameLen = computed(() => folderModal.value.name.length)

function openCreateFolder() {
  folderModal.value = { open: true, name: '', selected: [], saving: false }
}

// 打开素材选择器:type 决定筛哪类,source 决定来源
async function openPicker(type, source) {
  picker.value = {
    open: true,
    type,
    source,
    category: type === 'image' ? 'character' : 'music',
    items: [],
    loading: false,
    checked: [],
  }
  await loadPickerItems()
}

const PICKER_CATS = computed(() =>
  picker.value.type === 'image'
    ? [{ id: 'character', label: '角色' }, { id: 'scene', label: '场景' }]
    : [{ id: 'music', label: 'BGM' }, { id: 'sound', label: '音效' }]
)

async function loadPickerItems() {
  picker.value.loading = true
  try {
    if (picker.value.source === 'material') {
      const res = await getAssetsApi({ category: picker.value.category, page: 1, page_size: 200 })
      picker.value.items = (res.items || []).map((m) => ({
        key: m.key, name: m.name, url: m.url, isImg: keyIsImage(m.key),
      }))
    } else {
      // 生成历史:按当前分类(角色/场景/BGM/音效)精确过滤,排除配音/环境音/样板
      picker.value.items = historyFiles.value
        .filter((f) => !isBoilerplate(f.key) && historyCategoryOf(f.key) === picker.value.category)
        .map((f) => ({ key: f.key, name: f.name, url: f.url || f.key, isImg: keyIsImage(f.key) }))
    }
    if (picker.value.type === 'audio') {
      warmAudioNames(picker.value.items.map((i) => i.name))
    }
  } catch {
    picker.value.items = []
  } finally {
    picker.value.loading = false
  }
}

function togglePickerCheck(key) {
  const arr = picker.value.checked
  const i = arr.indexOf(key)
  if (i >= 0) arr.splice(i, 1)
  else arr.push(key)
}

// 确认把选择器里勾选的加入文件夹待选清单(去重)
function confirmPicker() {
  const existing = new Set(folderModal.value.selected.map((s) => s.key))
  for (const it of picker.value.items) {
    if (picker.value.checked.includes(it.key) && !existing.has(it.key)) {
      folderModal.value.selected.push(it)
    }
  }
  picker.value.open = false
}

function removeSelected(key) {
  folderModal.value.selected = folderModal.value.selected.filter((s) => s.key !== key)
}

async function confirmCreateFolder() {
  const name = folderModal.value.name.trim() || '未命名文件夹'
  folderModal.value.saving = true
  try {
    const folder = await createFolderApi(name)
    userFolders.value.unshift(folder)
    for (const s of folderModal.value.selected) {
      try {
        await saveAssetApi(s.key, folder.id)
      } catch {
        // 单条失败不阻断
      }
    }
    folderModal.value.open = false
    await fetchAll()
    message.success('已创建文件夹')
  } catch {
    message.error('创建文件夹失败')
  } finally {
    folderModal.value.saving = false
  }
}

// 删除文件夹确认(自定义暗色弹窗,避免 Modal.confirm 的浅色与整体暗色主题反差)
const deleteModal = ref({ open: false, folder: null, deleting: false })
function removeFolder(f) {
  openMenuId.value = null
  deleteModal.value = { open: true, folder: f, deleting: false }
}
async function confirmRemoveFolder() {
  const f = deleteModal.value.folder
  if (!f) return
  deleteModal.value.deleting = true
  try {
    await deleteFolderApi(f.realId)
    deleteModal.value.open = false
    await fetchAll()
    message.success('已删除文件夹')
  } catch {
    message.error('删除文件夹失败')
  } finally {
    deleteModal.value.deleting = false
  }
}

function openFolder(f) {
  openedFolder.value = { realId: f.realId, name: f.name, kind: f.kind }
  view.value = 'folder'
  if (f.kind === 'saved') {
    const key = f.realId ?? '__none__'
    const list = savedByFolder.value.get(key) || []
    const first = MATERIAL_CATEGORIES.find((c) => list.some((m) => categoryOf(m.key) === c.id))
    folderCategory.value = first ? first.id : 'character'
  }
}
function backToGrid() {
  view.value = 'grid'
  openedFolder.value = null
}

function toggleMenu(id) {
  openMenuId.value = openMenuId.value === id ? null : id
}

function askTransfer(mode, material) {
  transferModal.value = { open: true, mode, material, targetId: targetFolderOptions.value[0]?.value ?? null }
}
async function confirmTransfer() {
  const { mode, material, targetId } = transferModal.value
  try {
    if (mode === 'move') await moveSavedMaterialApi(material.id, targetId)
    else await copySavedMaterialApi(material.id, targetId)
    transferModal.value.open = false
    await fetchAll()
    message.success(mode === 'move' ? '已移动' : '已复制')
  } catch {
    message.error('操作失败')
  }
}

async function removeSaved(material) {
  try {
    await deleteSavedMaterialApi(material.id)
    await fetchAll()
    message.success('已从我的资产移除')
  } catch {
    message.error('移除失败')
  }
}

onMounted(fetchAll)
</script>

<template>
  <section class="asset-workspace">
    <div class="asset-tabs" role="tablist" aria-label="资产工作区">
      <button
        v-for="t in TABS"
        :key="t.id"
        :class="{ active: activeTab === t.id }"
        role="tab"
        :aria-selected="activeTab === t.id"
        type="button"
        @click="activeTab = t.id; openMenuId = null; backToGrid()"
      >
        {{ t.label }}
      </button>
    </div>

    <a-spin :spinning="loading">
      <!-- 资产:文件夹网格 -->
      <template v-if="activeTab === 'assets'">
        <!-- 网格视图 -->
        <div v-if="view === 'grid'" class="asset-grid">
          <button class="asset-folder-card asset-create-card" type="button" @click="openCreateFolder">
            <div class="asset-folder-preview">
              <span class="asset-create-plus">+</span>
            </div>
            <h3>新建资产</h3>
          </button>

          <article
            v-for="f in folders"
            :key="f.id"
            class="asset-folder-card"
            tabindex="0"
            @click="openFolder(f)"
            @keydown.enter="openFolder(f)"
          >
            <div class="asset-folder-preview">
              <div v-if="f.images.length" class="asset-folder-documents">
                <img
                  v-for="(img, i) in f.images"
                  :key="`${f.id}-${i}`"
                  class="asset-folder-document"
                  :class="`document-${i + 1}`"
                  :src="img"
                  alt=""
                />
              </div>
              <span v-else class="asset-folder-empty">空</span>
            </div>
            <div class="asset-folder-info">
              <div>
                <h3>{{ f.name }}</h3>
                <p>最近修改 · {{ f.date }} · {{ f.count }} 个文件</p>
              </div>
              <div v-if="f.removable" class="asset-more-actions">
                <button
                  class="asset-more-button"
                  type="button"
                  :aria-label="`${f.name}更多操作`"
                  :aria-expanded="openMenuId === f.id"
                  title="更多操作"
                  @click.stop="toggleMenu(f.id)"
                >
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                    stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                    <circle cx="5" cy="12" r="1" fill="currentColor" />
                    <circle cx="12" cy="12" r="1" fill="currentColor" />
                    <circle cx="19" cy="12" r="1" fill="currentColor" />
                  </svg>
                </button>
                <div v-if="openMenuId === f.id" class="asset-more-menu" @click.stop>
                  <button type="button" @click="removeFolder(f)">删除</button>
                </div>
              </div>
            </div>
          </article>

          <p class="asset-end">没有更多了</p>
        </div>

        <!-- 文件夹详情视图 -->
        <div v-else class="asset-folder-detail">
          <div class="asset-folder-detail-bar">
            <button type="button" class="asset-back-button" @click="backToGrid">‹ 返回</button>
            <h2>{{ openedFolder?.name }}</h2>
          </div>

          <!-- 已保存素材文件夹:分类子标签 + 可移动/复制/移除 -->
          <template v-if="openedFolder?.kind === 'saved'">
            <template v-if="openedMaterials.length">
              <div class="generation-category-tabs folder-category-tabs" role="tablist">
                <button
                  v-for="c in MATERIAL_CATEGORIES"
                  :key="c.id"
                  type="button"
                  :class="{ active: folderCategory === c.id }"
                  :aria-selected="folderCategory === c.id"
                  @click="folderCategory = c.id"
                >
                  {{ c.label }} ({{ folderCategoryCounts[c.id] }})
                </button>
              </div>

              <!-- 图片素材:网格(场景横版) -->
              <div
                v-if="isImageCategory && openedMaterialsInCategory.length"
                class="asset-history-grid"
                :class="{ 'scene-grid': isSceneCategory }"
              >
                <article
                  v-for="m in openedMaterialsInCategory"
                  :key="m.id"
                  class="asset-history-card"
                  :class="{ landscape: isSceneCategory }"
                >
                  <img :src="m.url" :alt="m.name" />
                  <div class="asset-history-actions">
                    <button type="button" @click="askTransfer('move', m)">移动</button>
                    <button type="button" @click="askTransfer('copy', m)">复制</button>
                    <button type="button" @click="removeSaved(m)">移除</button>
                  </div>
                </article>
              </div>

              <!-- 音频素材:行列表 -->
              <div
                v-else-if="!isImageCategory && openedMaterialsInCategory.length"
                class="audio-history-list"
              >
                <article v-for="m in openedMaterialsInCategory" :key="m.id" class="audio-history-row">
                  <div class="audio-history-name">
                  <strong>{{ prettifyAudioName(m.name) }}</strong>
                  <span class="audio-raw-name">{{ m.name }}</span>
                </div>
                  <div class="audio-history-player">
                    <audio :src="m.url" controls preload="none" />
                  </div>
                  <div class="audio-history-actions">
                    <button type="button" @click="askTransfer('move', m)">移动</button>
                    <button type="button" @click="askTransfer('copy', m)">复制</button>
                    <button type="button" @click="removeSaved(m)">移除</button>
                  </div>
                </article>
              </div>

              <a-empty v-else description="该分类暂无素材" />
            </template>
            <a-empty v-else description="该文件夹暂无素材" />
          </template>
        </div>
      </template>

      <!-- 生成历史:按分类展示 -->
      <div v-else-if="activeTab === 'history'" class="generation-history">
        <a-empty v-if="!historyFiles.length && !loading" description="暂无生成历史" />
        <template v-else>
          <div class="history-toolbar">
            <div class="generation-category-tabs folder-category-tabs" role="tablist">
              <button
                v-for="c in historyTabs"
                :key="c.id"
                type="button"
                :class="{ active: historyCategory === c.id }"
                :aria-selected="historyCategory === c.id"
                @click="historyCategory = c.id"
              >
                {{ c.label }} ({{ historyByCategory[c.id].length }})
              </button>
            </div>
            <a-range-picker v-model:value="historyRange" size="small" class="history-range" />
          </div>

          <a-empty v-if="!historyCurrentFiles.length" description="该分类/时间段暂无内容" />

          <!-- 按时间分组 -->
          <div v-for="g in historyGroups" :key="g.key" class="history-group">
            <div class="history-group-title">{{ g.label }} ({{ g.files.length }})</div>

            <!-- 图片类:网格(场景横版) -->
            <div
              v-if="historyCurrentKind === 'image'"
              class="asset-history-grid"
              :class="{ 'scene-grid': historyCategory === 'scene' }"
            >
              <article
                v-for="f in g.files"
                :key="f.key"
                class="asset-history-card"
                :class="{ landscape: historyCategory === 'scene' }"
              >
                <img :src="f.url || f.key" :alt="f.name" />
                <div class="asset-history-meta">
                  <strong>{{ f.name }}</strong>
                  <span>{{ f.project }}</span>
                </div>
              </article>
            </div>

            <!-- 音频类:行列表 + 播放器 -->
            <div v-else-if="historyCurrentKind === 'audio'" class="audio-history-list">
              <article v-for="f in g.files" :key="f.key" class="audio-history-row">
                <div class="audio-history-name">
                  <strong>{{ prettifyAudioName(f.name) }}</strong>
                  <span>{{ f.project }} · {{ f.name }}</span>
                </div>
                <div class="audio-history-player">
                  <audio :src="f.url || f.key" controls preload="none" />
                </div>
              </article>
            </div>

            <!-- 其他文件 -->
            <div v-else class="asset-history-grid">
              <article v-for="f in g.files" :key="f.key" class="asset-history-card">
                <div class="asset-draft-file">{{ f.name }}</div>
                <div class="asset-history-meta">
                  <strong>{{ f.name }}</strong>
                  <span>{{ f.project }} · {{ formatSize(f.size) }}</span>
                </div>
              </article>
            </div>
          </div>
        </template>
      </div>

      <!-- 画布 -->
      <div v-else class="asset-canvas-empty">
        <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="m12 3 9 5-9 5-9-5 9-5Z" />
          <path d="m3 12 9 5 9-5M3 16l9 5 9-5" />
        </svg>
        <h3>画布工作区</h3>
        <p>画布功能正在准备中,后续可在这里编辑和组织资产。</p>
      </div>
    </a-spin>

    <!-- 移动/复制目标选择弹窗 -->
    <a-modal
      v-model:open="transferModal.open"
      :title="transferModal.mode === 'move' ? '移动到文件夹' : '复制到文件夹'"
      ok-text="确定"
      cancel-text="取消"
      @ok="confirmTransfer"
    >
      <a-select
        v-model:value="transferModal.targetId"
        style="width: 100%"
        :options="targetFolderOptions"
        placeholder="选择目标文件夹"
      />
    </a-modal>

    <!-- 新建资产弹窗(参照 1.html) -->
    <a-modal
      v-model:open="folderModal.open"
      :footer="null"
      :closable="false"
      width="520px"
      wrap-class-name="nf-modal"
    >
      <div class="nf-head">
        <div class="nf-title">新建资产</div>
        <div class="nf-close" @click="folderModal.open = false">✕</div>
      </div>

      <!-- 图片素材分区 -->
      <div class="nf-section">
        <div class="nf-sec-top">
          <div class="nf-sec-title">图片素材</div>
          <div class="nf-sec-actions">
            <button type="button" @click="openPicker('image', 'history')">从生成历史添加</button>
            <button type="button" @click="openPicker('image', 'material')">从素材添加</button>
          </div>
        </div>
        <div v-if="!selectedImages.length" class="nf-sec-desc">暂未添加图片素材</div>
        <div v-else class="nf-thumbs">
          <div v-for="s in selectedImages" :key="s.key" class="nf-thumb">
            <img :src="s.url" :alt="s.name" />
            <span class="nf-thumb-del" @click="removeSelected(s.key)">✕</span>
          </div>
        </div>
      </div>

      <!-- 音频素材分区 -->
      <div class="nf-section">
        <div class="nf-sec-top">
          <div class="nf-sec-title">音频素材</div>
          <div class="nf-sec-actions">
            <button type="button" @click="openPicker('audio', 'history')">从生成历史添加</button>
            <button type="button" @click="openPicker('audio', 'material')">从素材添加</button>
          </div>
        </div>
        <div v-if="!selectedAudios.length" class="nf-sec-desc">暂未添加音频素材</div>
        <ul v-else class="nf-audio-list">
          <li v-for="s in selectedAudios" :key="s.key">
            <span class="nf-audio-name">{{ prettifyAudioName(s.name) }}</span>
            <span class="nf-thumb-del" @click="removeSelected(s.key)">✕</span>
          </li>
        </ul>
      </div>

      <div class="nf-input-group">
        <label>名称</label>
        <div class="nf-input-wrapper">
          <input
            v-model="folderModal.name"
            type="text"
            placeholder="输入文件夹名称"
            :maxlength="NAME_MAX"
          />
          <span class="nf-char-count">{{ nameLen }}/{{ NAME_MAX }}</span>
        </div>
      </div>

      <button class="nf-save-btn" :disabled="folderModal.saving" @click="confirmCreateFolder">
        {{ folderModal.saving ? '保存中…' : '保存' }}
      </button>
    </a-modal>

    <!-- 素材选择子弹窗 -->
    <a-modal
      v-model:open="picker.open"
      :title="picker.source === 'history' ? '从生成历史添加' : '从素材添加'"
      ok-text="确定"
      cancel-text="取消"
      width="720px"
      @ok="confirmPicker"
    >
      <div class="generation-category-tabs" role="tablist">
        <button
          v-for="c in PICKER_CATS"
          :key="c.id"
          type="button"
          :class="{ active: picker.category === c.id }"
          @click="picker.category = c.id; loadPickerItems()"
        >
          {{ c.label }}
        </button>
      </div>
      <a-spin :spinning="picker.loading">
        <div class="nf-grid">
          <a-empty v-if="!picker.items.length && !picker.loading" description="暂无可选素材" />
          <button
            v-for="it in picker.items"
            :key="it.key"
            type="button"
            class="nf-item"
            :class="{ picked: picker.checked.includes(it.key) }"
            @click="togglePickerCheck(it.key)"
          >
            <img v-if="it.isImg" :src="it.url" :alt="it.name" />
            <div v-else class="nf-item-file">{{ prettifyAudioName(it.name) }}</div>
            <span class="nf-item-name">{{ it.isImg ? it.name : prettifyAudioName(it.name) }}</span>
          </button>
        </div>
      </a-spin>
    </a-modal>

    <!-- 删除文件夹确认(暗色,与整体风格一致) -->
    <a-modal
      v-model:open="deleteModal.open"
      :footer="null"
      :closable="false"
      width="400px"
      wrap-class-name="nf-modal"
    >
      <div class="del-title">删除文件夹「{{ deleteModal.folder?.name }}」?</div>
      <div class="del-desc">文件夹内已保存素材记录会一并移除(不影响素材库原文件)。</div>
      <div class="del-actions">
        <button type="button" class="del-cancel" @click="deleteModal.open = false">取消</button>
        <button type="button" class="del-confirm" :disabled="deleteModal.deleting" @click="confirmRemoveFolder">
          {{ deleteModal.deleting ? '删除中…' : '删除' }}
        </button>
      </div>
    </a-modal>
  </section>
</template>

<style scoped>
.folder-category-tabs {
  margin-bottom: 18px;
}
/* 场景为横版素材,减少每行列数让卡片更宽,避免竖版留白 */
.asset-history-grid.scene-grid {
  grid-template-columns: repeat(4, minmax(0, 1fr));
}
/* 生成历史卡片下方的文件名/来源说明 */
.asset-history-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px 10px;
}
.asset-history-meta strong {
  overflow: hidden;
  color: #f1f3f8;
  font-size: 13px;
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.asset-history-meta span {
  overflow: hidden;
  color: #858b99;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.audio-history-name span {
  display: block;
  margin-top: 2px;
  color: #858b99;
  font-size: 12px;
}
.audio-raw-name {
  display: block;
  margin-top: 2px;
  color: #6f7582;
  font-size: 11px;
}
/* 生成历史工具栏(分类 + 时间搜索) */
.history-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
.history-toolbar .folder-category-tabs {
  margin-bottom: 0;
}
.history-group {
  margin-bottom: 22px;
}
.history-group-title {
  margin: 0 0 12px;
  color: #9aa0b0;
  font-size: 13px;
  font-weight: 500;
}
/* 新建文件夹弹窗 */
/* 新建资产弹窗(参照 1.html 暗色设计) */
.nf-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
}
.nf-title {
  color: #e5e7eb;
  font-size: 16px;
  font-weight: 600;
}
.nf-close {
  color: #9ca3af;
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  transition: color 0.2s;
}
.nf-close:hover {
  color: #e5e7eb;
}
.nf-section {
  margin-bottom: 12px;
  padding: 16px;
  border: 1px solid #3b3f54;
  border-radius: 8px;
}
.nf-sec-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}
.nf-sec-title {
  color: #e5e7eb;
  font-size: 14px;
  font-weight: 500;
}
.nf-sec-actions button {
  margin-left: 8px;
  padding: 4px 12px;
  border: 1px solid #4b526d;
  border-radius: 4px;
  background: transparent;
  color: #e5e7eb;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.2s;
}
.nf-sec-actions button:hover {
  background: rgba(255, 255, 255, 0.05);
}
.nf-sec-desc {
  color: #9ca3af;
  font-size: 12px;
}
.nf-thumbs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.nf-thumb {
  position: relative;
  width: 64px;
  height: 64px;
  border-radius: 6px;
  overflow: hidden;
  background: #1f222d;
}
.nf-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.nf-thumb-del {
  position: absolute;
  top: 2px;
  right: 2px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.6);
  color: #fff;
  font-size: 10px;
  cursor: pointer;
}
.nf-audio-list {
  margin: 0;
  padding: 0;
  list-style: none;
}
.nf-audio-list li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 0;
  color: #e5e7eb;
  font-size: 13px;
}
.nf-audio-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.nf-audio-list .nf-thumb-del {
  position: static;
  background: transparent;
  color: #9ca3af;
}
.nf-input-group {
  margin: 20px 0 24px;
}
.nf-input-group label {
  display: block;
  margin-bottom: 8px;
  color: #e5e7eb;
  font-size: 14px;
  font-weight: 500;
}
.nf-input-wrapper {
  position: relative;
}
.nf-input-wrapper input {
  width: 100%;
  box-sizing: border-box;
  padding: 12px 48px 12px 14px;
  border: 1px solid #3b3f54;
  border-radius: 6px;
  background: #1f222d;
  color: #e5e7eb;
  font-size: 14px;
  outline: none;
}
.nf-input-wrapper input::placeholder {
  color: #6b7280;
}
.nf-char-count {
  position: absolute;
  right: 14px;
  top: 50%;
  transform: translateY(-50%);
  color: #9ca3af;
  font-size: 12px;
}
.nf-save-btn {
  width: 100%;
  padding: 12px 0;
  border: none;
  border-radius: 6px;
  background: linear-gradient(90deg, #4f93f9 0%, #3b76f6 100%);
  color: #fff;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  box-shadow: 0 4px 12px rgba(59, 118, 246, 0.3);
  transition: opacity 0.2s;
}
.nf-save-btn:hover {
  opacity: 0.9;
}
.nf-save-btn:disabled {
  opacity: 0.6;
  cursor: default;
}
.nf-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  max-height: 340px;
  overflow-y: auto;
  padding: 2px;
}
.nf-item {
  position: relative;
  display: flex;
  flex-direction: column;
  padding: 0;
  border: 2px solid transparent;
  border-radius: 8px;
  background: #f5f5f6;
  overflow: hidden;
  cursor: pointer;
  transition: border-color 0.15s ease;
}
.nf-item.picked {
  border-color: #8e9fff;
}
.nf-item img {
  width: 100%;
  aspect-ratio: 1 / 1;
  object-fit: cover;
  display: block;
}
.nf-item-file {
  display: flex;
  align-items: center;
  justify-content: center;
  aspect-ratio: 1 / 1;
  padding: 8px;
  color: #666;
  font-size: 12px;
  word-break: break-all;
}
.nf-item-name {
  overflow: hidden;
  padding: 6px 8px;
  color: #333;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.asset-folder-detail-bar {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 18px;
}
.asset-folder-detail-bar h2 {
  margin: 0;
  font-size: 1.05rem;
  color: var(--text-primary, #f3f5fa);
}
.asset-back-button {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 6px 14px;
  border: 1px solid rgba(142, 159, 255, 0.18);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.03);
  color: #8b90a0;
  font-size: 0.9rem;
  cursor: pointer;
  transition: color 0.15s ease, border-color 0.15s ease, background 0.15s ease;
}
.asset-back-button:hover {
  color: #f3f5fa;
  border-color: rgba(142, 159, 255, 0.4);
  background: rgba(142, 159, 255, 0.08);
}
</style>

<!-- 新建资产弹窗:暗色主题(a-modal 内容被 teleport 到 body,用非 scoped 覆盖) -->
<style>
.nf-modal .ant-modal-content {
  background: #272a37;
  border: 1px solid rgba(255, 255, 255, 0.05);
  padding: 24px;
}
.nf-modal .ant-modal-body {
  padding: 0;
}
.nf-modal .del-title {
  margin-bottom: 10px;
  color: #e5e7eb;
  font-size: 16px;
  font-weight: 600;
}
.nf-modal .del-desc {
  margin-bottom: 22px;
  color: #9ca3af;
  font-size: 13px;
  line-height: 1.6;
}
.nf-modal .del-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
.nf-modal .del-cancel,
.nf-modal .del-confirm {
  padding: 7px 18px;
  border-radius: 6px;
  font-size: 14px;
  cursor: pointer;
}
.nf-modal .del-cancel {
  border: 1px solid #4b526d;
  background: transparent;
  color: #e5e7eb;
}
.nf-modal .del-cancel:hover {
  background: rgba(255, 255, 255, 0.05);
}
.nf-modal .del-confirm {
  border: none;
  background: linear-gradient(90deg, #f56565 0%, #e53e3e 100%);
  color: #fff;
}
.nf-modal .del-confirm:hover {
  opacity: 0.9;
}
.nf-modal .del-confirm:disabled {
  opacity: 0.6;
  cursor: default;
}
</style>
