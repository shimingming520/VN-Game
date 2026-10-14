<script setup>
import { ref } from 'vue'

// 章节预览弹窗。iframe id 刻意不叫 iframe1：
// 父组件的「预览」按钮用 getElementById('export-main-iframe') 取全屏目标，
// 同 id 会让全屏请求打到弹窗里的 iframe 上。
const open = ref(false)
const iframeUrl = ref('')

function init(linkUrl) {
  iframeUrl.value = linkUrl || ''
  open.value = true
}

function handleCancel() {
  open.value = false
  iframeUrl.value = ''
}

defineExpose({ init })
</script>

<template>
  <a-modal
    :open="open"
    title="章节预览"
    :width="1000"
    :footer="null"
    :mask-closable="false"
    destroy-on-close
    centered
    @cancel="handleCancel"
  >
    <div class="chapter-view">
      <iframe
        v-if="iframeUrl"
        id="chapter-view-iframe"
        :src="iframeUrl"
        frameborder="0"
        allowfullscreen
      ></iframe>
      <p v-else class="chapter-view__empty">该章节暂无可预览的链接</p>
    </div>
  </a-modal>
</template>

<style scoped>
.chapter-view {
  display: flex;
  aspect-ratio: 16 / 9;
  overflow: hidden;
  border-radius: 10px;
  background: #10121a;
}

.chapter-view iframe {
  width: 100%;
  height: 100%;
  border: 0;
}

.chapter-view__empty {
  margin: auto;
  color: #767c8f;
  font-size: 13px;
}
</style>
