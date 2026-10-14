<script setup>
import { ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { publishProjectApi } from '@/api'
import PublishForm from '@/components/publish/PublishForm.vue'

// 单个发布抽屉。表单本体与批量发布共用 PublishForm，
// 提交走 creation-engine 的 /v1/projects/{task_id}/publish（后端再转中台 publish-cms）——
// 迁移代码里直连 CMS 的写法不能照抄，CMS token 不下发浏览器。
const emit = defineEmits(['success'])

const props = defineProps({
  ownerTaskId: { type: String, default: '' },
})

const PENDING_PUBLISH_KEY = 'engine_pending_inner_publish'

const open = ref(false)
const taskId = ref('')
const submitting = ref(false)
const formRef = ref(null)

function rememberPendingPublish(id) {
  try {
    sessionStorage.setItem(PENDING_PUBLISH_KEY, id)
  } catch {
    // 无痕模式下仍可在当前页面正常发布，只是不自动恢复抽屉。
  }
}

function pendingPublishTask() {
  try {
    return sessionStorage.getItem(PENDING_PUBLISH_KEY) || ''
  } catch {
    return ''
  }
}

function clearPendingPublish() {
  try {
    sessionStorage.removeItem(PENDING_PUBLISH_KEY)
  } catch {
    // ignore
  }
}

function errorText(error) {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  return error?.message || String(error || '未知错误')
}

function init(id, remember = true) {
  taskId.value = id || ''
  if (!taskId.value) return
  if (remember) rememberPendingPublish(taskId.value)
  open.value = true
}

function handleClose() {
  if (submitting.value) return
  open.value = false
  clearPendingPublish()
}

async function handleOk() {
  const payload = await formRef.value?.validateAndCollect()
  if (!payload) return
  submitting.value = true
  try {
    const { task_id, ...rest } = payload
    await publishProjectApi(task_id, rest)
    message.success('发布成功')
    open.value = false
    clearPendingPublish()
    emit('success')
  } catch (error) {
    message.error(`发布失败：${errorText(error)}`)
  } finally {
    submitting.value = false
  }
}

defineExpose({ init })

watch(
  () => props.ownerTaskId,
  (id) => {
    if (id && pendingPublishTask() === id) init(id, false)
  },
  { immediate: true },
)
</script>

<template>
  <a-drawer
    :open="open"
    placement="right"
    :width="700"
    destroy-on-close
    :mask-closable="false"
    root-class-name="release-drawer"
    @close="handleClose"
  >
    <template #title>
      <div class="release-drawer__title">
        <strong>发布信息</strong>
        <small>确认作品资料后发布到内容平台</small>
      </div>
    </template>
    <PublishForm v-if="taskId" ref="formRef" :task-id="taskId" auto-load />

    <template #footer>
      <div class="release-drawer__foot">
        <a-button :disabled="submitting" @click="handleClose">取消</a-button>
        <a-button type="primary" :loading="submitting" @click="handleOk">确定</a-button>
      </div>
    </template>
  </a-drawer>
</template>

<style scoped>
.release-drawer__title {
  display: grid;
  gap: 2px;
}

.release-drawer__title strong {
  color: #e7eaf4;
  font-size: 16px;
  font-weight: 600;
}

.release-drawer__title small {
  color: #8f97aa;
  font-size: 11px;
  font-weight: 400;
}

.release-drawer__foot {
  display: flex;
  gap: 12px;
  justify-content: center;
}

:global(.release-drawer .ant-drawer-content),
:global(.release-drawer .ant-drawer-header) {
  background: #171a24;
}

:global(.release-drawer .ant-drawer-header) {
  border-bottom-color: rgba(145, 158, 205, 0.14);
}

:global(.release-drawer .ant-drawer-body) {
  background:
    radial-gradient(500px 220px at 100% 0, rgba(103, 122, 211, 0.08), transparent 70%),
    #171a24;
}

:global(.release-drawer .ant-drawer-close) {
  color: #8f97aa;
}

:global(.release-drawer .ant-drawer-footer) {
  border-top-color: rgba(145, 158, 205, 0.14);
  background: #1c202b;
}

:global(.release-drawer .ant-btn-default) {
  border-color: rgba(151, 164, 211, 0.24);
  background: rgba(41, 46, 61, 0.88);
  color: #cbd1e1;
  box-shadow: none;
}

:global(.release-drawer .ant-btn-default:not(:disabled):hover) {
  border-color: rgba(142, 159, 232, 0.56);
  background: rgba(65, 73, 103, 0.76);
  color: #f0f2fa;
}

:global(.release-drawer .ant-btn-primary) {
  border-color: #778bea;
  background: linear-gradient(135deg, #7188e5, #846fd6);
  box-shadow: none;
}
</style>
