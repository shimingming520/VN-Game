<script setup>
import { computed } from 'vue'
import { PictureOutlined } from '@ant-design/icons-vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  models: { type: Array, default: () => [] },
  label: { type: String, default: '图像模型' },
  emptyDescription: { type: String, default: '暂无图像模型' },
  loading: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue', 'generate'])

const selectedModel = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value),
})

function handleGenerate() {
  emit('generate')
}
</script>

<template>
  <div class="model-generate">
    <a-popover trigger="click" placement="topLeft">
      <template #content>
        <div class="model-generate__panel">
          <div class="model-generate__panel-title">
            <PictureOutlined />
            <span>{{ label }}</span>
          </div>
          <a-radio-group
            v-if="models.length"
            v-model:value="selectedModel"
            class="model-generate__list"
          >
            <a-radio
              v-for="model in models"
              :key="model"
              :value="model"
              class="model-generate__item"
              :class="{ 'is-active': selectedModel === model }"
            >
              {{ model }}
            </a-radio>
          </a-radio-group>
          <a-empty v-else :description="emptyDescription" />
        </div>
      </template>
      <a-button html-type="button" :disabled="disabled">
        <template #icon><PictureOutlined /></template>
        {{ selectedModel || label }}
      </a-button>
    </a-popover>

    <a-button
      type="primary"
      :loading="loading"
      :disabled="disabled || !selectedModel"
      @click="handleGenerate"
    >
      生成
    </a-button>
  </div>
</template>

<style scoped>
.model-generate {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

.model-generate__panel {
  width: 240px;
}

.model-generate__panel-title {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-bottom: 8px;
  color: #8a91a8;
  font-size: 13px;
}

.model-generate__list {
  display: flex;
  max-height: 180px;
  flex-direction: column;
  gap: 4px;
  overflow-y: auto;
}

.model-generate__item {
  padding: 6px 8px;
  border-radius: 6px;
  transition: background 0.2s;
}

.model-generate__item.is-active {
  background: #7166f01f;
}
</style>
