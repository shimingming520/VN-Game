<template>
  <div
    class="plot-node"
    :class="{ 'is-selected': data.selected }"
    role="button"
    tabindex="0"
    @click="data.onSelect(data.id)"
    @keydown.enter.prevent="data.onSelect(data.id)"
    @keydown.space.prevent="data.onSelect(data.id)"
  >
    <Handle
      id="chapter-target"
      type="target"
      :position="Position.Left"
      class="!left-0 !top-1/2 !-translate-x-1/2 !-translate-y-1/2"
    />
    <Handle
      v-if="data.options.length === 1"
      id="chapter-source"
      type="source"
      :position="Position.Right"
      class="!right-0 !top-1/2 !-translate-y-1/2 !translate-x-1/2"
    />

    <div class="plot-node__title">
      {{ data.chapter?.chapterLabel || `第${data.index + 1}章` }}
      {{ data.title }}
    </div>

    <div class="plot-node__cover-wrap">
      <div
        class="plot-node__cover"
      >
        <a-image
          v-if="data.cover"
          :src="data.cover"
          :preview="false"
          class="plot-node-cover-image"
        />
        <Icon v-else icon="mdi:image-area" class="text-primary/35 h-12 w-12" />

        <a-button
          type="primary"
          size="small"
          class="!absolute left-1/2 top-1/2 !flex !-translate-x-1/2 !-translate-y-1/2 !items-center !gap-1 !rounded !px-3 !font-bold"
          @click.stop="data.onTune(data.chapter)"
        >
          <template #icon>
            <Icon icon="mdi:play" />
          </template>
          演出微调
        </a-button>
      </div>
    </div>

    <a-typography-paragraph
      :ellipsis="{ rows: 3, css: true, expandable: false }"
      class="!text-foreground/70 !px-4 !pb-3 !pt-2 !text-xs !font-semibold !leading-5"
    >
      {{ data.summary }}
    </a-typography-paragraph>

    <div v-if="data.options.length" class="plot-node__options">
      <div
        v-for="(option, index) in data.options"
        :key="`${data.id}-${index}`"
        class="plot-node__option"
        :title="option.label || '--'"
      >
        {{ option.label || '--' }}
        <Handle
          v-if="data.options.length > 1"
          :id="`option-${index}`"
          type="source"
          :position="Position.Right"
          class="!right-0 !top-1/2 !-translate-y-1/2 !translate-x-1/2"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core';
import Icon from '../CompatIcon.vue';
import type { PlotCanvasNodeData } from './plotCanvasTypes';

defineProps<{
  data: PlotCanvasNodeData;
}>();
</script>

<style scoped>
.plot-node {
  position: relative;
  width: 220px;
  overflow: visible;
  border: 1px solid #343849;
  border-radius: 8px;
  background: #1b1e29;
  box-shadow: 0 4px 14px #00000035;
  cursor: pointer;
}

.plot-node:hover,
.plot-node.is-selected {
  border-color: #7166f0;
}

.plot-node.is-selected {
  box-shadow: 0 0 0 2px #7166f033, 0 10px 24px #7166f02e;
}

.plot-node__title {
  overflow: hidden;
  padding: 12px 16px 8px;
  color: #e7eaf5;
  font-size: 13px;
  font-weight: 700;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.plot-node__cover-wrap {
  padding: 0 16px;
}

.plot-node__cover {
  position: relative;
  display: flex;
  aspect-ratio: 16 / 9;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  border: 3px solid #343849;
  border-radius: 5px;
  background: #10121a;
}

.plot-node__options {
  display: grid;
  gap: 4px;
  padding: 0 12px 12px;
}

.plot-node__option {
  position: relative;
  overflow: hidden;
  padding: 4px 8px;
  border-radius: 4px;
  background: #7166f02b;
  color: #a99df8;
  font-size: 12px;
  text-align: center;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.plot-node-cover-image,
.plot-node-cover-image :deep(.ant-image),
.plot-node-cover-image :deep(.ant-image-img) {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
}
</style>
