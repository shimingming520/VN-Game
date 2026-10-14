<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import GameWarehouse from '@/components/GameWarehouse.vue'
import ProjectsSection from '@/components/projects/ProjectsSection.vue'
import ScriptComposer from '@/components/ScriptComposer.vue'
import { getProjectsApi } from '@/api'

const SLOGANS = [
  '捕捉每一份热爱,打造你的首款AI游戏',
  '上传一部小说,收获一款可玩的游戏',
  '从文字到画面,创作只差一步',
]

const sloganIndex = ref(0)
const projects = ref([])
const pendingProjects = new Map()
let sloganTimer = null
let projectRefreshTimer = null

async function fetchProjects() {
  try {
    const res = await getProjectsApi({ limit: 7, offset: 0 })
    const rows = res?.items || []
    rows.forEach((item) => pendingProjects.delete(item.task_id))
    projects.value = [
      ...pendingProjects.values(),
      ...rows.filter((item) => !pendingProjects.has(item.task_id)),
    ]
  } catch {
    projects.value = [...pendingProjects.values()]
  }
}

function handleUploaded(payload) {
  const taskId = payload?.taskId
  if (!taskId) return
  const optimistic = {
    task_id: taskId,
    name: payload?.project_name || payload?.novel_filename || '新作品',
    cover: '',
    progress: 0,
    status: 0,
    is_published: false,
    update_time: new Date().toISOString(),
  }
  pendingProjects.set(taskId, optimistic)
  projects.value = [optimistic, ...projects.value.filter((item) => item.task_id !== taskId)]
  // 上传接口已完成落库；稍后再以服务端数据校正名称、状态和排序。
  if (projectRefreshTimer) window.clearTimeout(projectRefreshTimer)
  projectRefreshTimer = window.setTimeout(fetchProjects, 500)
}

onMounted(() => {
  fetchProjects()
  sloganTimer = window.setInterval(() => {
    sloganIndex.value = (sloganIndex.value + 1) % SLOGANS.length
  }, 4000)
})

onBeforeUnmount(() => {
  if (sloganTimer) window.clearInterval(sloganTimer)
  if (projectRefreshTimer) window.clearTimeout(projectRefreshTimer)
})
</script>

<template>
  <section class="creation-section">
    <h1 class="slogan-loop">
      <span :key="sloganIndex" class="slogan-loop__text">{{ SLOGANS[sloganIndex] }}</span>
    </h1>

    <ScriptComposer
      class="home-script-composer"
      @uploaded="handleUploaded"
      @submitted-batch="fetchProjects"
    />
  </section>

  <ProjectsSection v-if="projects.length" variant="compact" :projects="projects" @refresh="fetchProjects" />

  <GameWarehouse />
</template>

<style scoped>
.slogan-loop {
  min-height: 1.4em;
}

.slogan-loop__text {
  display: inline-block;
  animation: slogan-in 0.6s ease;
}

/* 首页的禁用遮罩只负责表现，按钮和由 body 承载的 Tooltip 保持可交互。 */
.home-script-composer :deep(.drop-area.is-upload-disabled::after) {
  z-index: 1;
}

.home-script-composer :deep(.creator-controls),
.home-script-composer :deep(.create-actions) {
  position: absolute;
  z-index: 10;
}

@keyframes slogan-in {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .slogan-loop__text {
    animation: none;
  }
}
</style>
