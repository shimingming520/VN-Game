<template>
  <div
    class="create-panel plot-interpretation"
  >
    <div class="plot-interpretation__head">
      <div>
        <span class="plot-interpretation__eyebrow">STEP 03 · PERFORMANCE</span>
        <h3>剧情演绎</h3>
        <p>查看章节走向与画面关系，进入章节后可微调场景景别、角色站位和素材表现。</p>
      </div>
      <div class="plot-interpretation__toolbar">
        <a-segmented
          v-model:value="activeView"
          :options="viewOptions"
          class="plot-view-switch"
        />
      </div>
    </div>

    <div
      v-if="loading"
      class="plot-interpretation__state"
    >
      <a-spin size="large" tip="正在整理剧情演绎数据…" />
    </div>

    <div
      v-else-if="plotError"
      class="plot-interpretation__state plot-interpretation__state--error"
    >
      <a-alert :message="plotError" type="warning" show-icon />
      <a-button @click="fetchPlotData">重新加载</a-button>
    </div>

    <div
      v-else-if="!chapterList.length"
      class="plot-interpretation__state plot-interpretation__state--empty"
    >
      <Icon icon="mdi:movie-open-outline" class="plot-interpretation__empty-icon" />
      <strong>暂无可演绎章节</strong>
      <span>章节生成完成后会自动出现在这里，可稍后重新加载。</span>
      <a-button @click="fetchPlotData">重新加载</a-button>
    </div>

    <div
      v-else-if="activeView === 'Script'"
      ref="scrollContainer"
      class="plot-interpretation__script"
    >
      <div class="plot-interpretation__chapter-list">
        <div
          v-for="(item, index) in chapterList"
          :key="item.id || index"
          class="plot-interpretation__chapter-card"
          :class="{
            'is-selected': selectedChapterId === getChapterId(item, index),
          }"
          role="button"
          tabindex="0"
          @click="handleChapterSelect(item, index)"
          @keydown.enter.prevent="handleChapterSelect(item, index)"
          @keydown.space.prevent="handleChapterSelect(item, index)"
        >
          <div class="plot-interpretation__chapter-info">
            <div class="plot-interpretation__chapter-heading">
              <a-tag
                class="plot-interpretation__chapter-tag"
              >
                {{ getChapterLabel(item, index) }}
              </a-tag>
              <a-typography-paragraph
                :ellipsis="{ rows: 1, css: true, expandable: false }"
                :title="getChapterTitle(item)"
                class="plot-interpretation__chapter-title"
              >
                {{
                  getChapterTitle(item).length > 10
                    ? `${getChapterTitle(item).slice(0, 10)}...`
                    : getChapterTitle(item)
                }}
              </a-typography-paragraph>
            </div>

            <div
              class="plot-interpretation__chapter-summary"
            >
              {{
                getChapterSummary(item).length > 500
                  ? `${getChapterSummary(item).slice(0, 500)}...`
                  : getChapterSummary(item)
              }}
            </div>

            <div
              v-if="getChapterOptions(item, index).length"
              class="plot-interpretation__options"
            >
              <div
                v-for="(option, optionIndex) in getChapterOptions(item, index)"
                :key="`${item.id || index}-${optionIndex}`"
                class="plot-interpretation__option"
                :class="{
                  'has-divider':
                    optionIndex < getChapterOptions(item, index).length - 1,
                }"
              >
                <span
                  class="plot-interpretation__option-label"
                  :title="option.label || '--'"
                >
                  {{ getOptionDisplayText(option.label || '--') }}
                </span>
                <Icon
                  icon="mdi:arrow-right"
                  class="plot-interpretation__option-arrow"
                />
                <span class="plot-interpretation__option-target-no">
                  {{ option.targetNo }}
                </span>
                <span class="plot-interpretation__option-target-title">
                  {{ option.targetTitle }}
                </span>
              </div>
            </div>
          </div>

          <div class="plot-interpretation__cover">
            <a-image
              v-if="item.cover"
              :src="item.cover"
              :preview="false"
              class="plot-cover-image"
            />
            <div
              v-else
              class="plot-interpretation__cover-empty"
            >
              <Icon icon="mdi:image-area" class="plot-interpretation__cover-empty-icon" />
            </div>

            <a-button
              type="primary"
              class="plot-interpretation__tune-button"
              @click.stop="handleTune(item)"
            >
              <template #icon>
                <Icon icon="mdi:play" class="plot-interpretation__tune-icon" />
              </template>
              演出微调
            </a-button>
          </div>
        </div>
      </div>
    </div>

    <div
      v-else
      ref="canvasContainerRef"
      class="canvas-fullscreen-container plot-interpretation__canvas"
    >
      <PlotCanvas
        ref="plotCanvasRef"
        class="plot-interpretation__canvas-inner"
        :chapters="chapterList"
        :selected-id="selectedChapterId"
        @select="handleCanvasSelect"
        @tune="handleTune"
      />
    </div>

    <div class="plot-interpretation__footer">
      <!-- <a-button type="primary" @click="emit('cancel')"> 取消 </a-button> -->
      <a-button v-if="activeView === 'Canvas'" :disabled="!chapterList.length" @click="handleCanvasFullscreen">
        全屏
      </a-button>
      <a-button
        type="primary"
        :disabled="loading || confirmLoading || published"
        :loading="confirmLoading"
        @click="handleNextStep"
      >
        下一步
      </a-button>
    </div>

    <!-- 演出微调 -->
    <PlotTuneModal
      ref="plotTuneModalRef"
      :is-renewal="true"
      :task-id="props.taskId"
      :published="props.published"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { message } from 'ant-design-vue';
import { confirmPerformanceApi, getRuntimeConfigApi, getStoryTreeApi } from '@/api';
import Icon from '../CompatIcon.vue';
import PlotCanvas from './PlotCanvas.vue';
import PlotTuneModal from './PlotTuneModal.vue';
import type { PlotChapterItem } from './plotCanvasTypes.js';

const props = defineProps<{
  published?: boolean;
  taskId?: string;
}>();

const emit = defineEmits<{
  confirmed: [];
}>();

interface PlotChapterOption {
  label: string;
  targetNo: string;
  targetTitle: string;
}

/** 故事树子节点关系 */
export interface StoryTreeChild {
  branch_threshold: null | number;
  branch_variable: null | string;
  child_id: string;
  choice_text: string;
}

/** 故事树节点 */
export interface StoryTreeNode {
  children: StoryTreeChild[];
  depth: number;
  is_branch: boolean;
  is_original: boolean;
  node_id: string;
  parent_ids: string[];
  summary: string;
  title: string;
}

/** 故事树接口响应 */
export interface StoryTreeResponse {
  nodes: StoryTreeNode[];
  task_id: string;
}

const loading = ref(false); // 加载中
const plotError = ref('');
const confirmLoading = ref(false); // 确认演绎微调加载中
const activeView = ref('Script'); // 当前视图
const chapterList = ref<PlotChapterItem[]>([]); // 剧情章节列表
const selectedChapterId = ref(''); // 当前选中的章节ID
const plotCanvasRef = ref<InstanceType<typeof PlotCanvas> | null>(null); // Canvas 画布
const plotTuneModalRef = ref<InstanceType<typeof PlotTuneModal> | null>(null); // 演出微调弹窗
const canvasContainerRef = ref<HTMLDivElement | null>(null); // Canvas 全屏容器

// 顶部视图切换选项
const viewOptions = computed(() => [
  { label: 'Script', value: 'Script' },
  { label: 'Canvas', value: 'Canvas' },
]);

onMounted(() => {
  fetchPlotData();
});

/**
 * 获取章节标题
 * @param item 章节数据
 */
function getChapterTitle(item: PlotChapterItem): string {
  return item?.title || item?.name || '--';
}

/**
 * 获取章节唯一ID
 * @param item 章节数据
 * @param index 章节索引
 */
function getChapterId(item: PlotChapterItem, index: number): string {
  return String(item?.id ?? item?.link ?? `chapter-${index}`);
}

/**
 * 获取章节显示文案
 * @param item 章节数据
 * @param index 章节索引
 */
function getChapterLabel(item: PlotChapterItem, index: number): string {
  return item?.chapterLabel || `第${index + 1}章`;
}

/**
 * 获取章节简介
 * @param item 章节数据
 */
function getChapterSummary(item: PlotChapterItem): string {
  return (
    item?.summary ||
    item?.description ||
    '--'
  );
}

/**
 * 获取选项文案
 * @param option 选项数据
 * @param index 选项索引
 */
function getOptionLabel(option: any, index: number): string {
  if (typeof option?.choice_text === 'string') {
    return option.choice_text;
  }

  return (
    option?.label ||
    option?.text ||
    option?.content ||
    option?.name ||
    `--`
  );
}

/**
 * 获取最多展示 8 个字的选项文案
 * @param text 完整选项文案
 */
function getOptionDisplayText(text: string): string {
  /** 按 Unicode 字符拆分的选项文案 */
  const characters = Array.from(text);
  return characters.length > 8 ? `${characters.slice(0, 8).join('')}...` : text;
}

/**
 * 获取目标章节信息
 * @param option 选项数据
 * @param fallbackIndex 默认章节索引
 */
function getTargetChapterInfo(option: any, fallbackIndex: number) {
  const targetId =
    option?.target_id ||
    option?.targetId ||
    option?.next_id ||
    option?.nextChapterId ||
    option?.chapter_id;
  const targetIndex = chapterList.value.findIndex(
    (chapter) => chapter?.id === targetId,
  );
  const finalIndex = targetIndex >= 0 ? targetIndex : fallbackIndex;
  const targetChapter = chapterList.value[finalIndex];

  return {
    targetNo: getChapterLabel(targetChapter || {}, finalIndex),
    targetTitle: getChapterTitle(targetChapter || {}),
  };
}

/**
 * 获取章节走向选项
 * @param item 章节数据
 * @param index 章节索引
 */
function getChapterOptions(
  item: PlotChapterItem,
  index: number,
): PlotChapterOption[] {
  const rawOptions = Array.isArray(item?.options) ? item.options : []; // 当前节点下级选项

  return rawOptions.map((option, optionIndex) => {
    const targetInfo = getTargetChapterInfo(option, index + optionIndex + 1);

    return {
      label: getOptionLabel(option, optionIndex),
      targetNo: targetInfo.targetNo,
      targetTitle: targetInfo.targetTitle,
    };
  });
}

/**
 * 获取微调章节索引
 * @param item 章节数据
 */
function getTuneChapterIndex(item: PlotChapterItem): number {
  const itemKey = String(item?.id ?? item?.link ?? '');
  const chapterIndex = chapterList.value.findIndex((chapter, index) => {
    if (chapter === item) {
      return true;
    }

    return itemKey ? getChapterId(chapter, index) === itemKey : false;
  });

  return chapterIndex >= 0 ? chapterIndex : 0;
}

/**
 * 将故事树节点转换为剧情章节数据
 * @param node 故事树节点
 */
function normalizeStoryTreeNode(node: StoryTreeNode): PlotChapterItem {
  const children = Array.isArray(node.children) ? node.children : []; // 当前节点下级数据
  /** 页面统一使用的剧情走向选项 */
  const options = children.map((child) => ({
    ...child,
    label: child.choice_text,
    target_id: child.child_id,
  }));

  return {
    ...node,
    id: node.node_id,
    options,
  };
}

/**
 * 根据节点 depth 和接口顺序生成章节显示文案
 * @param depths 故事节点章节层级列表
 */
function createChapterLabels(depths: readonly number[]): string[] {
  /** 每个章节层级的节点总数 */
  const depthCounts = new Map<number, number>();
  depths.forEach((depth) => {
    depthCounts.set(depth, (depthCounts.get(depth) ?? 0) + 1);
  });

  /** 每个章节层级当前出现次数 */
  const depthIndexes = new Map<number, number>();
  return depths.map((depth) => {
    /** 当前节点在同章节层级中的顺序 */
    const depthIndex = (depthIndexes.get(depth) ?? 0) + 1;
    depthIndexes.set(depth, depthIndex);

    return (depthCounts.get(depth) ?? 0) > 1
      ? `第${depth}章-${depthIndex}`
      : `第${depth}章`;
  });
}

/**
 * 获取剧情章节数据
 */
async function fetchPlotData() {
  if (!props.taskId) {
    chapterList.value = [];
    selectedChapterId.value = '';
    return;
  }

  loading.value = true;
  plotError.value = '';
  try {
    /** 故事树接口响应 */
    const [response, runtimeConfig] = await Promise.all([
      getStoryTreeApi(props.taskId),
      getRuntimeConfigApi({ skipErrorToast: true }),
    ]);
    /** 接口返回的故事树节点 */
    const { nodes: responseNodes = [] } = response ?? {};
    /** 有效故事树节点 */
    const storyNodes: StoryTreeNode[] = Array.isArray(responseNodes)
      ? responseNodes
      : [];
    /** 按 depth 和接口顺序生成的章节显示文案 */
    const chapterLabels = createChapterLabels(
      storyNodes.map((node) => node.depth),
    );
    const gameBaseUrl = String(runtimeConfig?.game_base_url || '').replace(/\/$/, '');
    chapterList.value = storyNodes.map((node, index) => ({
      ...normalizeStoryTreeNode(node),
      chapterLabel: chapterLabels[index],
      link: gameBaseUrl
        ? `${gameBaseUrl}/${props.taskId}/chapter_${node.depth}_${node.node_id.replace(/^node_/, '')}/`
        : '',
    }));

    syncSelectedChapter();
  } catch (error) {
    console.error('获取剧情章节失败:', error);
    chapterList.value = [];
    selectedChapterId.value = '';
    const detail = error?.response?.data?.detail;
    plotError.value = typeof detail === 'string' && detail.trim()
      ? `获取剧情章节失败：${detail.trim()}`
      : '获取剧情章节失败，请稍后重试';
  } finally {
    loading.value = false;
  }
}

/**
 * 同步默认选中章节
 */
function syncSelectedChapter() {
  const selectedExists = chapterList.value.some(
    (item, index) => getChapterId(item, index) === selectedChapterId.value,
  );

  if (!selectedExists) {
    selectedChapterId.value = chapterList.value[0]
      ? getChapterId(chapterList.value[0], 0)
      : '';
  }
}

/**
 * 选择脚本章节卡片
 * @param item 章节数据
 * @param index 章节索引
 */
function handleChapterSelect(item: PlotChapterItem, index: number) {
  selectedChapterId.value = getChapterId(item, index);
}

/**
 * 选择画布章节节点
 * @param id 章节ID
 */
function handleCanvasSelect(id: string) {
  selectedChapterId.value = id;
}

/**
 * 处理演出微调
 * @param item 章节数据
 */
function handleTune(item: PlotChapterItem) {
  const chapterIndex = getTuneChapterIndex(item);

  selectedChapterId.value = getChapterId(item, chapterIndex);
  plotTuneModalRef.value?.init({
    chapter: item,
    index: chapterIndex,
  });
}

/** 全屏展示 Canvas 画布 */
async function handleCanvasFullscreen() {
  /** Canvas 画布容器 */
  const canvasContainer = canvasContainerRef.value;
  if (!canvasContainer?.requestFullscreen) {
    message.warning('当前浏览器不支持全屏展示');
    return;
  }

  try {
    await canvasContainer.requestFullscreen();
    requestAnimationFrame(() => {
      plotCanvasRef.value?.fitView();
    });
  } catch (error) {
    console.error('Canvas 画布全屏展示失败:', error);
    message.error('全屏展示失败，请稍后重试');
  }
}

/** 确认演绎微调并进入下一步 */
async function handleNextStep() {
  if (!props.taskId) {
    message.warning('当前任务不存在，无法确认演绎微调');
    return;
  }
  if (props.published) {
    message.warning('作品已发布，不可修改');
    return;
  }

  confirmLoading.value = true;
  try {
    await confirmPerformanceApi(props.taskId);
    emit('confirmed');
  } catch (error) {
    console.error('确认演绎微调失败:', error);
  } finally {
    confirmLoading.value = false;
  }
}
</script>

<style scoped>
@import '../../styles/panel.css';

.plot-interpretation {
  display: flex;
  min-height: 560px;
  flex-direction: column;
}

.plot-interpretation__head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 20px;
  padding-bottom: 18px;
  border-bottom: 1px solid rgba(145, 158, 205, 0.13);
}

.plot-interpretation__eyebrow {
  display: block;
  margin-bottom: 5px;
  color: #8496d1;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.14em;
}

.plot-interpretation__head h3 {
  margin: 0;
  color: #e9ecf5;
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.025em;
}

.plot-interpretation__head p {
  max-width: 720px;
  margin: 7px 0 0;
  color: #929aaf;
  font-size: 13px;
  line-height: 1.65;
}

.plot-interpretation__toolbar {
  display: flex;
  justify-content: flex-end;
  flex: none;
}

.plot-view-switch {
  padding: 4px;
  border: 1px solid rgba(145, 158, 205, 0.16);
  border-radius: 11px;
  background: rgba(12, 15, 23, 0.5) !important;
}

.plot-view-switch :deep(.ant-segmented-item) {
  min-width: 92px;
  border-radius: 8px;
  color: #858da3;
  transition: color 0.18s ease, background 0.18s ease;
}

.plot-view-switch :deep(.ant-segmented-item-selected) {
  background: rgba(112, 132, 224, 0.18) !important;
  color: #b5c0f5 !important;
  box-shadow: none !important;
}

.plot-interpretation__state {
  display: flex;
  min-height: 420px;
  flex: 1;
  align-items: center;
  justify-content: center;
}

.plot-interpretation__state--error {
  flex-direction: column;
  gap: 14px;
}

.plot-interpretation__state--empty {
  flex-direction: column;
  gap: 8px;
  margin: 4px 0;
  border: 1px dashed rgba(145, 158, 205, 0.22);
  border-radius: 16px;
  background: rgba(12, 15, 22, 0.28);
  color: #7f879d;
}

.plot-interpretation__state--empty strong {
  color: #d9deeb;
  font-size: 15px;
  font-weight: 600;
}

.plot-interpretation__state--empty span {
  margin-bottom: 8px;
  font-size: 12px;
}

.plot-interpretation__empty-icon {
  width: 48px;
  height: 48px;
  margin-bottom: 3px;
  color: #7182bd;
}

.plot-interpretation__script {
  max-height: 520px;
  flex: 1;
  overflow-y: auto;
  padding-right: 4px;
}

.plot-interpretation__chapter-list {
  display: grid;
  gap: 14px;
}

.plot-interpretation__chapter-card {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 220px;
  gap: 24px;
  padding: 20px;
  border: 1px solid rgba(145, 158, 205, 0.18);
  border-radius: 15px;
  background: linear-gradient(145deg, rgba(35, 40, 54, 0.82), rgba(25, 29, 40, 0.82));
  cursor: pointer;
  transition: transform 0.18s ease, border-color 0.18s ease, background 0.18s ease;
}

.plot-interpretation__chapter-card:hover {
  border-color: rgba(136, 154, 232, 0.48);
  background: linear-gradient(145deg, rgba(41, 47, 65, 0.9), rgba(27, 32, 44, 0.9));
  transform: translateY(-1px);
}

.plot-interpretation__chapter-card.is-selected {
  border-color: rgba(139, 158, 238, 0.66);
  background: linear-gradient(145deg, rgba(49, 56, 79, 0.94), rgba(29, 34, 48, 0.94));
  box-shadow: 0 0 0 3px rgba(105, 126, 221, 0.1), 0 12px 26px rgba(4, 8, 20, 0.16);
}

.plot-interpretation__chapter-info {
  min-width: 0;
}

.plot-interpretation__chapter-heading {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
}

.plot-interpretation__chapter-tag {
  flex: none;
  margin: 0;
  padding: 3px 11px;
  border: 1px solid rgba(135, 154, 233, 0.22);
  border-radius: 999px;
  background: rgba(104, 125, 220, 0.12);
  color: #aebaf3;
  font-size: 12px;
  font-weight: 600;
}

.plot-interpretation__chapter-title {
  min-width: 0;
  margin-bottom: 0 !important;
  color: #e8ebf4;
  font-size: 16px;
  font-weight: 600;
}

.plot-interpretation__chapter-summary {
  margin-bottom: 14px;
  color: #a7aec0;
  font-size: 13px;
  font-weight: 400;
  line-height: 1.75;
}

.plot-interpretation__options {
  padding: 5px 12px;
  border: 1px solid rgba(145, 158, 205, 0.14);
  border-radius: 11px;
  background: rgba(13, 16, 24, 0.3);
}

.plot-interpretation__option {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 42px;
  color: #969eb2;
  font-size: 12px;
}

.plot-interpretation__option.has-divider {
  border-bottom: 1px solid rgba(145, 158, 205, 0.11);
}

.plot-interpretation__option-label {
  max-width: 170px;
  flex: none;
  overflow: hidden;
  padding: 4px 9px;
  border-radius: 999px;
  background: rgba(100, 111, 151, 0.18);
  color: #b7bed0;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.plot-interpretation__option-arrow {
  width: 18px;
  height: 18px;
  flex: none;
  color: #69738c;
}

.plot-interpretation__option-target-no {
  flex: none;
  color: #93a6ef;
  font-weight: 600;
}

.plot-interpretation__option-target-title {
  min-width: 0;
  overflow: hidden;
  color: #aeb9e9;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.plot-interpretation__cover {
  position: relative;
  display: flex;
  aspect-ratio: 16 / 9;
  align-self: center;
  overflow: hidden;
  padding: 6px;
  border: 1px solid rgba(145, 158, 205, 0.2);
  border-radius: 12px;
  background: rgba(11, 14, 21, 0.72);
}

.plot-interpretation__cover-empty {
  display: flex;
  width: 100%;
  height: 100%;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  border-radius: 8px;
  background: rgba(104, 125, 220, 0.09);
}

.plot-interpretation__cover-empty-icon {
  width: 54px;
  height: 54px;
  color: rgba(137, 153, 223, 0.32);
}

.plot-interpretation__tune-button {
  position: absolute !important;
  bottom: 14px;
  left: 50%;
  display: flex !important;
  height: 38px !important;
  align-items: center;
  gap: 5px;
  padding: 0 16px !important;
  transform: translateX(-50%);
}

.plot-interpretation__tune-icon {
  width: 17px;
  height: 17px;
}

.plot-interpretation__cover :deep(.ant-image),
.plot-interpretation__cover :deep(.ant-image-img) {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.plot-interpretation__canvas {
  min-height: 480px;
  flex: 1;
  overflow: hidden;
  border: 1px solid rgba(145, 158, 205, 0.16);
  border-radius: 14px;
  background: rgba(12, 15, 23, 0.38);
}

.plot-interpretation__canvas-inner {
  min-height: 0;
}

.plot-interpretation__footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 18px;
}

.canvas-fullscreen-container:fullscreen {
  padding: 16px;
  background: #10121a;
}

@media (max-width: 860px) {
  .plot-interpretation__chapter-card {
    grid-template-columns: 1fr;
  }
}
</style>
