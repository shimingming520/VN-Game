<script setup>
import { onMounted, ref } from 'vue'
import ProjectsSection from '@/components/projects/ProjectsSection.vue'
import { getProjectsApi } from '@/api'

/** 全部项目页:一次拉满(后端单页上限 100),不足再翻页补齐 */
const PAGE_SIZE = 100

const loading = ref(false)
const projects = ref([])

async function fetchProjects() {
  loading.value = true
  try {
    const first = await getProjectsApi({ limit: PAGE_SIZE, offset: 0 })
    const items = first?.items || []
    const total = Number(first?.total) || items.length

    // 超过一页时继续翻:这里项目数量级很小,顺序补齐比做无限滚动更省事也更好懂
    for (let offset = PAGE_SIZE; offset < total; offset += PAGE_SIZE) {
      const next = await getProjectsApi({ limit: PAGE_SIZE, offset })
      items.push(...(next?.items || []))
    }
    projects.value = items
  } finally {
    loading.value = false
  }
}

onMounted(fetchProjects)
</script>

<template>
  <ProjectsSection variant="all" :projects="projects" @refresh="fetchProjects" />
</template>
