<script setup>
import { computed, ref } from 'vue'

/**
 * 单个项目卡片(视觉与 demo Projects 组件 1:1,样式全部取 chilang.css 的 .project-* )。
 *
 * 卡片本身不做任何数据请求:选中态、菜单开合、弹窗都由父级 ProjectsSection 统一持有,
 * 否则「同一时刻只能展开一个菜单」「批量发布选了哪些」这类跨卡片状态没法收口。
 */
const props = defineProps({
  project: { type: Object, required: true },
  selected: { type: Boolean, default: false },
  /** 批量发布被未完成项目拦下时,给这张卡片套高亮抖动 */
  blocked: { type: Boolean, default: false },
  menuOpen: { type: Boolean, default: false },
})

const emit = defineEmits(['open', 'toggle-select', 'toggle-menu', 'rename', 'similar', 'publish', 'delete'])

// 封面对象可能还没生成好(后端不逐条探测存在性),加载失败就回落占位图标
const coverFailed = ref(false)

const showCover = computed(() => !!props.project.cover && !coverFailed.value)
const progress = computed(() => Number(props.project.progress) || 0)
const isCompleted = computed(() => progress.value >= 100)
const updatedDate = computed(() => (props.project.update_time || '').slice(0, 10))

function onCardKeydown(e) {
  if (e.key !== 'Enter' && e.key !== ' ') return
  e.preventDefault()
  emit('open', props.project)
}
</script>

<template>
  <article
    class="project-card"
    :class="{ selected, 'publish-blocked': blocked }"
    :data-project-id="project.task_id"
    :tabindex="0"
    @click="emit('open', project)"
    @keydown="onCardKeydown"
  >
    <div class="project-cover" :class="{ 'has-cover': showCover }">
      <img v-if="showCover" :src="project.cover" :alt="`${project.name}封面`" @error="coverFailed = true" />
      <svg v-else width="38" height="38" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="m12 3 9 5-9 5-9-5 9-5Z" />
        <path d="m3 12 9 5 9-5M3 16l9 5 9-5" />
      </svg>

      <span class="project-status" :class="{ completed: isCompleted }">{{ progress }}%</span>

      <!-- 勾选框与更多按钮都要 stop:它们落在卡片内,冒泡上去会顺带打开项目 -->
      <button
        class="project-select-button"
        type="button"
        :aria-label="`${selected ? '取消选择' : '选择'}${project.name}`"
        :aria-pressed="selected"
        @click.stop="emit('toggle-select', project)"
      >
        <template v-if="selected">✓</template>
      </button>
    </div>

    <div class="project-info">
      <div class="project-info-main">
        <h3 :title="project.name">{{ project.name }}</h3>
        <p>{{ updatedDate }}</p>
      </div>

      <div class="project-more-wrap">
        <button
          class="project-more-button"
          type="button"
          :aria-label="`${project.name}更多操作`"
          :aria-expanded="menuOpen"
          @click.stop="emit('toggle-menu', project)"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <circle cx="5" cy="12" r="1" fill="currentColor" />
            <circle cx="12" cy="12" r="1" fill="currentColor" />
            <circle cx="19" cy="12" r="1" fill="currentColor" />
          </svg>
        </button>

        <div v-if="menuOpen" class="project-more-menu" @click.stop>
          <button type="button" @click="emit('open', project)">打开</button>
          <button type="button" @click="emit('rename', project)">重命名</button>
          <button type="button" @click="emit('similar', project)">创建相似品</button>
          <button type="button" @click="emit('publish', project)">发布</button>
          <button type="button" class="project-delete-action" @click="emit('delete', project)">删除</button>
        </div>
      </div>
    </div>
  </article>
</template>

<style scoped>
/* chilang.css 里 .project-info h3 已经写了 ellipsis,但 flex 子项默认 min-width:auto,
   名字过长时会把「更多」按钮挤出卡片。补一条 min-width:0 让省略号真正生效。 */
.project-info-main {
  min-width: 0;
  flex: 1 1 auto;
}
</style>
