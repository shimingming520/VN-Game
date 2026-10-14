<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import ProjectCard from './ProjectCard.vue'
import PublishWizardModal from '@/components/publish/PublishWizardModal.vue'
import { deleteProjectApi, renameProjectApi } from '@/api'

/**
 * 我的项目 / 全部项目 两种形态共用一段逻辑(对应 demo 里的 Projects 组件)。
 *
 * compact:首页,最多 6 个,标题「我的项目」,带「全部项目」入口。
 * all:全部项目页,首卡是「开始创作」,底部固定「没有更多了」。
 */
const props = defineProps({
  projects: { type: Array, default: () => [] },
  variant: { type: String, default: 'compact' },
})

const emit = defineEmits(['refresh'])

const router = useRouter()

const isCompact = computed(() => props.variant === 'compact')
const hiddenDeletedIds = ref([])

// 后端已按 coalesce(update_time, create_time) 倒序返回,这里再排一次是为了重命名后
// 本地时间戳变化能立刻反映顺序,不必等下一次请求。
const sorted = computed(() =>
  props.projects.filter((item) => !hiddenDeletedIds.value.includes(item.task_id)).sort(
    (a, b) => new Date(b.update_time || 0) - new Date(a.update_time || 0),
  ),
)
const visible = computed(() => (isCompact.value ? sorted.value.slice(0, 6) : sorted.value))

const progressOf = (p) => Number(p.progress) || 0

// ==================== 跨卡片状态 ====================
const sectionRef = ref(null)
const openMenuId = ref(null)
const selectedIds = ref([])
const blockedId = ref(null)

const selectedProjects = computed(() =>
  props.projects.filter((p) => selectedIds.value.includes(p.task_id)),
)

function toggleMenu(p) {
  openMenuId.value = openMenuId.value === p.task_id ? null : p.task_id
}

function toggleSelect(p) {
  const id = p.task_id
  selectedIds.value = selectedIds.value.includes(id)
    ? selectedIds.value.filter((x) => x !== id)
    : [...selectedIds.value, id]
}

/** 菜单外按下即关(用 pointerdown 而非 click:拖拽/右键也该收起) */
function onPointerDown(e) {
  if (sectionRef.value?.contains(e.target)) return
  openMenuId.value = null
}

onMounted(() => window.addEventListener('pointerdown', onPointerDown))
onBeforeUnmount(() => window.removeEventListener('pointerdown', onPointerDown))

/** 被拦下的卡片滚进视野并高亮一会儿,否则用户在长列表里不知道是哪个项目挡住了发布 */
let blockedTimer = null
async function highlightBlocked(taskId) {
  if (blockedTimer) window.clearTimeout(blockedTimer)
  blockedId.value = taskId
  await nextTick()
  sectionRef.value
    ?.querySelector(`[data-project-id="${taskId}"]`)
    ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  blockedTimer = window.setTimeout(() => {
    blockedId.value = null
  }, 1800)
}
onBeforeUnmount(() => blockedTimer && window.clearTimeout(blockedTimer))

// 列表刷新后清掉已经不存在的选中项(删除项目后残留 id 会让批量发布数字对不上)
watch(
  () => props.projects,
  (list) => {
    const ids = new Set(list.map((p) => p.task_id))
    selectedIds.value = selectedIds.value.filter((id) => ids.has(id))
  },
)

// ==================== 打开 / 创作 ====================
function openProject(p) {
  openMenuId.value = null
  // 只带 task_id:创作页按后端 entry/stage 判断该落到第几步,不靠前端传参
  router.push({ path: '/create', query: { task_id: p.task_id } })
}

function goCreate() {
  router.push('/create')
}

/** 创建相似品:后端还没有「复制项目」端点,先把来源 id 带进创作页,由创作页后续读取 */
function createSimilar(p) {
  openMenuId.value = null
  router.push({ path: '/create', query: { source_task_id: p.task_id } })
}

/** 单个发布:先过发布表单,再走中台那套 CMS 发布(与中台任务列表的发布同一条链路) */
const wizardRef = ref(null)

function publishProject(p) {
  openMenuId.value = null
  // 没跑完的作品发布出去就是坏链,和批量发布一个口径:先拦下并指出是哪个
  if (progressOf(p) < 100) {
    highlightBlocked(p.task_id)
    message.info('作品尚未生成完成，暂时不能发布')
    return
  }
  wizardRef.value?.init([{ task_id: p.task_id, name: p.name }], 'single')
}

// ==================== 重命名 ====================
const NAME_MAX = 20
const renaming = ref(null)
const renameValue = ref('')
const renameError = ref('')
const renameSaving = ref(false)
const renameInputRef = ref(null)

const renameLength = computed(() => renameValue.value.length)
const renameOverflow = computed(() => renameLength.value > NAME_MAX)

async function startRename(p) {
  openMenuId.value = null
  renaming.value = p
  renameValue.value = p.name || ''
  renameError.value = ''
  await nextTick()
  renameInputRef.value?.select()
}

function closeRename() {
  renaming.value = null
  renameError.value = ''
}

/**
 * 不加 maxlength、也不 slice 截断:静默吃掉第 21 个字,用户只会觉得键盘坏了。
 * 让他打得出来,再明确告诉他超了。
 */
function onRenameInput(e) {
  renameValue.value = e.target.value
  renameError.value = renameOverflow.value ? '超出字数上限' : ''
}

async function submitRename() {
  if (!renaming.value || renameSaving.value) return
  const name = renameValue.value.trim()
  if (!name) {
    renameError.value = '作品名不能为空'
    return
  }
  if (name.length > NAME_MAX) {
    renameError.value = '超出字数上限'
    return
  }
  // 本地先挡一次重名,省一次必然失败的请求;后端仍是最终判定方
  const dup = props.projects.some(
    (p) => p.task_id !== renaming.value.task_id && (p.name || '').trim() === name,
  )
  if (dup) {
    renameError.value = '已有该命名'
    return
  }

  renameSaving.value = true
  try {
    await renameProjectApi(renaming.value.task_id, name)
    closeRename()
    emit('refresh')
  } catch (err) {
    // 后端把「超出字数上限」「已有该命名」放在 detail 里,原样展示给用户
    renameError.value =
      err?.response?.data?.detail || err?.response?.data?.msg || err?.message || '重命名失败'
  } finally {
    renameSaving.value = false
  }
}

// ==================== 删除 ====================
const deleting = ref(null)
const deleteSaving = ref(false)

function startDelete(p) {
  openMenuId.value = null
  deleting.value = p
}

async function submitDelete() {
  if (!deleting.value || deleteSaving.value) return
  const taskId = deleting.value.task_id
  deleteSaving.value = true
  // 先关弹窗并隐藏卡片。即使后端已提交删除、HTTP 响应在途中丢失，界面也不会卡死。
  deleting.value = null
  hiddenDeletedIds.value = [...hiddenDeletedIds.value, taskId]
  try {
    await deleteProjectApi(taskId)
    message.success('项目已移入回收站')
    emit('refresh')
  } catch (error) {
    hiddenDeletedIds.value = hiddenDeletedIds.value.filter((id) => id !== taskId)
    emit('refresh')
    message.warning(`删除结果未确认，已重新同步项目列表：${error?.message ?? error}`)
  } finally {
    deleteSaving.value = false
  }
}

// ==================== 弹窗打开时的全局副作用 ====================
const modalOpen = computed(() => !!renaming.value || !!deleting.value)

function closeTopModal() {
  if (renaming.value) return closeRename()
  if (deleting.value) deleting.value = null
}

function onKeydown(e) {
  if (e.key === 'Escape' && modalOpen.value) closeTopModal()
}

/**
 * 复用 demo 的 asset-modal-open 约定:锁背景滚动,同时把 header / side-nav 压到
 * z-index:0 并断掉指针 —— 这两块是 fixed 定位,不处理会浮在遮罩之上。
 */
watch(modalOpen, (open) => {
  document.body.classList.toggle('asset-modal-open', open)
  document.querySelector('.guest-app')?.classList.toggle('asset-modal-open', open)
})

onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  // 组件被卸载时弹窗可能还开着(比如路由跳走),不清理会让整站滚动一直锁死
  document.body.classList.remove('asset-modal-open')
  document.querySelector('.guest-app')?.classList.remove('asset-modal-open')
})

// ==================== 批量发布 ====================
// 已发布的作品不再重复发布;未跑完的会被下面的 unfinished 拦下
const publishableSelection = computed(() => selectedProjects.value.filter((p) => !p.is_published))

function onBatchPublish() {
  const picked = selectedProjects.value
  if (!picked.length) return

  const unfinished = picked.find((p) => progressOf(p) < 100)
  if (unfinished) {
    highlightBlocked(unfinished.task_id)
    message.info('有项目未完成，请先取消勾选未生成完的作品')
    return
  }

  if (!publishableSelection.value.length) {
    message.info('所选作品均已发布')
    return
  }

  // 一张张过表单；这是批量入口，即使只勾选一项也必须调用批量发布接口。
  wizardRef.value?.init(
    publishableSelection.value.map((p) => ({ task_id: p.task_id, name: p.name })),
    'batch',
  )
}

function onPublished() {
  selectedIds.value = []
  emit('refresh')
}
</script>

<template>
  <section ref="sectionRef" class="projects-section" :class="isCompact ? 'projects-compact' : 'projects-all'">
    <header class="projects-heading">
      <h2>{{ isCompact ? '我的项目' : '全部项目' }}</h2>

      <div class="project-heading-actions">
        <button
          class="project-batch-publish"
          :class="{ disabled: !selectedIds.length }"
          type="button"
          :disabled="!selectedIds.length"
          @click="onBatchPublish"
        >
          批量发布<template v-if="selectedIds.length"> ({{ selectedIds.length }})</template>
        </button>

        <button
          v-if="isCompact"
          class="projects-all-link"
          :class="{ muted: projects.length <= 6 }"
          type="button"
          @click="router.push('/projects')"
        >
          全部项目
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M5 12h14M13 6l6 6-6 6" />
          </svg>
        </button>
      </div>
    </header>

    <div class="projects-grid">
      <button v-if="!isCompact" class="project-card project-create-card" type="button" @click="goCreate">
        <div class="project-cover">
          <span>+</span>
          <b>开始创作</b>
        </div>
        <div class="project-info">
          <h3>创建新的游戏项目</h3>
        </div>
      </button>

      <ProjectCard
        v-for="p in visible"
        :key="p.task_id"
        :project="p"
        :selected="selectedIds.includes(p.task_id)"
        :blocked="blockedId === p.task_id"
        :menu-open="openMenuId === p.task_id"
        @open="openProject"
        @toggle-select="toggleSelect"
        @toggle-menu="toggleMenu"
        @rename="startRename"
        @similar="createSimilar"
        @publish="publishProject"
        @delete="startDelete"
      />
    </div>

    <p v-if="!isCompact" class="projects-end">没有更多了</p>

    <PublishWizardModal ref="wizardRef" @success="onPublished" />

    <!-- 两个弹窗都 teleport 到 body:.asset-modal-backdrop 是 z-index:120,但它原本嵌在
         main(z-index:1)里,而 .guest-header 是 z-index:12 —— 留在原地会被头部盖住一角。 -->
    <Teleport to="body">
      <!-- 重命名弹窗:点遮罩关闭,弹窗本体吞掉 mousedown -->
      <div v-if="renaming" class="asset-modal-backdrop" @mousedown="closeRename">
        <section
          class="asset-modal rename-folder-modal project-rename-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="rename-project-title"
          @mousedown.stop
        >
          <header>
            <h2 id="rename-project-title">重命名项目</h2>
            <button type="button" aria-label="关闭" @click="closeRename">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="m6 6 12 12M18 6 6 18" />
              </svg>
            </button>
          </header>

          <label class="asset-name-field">
            名称
            <input
              ref="renameInputRef"
              :value="renameValue"
              placeholder="输入项目名称"
              :aria-invalid="renameOverflow"
              @input="onRenameInput"
              @keyup.enter="submitRename"
            />
            <span :class="{ 'over-limit': renameOverflow }">{{ renameLength }}/{{ NAME_MAX }}</span>
          </label>

          <p v-if="renameError" class="asset-name-error">{{ renameError }}</p>

          <footer>
            <button type="button" class="asset-save-button" :disabled="renameSaving" @click="submitRename">
              {{ renameSaving ? '保存中' : '保存' }}
            </button>
          </footer>
        </section>
      </div>

      <!-- 删除弹窗 -->
      <div v-if="deleting" class="asset-modal-backdrop" @mousedown="deleting = null">
        <section
          class="asset-modal delete-project-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="delete-project-title"
          @mousedown.stop
        >
          <header>
            <h2 id="delete-project-title">删除项目</h2>
            <button type="button" aria-label="关闭" @click="deleting = null">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="m6 6 12 12M18 6 6 18" />
              </svg>
            </button>
          </header>

          <p>删除“{{ deleting.name }}”后将移入回收站,不再出现在项目列表中,确认删除吗?</p>

          <footer>
            <button type="button" class="asset-cancel-button" @click="deleting = null">取消</button>
            <button type="button" class="asset-danger-button" :disabled="deleteSaving" @click="submitDelete">
              {{ deleteSaving ? '删除中' : '确认删除' }}
            </button>
          </footer>
        </section>
      </div>
    </Teleport>
  </section>
</template>

<style scoped>
/* 弹窗输入框的补充规则:chilang.css 只定义了正常态,超长时需要红框 + 红字数提示。
   计数器右侧留白已由 .asset-name-field input 的 padding-right 让出,这里不改布局。 */
.asset-name-field input[aria-invalid='true'] {
  border-color: #ff9f9f;
  box-shadow: 0 0 0 3px rgba(255, 120, 120, 0.16);
}

.asset-name-field > span.over-limit {
  color: #ff9f9f;
}

.asset-save-button:disabled,
.asset-danger-button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

/* 长项目名不破弹窗布局 */
.delete-project-modal > p {
  overflow-wrap: anywhere;
}
</style>
