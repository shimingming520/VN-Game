<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { batchPublishProjects, publishProjectApi } from '@/api'
import PublishForm from './PublishForm.vue'

/**
 * 发布向导。一张张过表单，最终接口由入口语义决定，而不是由勾选数量决定：
 * 卡片单发 → POST /v1/projects/{task_id}/publish（中台 publish-cms）
 * 批量按钮 → POST /v1/projects/publish-batch（即使只勾选一项，也走 CMS 原生 batch_set）
 * 中途取消不提交任何一项。
 */
const emit = defineEmits(['success'])

const open = ref(false)
const items = ref([])
const cursor = ref(0)
const collected = ref([])
const submitting = ref(false)
const formRef = ref(null)
const formKey = ref(0)
const publishMode = ref('single')
const PENDING_WIZARD_KEY = 'engine_pending_publish_wizard'

const total = computed(() => items.value.length)
const current = computed(() => items.value[cursor.value] ?? null)
const isLast = computed(() => cursor.value === total.value - 1)
const title = computed(() =>
  total.value > 1 ? `发布信息（第 ${cursor.value + 1} / ${total.value} 项）` : '发布信息',
)

/** @param {Array<{task_id:string, name?:string}>} projects 已经过 progress 拦截的项目 */
function savePendingWizard(projects, mode) {
  try {
    sessionStorage.setItem(PENDING_WIZARD_KEY, JSON.stringify({ projects, mode }))
  } catch {
    // ignore
  }
}

function clearPendingWizard() {
  try {
    sessionStorage.removeItem(PENDING_WIZARD_KEY)
  } catch {
    // ignore
  }
}

function init(projects, mode = 'single', remember = true) {
  items.value = (projects ?? []).filter((p) => p?.task_id)
  if (!items.value.length) return
  publishMode.value = mode === 'batch' ? 'batch' : 'single'
  if (remember) savePendingWizard(items.value, publishMode.value)
  cursor.value = 0
  collected.value = []
  formKey.value += 1
  open.value = true
  // 由父级统一触发一次 load；避免子组件 auto-load 与这里重复请求 CMS。
  void loadCurrentForm()
}

async function loadCurrentForm(payload) {
  await nextTick()
  await formRef.value?.load()
  if (payload) formRef.value?.restore(payload)
}

async function remountForm(payload) {
  formKey.value += 1
  await loadCurrentForm(payload)
}

async function handleNext() {
  const payload = await formRef.value?.validateAndCollect()
  if (!payload) return
  collected.value[cursor.value] = payload
  if (!isLast.value) {
    cursor.value += 1
    await remountForm(collected.value[cursor.value])
    return
  }
  await submitAll()
}

async function handlePrev() {
  if (!cursor.value) return
  // 当前项已填的内容先存下来，回来时能原样看到
  const payload = await formRef.value?.validateAndCollect()
  if (payload) collected.value[cursor.value] = payload
  cursor.value -= 1
  await remountForm(collected.value[cursor.value])
}

function nameOf(taskId) {
  return items.value.find((p) => p.task_id === taskId)?.name || taskId
}

function errorText(error) {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  return error?.message || String(error || '未知错误')
}

async function submitAll() {
  submitting.value = true
  try {
    if (publishMode.value === 'single') {
      const { task_id, ...rest } = collected.value[0]
      await publishProjectApi(task_id, rest)
      message.success('发布成功')
    } else {
      const res = await batchPublishProjects(collected.value)
      const data = res?.data ?? res
      const rows = [...(data?.results ?? []), ...(data?.skipped ?? [])]
      const failedRows = rows.filter((row) => row?.success === false)
      const explicitRows = rows.filter((row) => typeof row?.success === 'boolean')
      const responseSucceeded =
        data?.ok === true ||
        data?.success === true ||
        [0, 1, 200, '0', '1', '200'].includes(data?.code)
      const failed = failedRows.length || (
        explicitRows.length ? 0 : (!responseSucceeded ? Number(data?.failed_count ?? 0) : 0)
      )
      const ok = explicitRows.length
        ? explicitRows.filter((row) => row.success).length
        : Number(data?.success_count ?? (responseSucceeded ? collected.value.length : 0))
      if (failed) {
        const reasons = failedRows
          .slice(0, 3)
          .map((r) => `${nameOf(r.task_id)}：${r.reason || '发布失败'}`)
        message.warning(
          `成功 ${ok} 个，失败 ${failed} 个${reasons.length ? `（${reasons.join('；')}）` : ''}`,
        )
      } else {
        message.success(`已发布 ${ok || collected.value.length} 个项目`)
      }
    }
    open.value = false
    clearPendingWizard()
    emit('success')
  } catch (error) {
    message.error(`发布失败：${errorText(error)}`)
  } finally {
    submitting.value = false
  }
}

function handleCancel() {
  if (submitting.value) return
  open.value = false
  collected.value = []
  clearPendingWizard()
}

defineExpose({ init })

onMounted(() => {
  try {
    const pending = JSON.parse(sessionStorage.getItem(PENDING_WIZARD_KEY) || 'null')
    if (pending?.projects?.length) init(pending.projects, pending.mode, false)
  } catch {
    clearPendingWizard()
  }
})
</script>

<template>
  <a-modal
    :open="open"
    :title="title"
    :width="760"
    :mask-closable="false"
    :closable="!submitting"
    centered
    destroy-on-close
    wrap-class-name="publish-wizard-modal"
    @cancel="handleCancel"
  >
    <div class="publish-wizard">
      <p v-if="total > 1" class="publish-wizard__hint">
        正在填写「{{ current?.name || current?.task_id }}」。全部填完后一次性提交，中途取消不会发布任何作品。
      </p>
      <PublishForm
        v-if="current"
        :key="formKey"
        ref="formRef"
        :task-id="current.task_id"
      />
    </div>

    <template #footer>
      <div class="publish-wizard__foot">
        <a-button :disabled="submitting" @click="handleCancel">取消</a-button>
        <a-button v-if="cursor > 0" :disabled="submitting" @click="handlePrev">上一项</a-button>
        <a-button type="primary" :loading="submitting" @click="handleNext">
          {{ isLast ? (total > 1 ? '提交全部' : '确定') : '下一项' }}
        </a-button>
      </div>
    </template>
  </a-modal>
</template>

<style scoped>
.publish-wizard {
  max-height: 66vh;
  overflow-y: auto;
  padding-right: 6px;
}

.publish-wizard__hint {
  margin: 0 0 18px;
  padding: 11px 13px;
  border: 1px solid rgba(137, 154, 226, 0.2);
  border-radius: 10px;
  background: rgba(104, 125, 220, 0.1);
  color: #aab2c5;
  font-size: 13px;
  line-height: 1.7;
}

.publish-wizard__foot {
  display: flex;
  gap: 12px;
  justify-content: flex-end;
}

:global(.publish-wizard-modal .ant-modal-content) {
  overflow: hidden;
  border: 1px solid rgba(145, 158, 205, 0.2);
  border-radius: 18px;
  background: #171a24;
  box-shadow: 0 28px 80px rgba(0, 0, 0, 0.48);
}

:global(.publish-wizard-modal .ant-modal-header) {
  margin-bottom: 0;
  padding-bottom: 16px;
  border-bottom: 1px solid rgba(145, 158, 205, 0.14);
  background: #171a24;
}

:global(.publish-wizard-modal .ant-modal-title) {
  color: #e7eaf4;
}

:global(.publish-wizard-modal .ant-modal-body) {
  padding-top: 18px;
  background: #171a24;
}

:global(.publish-wizard-modal .ant-modal-footer) {
  border-top: 1px solid rgba(145, 158, 205, 0.14);
  background: #1c202b;
}

:global(.publish-wizard-modal .ant-modal-close) {
  color: #8f97aa;
}

:global(.publish-wizard-modal .ant-btn-default) {
  border-color: rgba(151, 164, 211, 0.24);
  background: rgba(41, 46, 61, 0.88);
  color: #cbd1e1;
  box-shadow: none;
}

:global(.publish-wizard-modal .ant-btn-default:not(:disabled):hover) {
  border-color: rgba(142, 159, 232, 0.56);
  background: rgba(65, 73, 103, 0.76);
  color: #f0f2fa;
}

:global(.publish-wizard-modal .ant-btn-primary) {
  border-color: #778bea;
  background: linear-gradient(135deg, #7188e5, #846fd6);
  box-shadow: none;
}
</style>
