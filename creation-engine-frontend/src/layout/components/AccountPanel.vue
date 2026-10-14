<script setup>
import { computed, ref, watch } from 'vue'
import { authState } from '@/store/auth'
import { getMyUsageSummaryApi } from '@/api'
import UsageDetailModal from './UsageDetailModal.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
})
const emit = defineEmits(['update:open'])

const open = computed({
  get: () => props.open,
  set: (v) => emit('update:open', v),
})

const username = computed(() => authState.user?.username || '账户')
const account = computed(() => authState.user?.main_site_uid || '')
const initial = computed(() => (username.value || '?').slice(0, 1).toUpperCase())

const loading = ref(false)
const summary = ref(null)
const infoOpen = ref(false)
const detailOpen = ref(false)

const totalTokens = computed(() => summary.value?.total_tokens ?? 0)

async function fetchSummary() {
  loading.value = true
  try {
    summary.value = await getMyUsageSummaryApi()
  } catch {
    summary.value = null
  } finally {
    loading.value = false
  }
}

function close() {
  open.value = false
}

watch(
  () => props.open,
  (v) => {
    if (v) fetchSummary()
  }
)
</script>

<template>
  <!-- 账户信息面板:遮罩 + 居中弹窗(参照 1.html profile modal) -->
  <div v-if="open" class="ce-overlay" @click.self="close">
    <div class="ce-profile" @click.stop>
      <div class="ce-close" @click="close">✕</div>

      <div class="ce-profile-header">
        <div class="ce-avatar">{{ initial }}</div>
        <div class="ce-profile-info">
          <div class="ce-name">{{ username }}</div>
          <div class="ce-id-text">{{ account }}</div>
        </div>
      </div>

      <div class="ce-info-list">
        <div class="ce-info-row">
          <span class="ce-label">用户 ID</span>
          <span class="ce-value ce-value-id">{{ account }}</span>
        </div>
        <div class="ce-info-row">
          <span class="ce-label">用户昵称</span>
          <span class="ce-value">{{ username }}</span>
        </div>
        <div class="ce-info-row ce-info-row-click" @click="detailOpen = true">
          <span class="ce-label">Token 账单</span>
          <span class="ce-value">
            <a-spin v-if="loading" size="small" />
            <template v-else>{{ totalTokens.toLocaleString() }} Token</template>
            <span class="ce-info-icon" title="账单说明" @click.stop="infoOpen = true">i</span>
          </span>
        </div>
      </div>
    </div>
  </div>

  <!-- 账单说明弹窗 -->
  <a-modal v-model:open="infoOpen" title="Token 账单说明" :footer="null" width="480px">
    <div class="ce-info-text">
      <p>Token 账单统计你名下全部作品在生成过程中消耗的 Token 用量,包含:</p>
      <ul>
        <li><b>文本(LLM)</b>:剧情、对话等文本生成消耗的输入/输出 Token。</li>
        <li><b>图像</b>:角色立绘、场景图等按张计数。</li>
        <li><b>语音(TTS)</b>:配音合成按字符计数。</li>
        <li><b>音乐</b>:BGM 等按次计数。</li>
      </ul>
      <p>账单仅展示用量数量,不代表计费金额。点击账单可查看逐条明细并按类型、模型、日期筛选。</p>
    </div>
  </a-modal>

  <UsageDetailModal v-model:open="detailOpen" />
</template>

<style scoped>
.ce-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  background: transparent;
}
.ce-profile {
  position: fixed;
  top: 96px;
  right: 28px;
  width: 320px;
  padding: 20px;
  border: 1px solid rgba(255, 255, 255, 0.05);
  border-radius: 12px;
  background: #272a37;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
}
.ce-close {
  position: absolute;
  top: 16px;
  right: 16px;
  color: #9ca3af;
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  transition: color 0.2s;
}
.ce-close:hover {
  color: #e5e7eb;
}
.ce-profile-header {
  display: flex;
  align-items: center;
  margin-bottom: 24px;
}
.ce-avatar {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 48px;
  height: 48px;
  margin-right: 12px;
  border: 1px solid #3b3f54;
  border-radius: 50%;
  background: linear-gradient(145deg, #333b6a, #222746);
  color: #eef0ff;
  font-size: 18px;
  font-weight: 600;
  overflow: hidden;
}
.ce-name {
  margin-bottom: 4px;
  color: #e5e7eb;
  font-size: 16px;
  font-weight: 600;
}
.ce-id-text {
  color: #9ca3af;
  font-size: 13px;
  word-break: break-all;
}
.ce-info-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.ce-info-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-radius: 8px;
  background: #1f222d;
  font-size: 13px;
}
.ce-info-row-click {
  cursor: pointer;
  transition: background 0.2s;
}
.ce-info-row-click:hover {
  background: #262a38;
}
.ce-label {
  color: #9ca3af;
}
.ce-value {
  display: flex;
  align-items: center;
  color: #e5e7eb;
  font-weight: 500;
}
.ce-value-id {
  max-width: 190px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ce-info-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px;
  height: 14px;
  margin-left: 6px;
  border: 1px solid #9ca3af;
  border-radius: 50%;
  color: #9ca3af;
  font-size: 10px;
  font-style: normal;
  cursor: pointer;
}
.ce-info-icon:hover {
  border-color: #e5e7eb;
  color: #e5e7eb;
}
.ce-info-text {
  color: #4a4f5c;
  font-size: 13px;
  line-height: 1.7;
}
.ce-info-text ul {
  padding-left: 18px;
}
</style>
