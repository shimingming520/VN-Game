<template>
  <div
    class="plot-canvas"
  >
    <VueFlow
      v-if="flowNodes.length"
      v-model:edges="flowEdges"
      v-model:nodes="flowNodes"
      class="plot-flow-canvas"
      :default-edge-options="defaultEdgeOptions"
      :fit-view-on-init="true"
      :max-zoom="1.3"
      :min-zoom="0.35"
      :nodes-draggable="false"
      :nodes-connectable="false"
      :pan-on-scroll="true"
      :zoom-on-double-click="false"
      @pane-ready="handlePaneReady"
    >
      <template #node-chapter="nodeProps">
        <PlotChapterNode :data="nodeProps.data" />
      </template>
      <template #edge-branch="edgeProps">
        <BaseEdge
          :id="edgeProps.id"
          :marker-end="edgeProps.markerEnd"
          :path="getBranchEdgePath(edgeProps)"
          :style="edgeProps.style"
        />
      </template>
    </VueFlow>

    <div v-else class="plot-canvas__empty">
      <a-empty description="暂无可展示的章节关系" />
    </div>

    <div
      v-if="flowNodes.length"
      class="plot-canvas__controls"
    >
      <a-tooltip title="放大">
        <a-button type="text" class="!rounded-none" @click="handleZoomIn">
          <Icon icon="mdi:plus" class="h-5 w-5" />
        </a-button>
      </a-tooltip>
      <a-tooltip title="缩小">
        <a-button type="text" class="!rounded-none" @click="handleZoomOut">
          <Icon icon="mdi:minus" class="h-5 w-5" />
        </a-button>
      </a-tooltip>
      <a-tooltip title="适配画布">
        <a-button type="text" class="!rounded-none" @click="handleFitView">
          <Icon icon="mdi:fit-to-page-outline" class="h-5 w-5" />
        </a-button>
      </a-tooltip>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import dagre from '@dagrejs/dagre';
import {
  BaseEdge,
  getSmoothStepPath,
  MarkerType,
  VueFlow,
  useVueFlow,
  type Edge,
  type EdgeProps,
  type Node,
} from '@vue-flow/core';
import Icon from '../CompatIcon.vue';
import '@vue-flow/core/dist/style.css';
import '@vue-flow/core/dist/theme-default.css';
import PlotChapterNode from './PlotChapterNode.vue';
import type {
  PlotCanvasNodeData,
  PlotCanvasOption,
  PlotChapterItem,
} from './plotCanvasTypes.js';

const props = defineProps<{
  chapters: PlotChapterItem[];
  selectedId: string;
}>();

const emit = defineEmits<{
  select: [id: string];
  tune: [item: PlotChapterItem];
}>();

/** 分支连线路由数据 */
interface PlotBranchEdgeData {
  routeCount: number;
  routeIndex: number;
}

const nodeWidth = 220; // 章节节点宽度
const nodeHeight = 228; // 章节节点基础高度
const flowNodes = ref<Node<PlotCanvasNodeData>[]>([]); // 画布节点
const flowEdges = ref<Edge[]>([]); // 画布连线
const { fitView, zoomIn, zoomOut } = useVueFlow(); // 画布视口控制

// 默认连线样式
const defaultEdgeOptions = computed(() => ({
  markerEnd: MarkerType.ArrowClosed,
  type: 'smoothstep',
}));

watch(
  () => props.chapters,
  () => {
    buildFlowElements();
  },
  { deep: true, immediate: true },
);

watch(
  () => props.selectedId,
  () => {
    syncSelectedNode();
  },
);

/**
 * 生成不聚合的分支连线路径
 * @param edge 连线坐标与路由数据
 */
function getBranchEdgePath(edge: EdgeProps<PlotBranchEdgeData>): string {
  const routeCount = Math.max(edge.data.routeCount, 1); // 当前父节点连线数量
  const routeSpacing = (edge.targetX - edge.sourceX) / (routeCount + 1); // 独立线路间距
  const centerX = edge.sourceX + routeSpacing * (edge.data.routeIndex + 1); // 当前线路转折位置
  const [path] = getSmoothStepPath({
    borderRadius: 8,
    centerX,
    offset: 16,
    sourcePosition: edge.sourcePosition,
    sourceX: edge.sourceX,
    sourceY: edge.sourceY,
    targetPosition: edge.targetPosition,
    targetX: edge.targetX,
    targetY: edge.targetY,
  });

  return path;
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
 * 获取章节标题
 * @param item 章节数据
 */
function getChapterTitle(item: PlotChapterItem): string {
  return item?.title || item?.name || '章节标题标题';
}

/**
 * 获取章节简介
 * @param item 章节数据
 */
function getChapterSummary(item: PlotChapterItem): string {
  return item?.summary || item?.description || '章节简介简介简介简介简介500字';
}

/**
 * 获取选项文案
 * @param option 选项数据
 */
function getOptionLabel(option: any): string {
  return option?.choice_text || '--';
}

/**
 * 获取选项目标ID
 * @param option 选项数据
 */
function getOptionTargetId(option: any): string {
  return String(
    option?.target_id ||
      option?.targetId ||
      option?.next_id ||
      option?.nextChapterId ||
      option?.chapter_id ||
      '',
  );
}

/**
 * 获取章节原始选项
 * @param item 章节数据
 */
function getRawOptions(item: PlotChapterItem): any[] {
  const rawOptions =
    item?.options ||
    item?.choices ||
    item?.branches ||
    item?.nodes?.flatMap((node) => node?.options || node?.choices || []) ||
    [];

  return Array.isArray(rawOptions) ? rawOptions : [];
}

/**
 * 解析章节选项
 * @param item 章节数据
 */
function getCanvasOptions(item: PlotChapterItem): PlotCanvasOption[] {
  const rawOptions = getRawOptions(item);

  return rawOptions
    .map((option) => ({
      label: getOptionLabel(option),
      targetId: getOptionTargetId(option),
    }))
    .filter((option) => option.targetId);
}

/**
 * 构建画布节点
 * @param chapterIds 章节ID列表
 */
function buildNodes(chapterIds: string[]): Node<PlotCanvasNodeData>[] {
  return props.chapters.map((item, index) => {
    const id = chapterIds[index] || getChapterId(item, index);
    const options = getCanvasOptions(item);

    return {
      id,
      data: {
        chapter: item,
        cover: item?.cover,
        id,
        index,
        onSelect: handleSelect,
        onTune: handleTune,
        options,
        selected: props.selectedId === id,
        summary: getChapterSummary(item),
        title: getChapterTitle(item),
      },
      position: { x: 0, y: 0 },
      type: 'chapter',
    };
  });
}

/**
 * 构建画布连线
 * @param nodes 画布节点
 */
function buildEdges(nodes: Node<PlotCanvasNodeData>[]): Edge[] {
  const nodeIdSet = new Set(nodes.map((node) => node.id));

  return nodes.flatMap((node) => {
    const nodeData = node.data as PlotCanvasNodeData;

    return nodeData.options
      .filter((option) => nodeIdSet.has(option.targetId))
      .map((option, index) => ({
        id: `${node.id}-${option.targetId}-${index}`,
        source: node.id,
        sourceHandle:
          nodeData.options.length === 1 ? 'chapter-source' : `option-${index}`,
        target: option.targetId,
        targetHandle: 'chapter-target',
        data: {
          routeCount: nodeData.options.length,
          routeIndex: index,
        },
        markerEnd: MarkerType.ArrowClosed,
        style: {
          stroke: '#7166f0',
          strokeWidth: 2.5,
        },
        type: 'branch',
      }));
  });
}

/**
 * 自动布局画布节点
 * @param nodes 画布节点
 * @param edges 画布连线
 */
function layoutNodes(
  nodes: Node<PlotCanvasNodeData>[],
  edges: Edge[],
): Node<PlotCanvasNodeData>[] {
  const graph = new dagre.graphlib.Graph();

  graph.setDefaultEdgeLabel(() => ({}));
  graph.setGraph({
    marginx: 32,
    marginy: 32,
    nodesep: 64,
    rankdir: 'LR',
    ranker: 'tight-tree',
    ranksep: 140,
  });

  nodes.forEach((node) => {
    const nodeData = node.data as PlotCanvasNodeData;
    const optionHeight = nodeData.options.length * 30;

    graph.setNode(node.id, {
      height: nodeHeight + optionHeight,
      width: nodeWidth,
    });
  });

  // Dagre 横向树会按连线注册顺序从下到上布局，反向注册以保持接口顺序从上到下展示。
  edges
    .slice()
    .reverse()
    .forEach((edge) => {
      graph.setEdge(edge.source, edge.target);
    });

  dagre.layout(graph);

  return nodes.map((node) => {
    const layoutNode = graph.node(node.id);
    const nodeData = node.data as PlotCanvasNodeData;
    const currentNodeHeight = nodeHeight + nodeData.options.length * 30; // 当前节点实际高度

    return {
      ...node,
      position: {
        x: layoutNode.x - nodeWidth / 2,
        y: layoutNode.y - currentNodeHeight / 2,
      },
    };
  });
}

/**
 * 构建画布元素
 */
async function buildFlowElements() {
  const chapterIds = props.chapters.map((item, index) =>
    getChapterId(item, index),
  );
  const nodes = buildNodes(chapterIds);
  const edges = buildEdges(nodes);

  flowNodes.value = layoutNodes(nodes, edges);
  flowEdges.value = edges;

  await nextTick();
  fitView({ duration: 240, padding: 0.2 });
}

/**
 * 同步节点选中状态
 */
function syncSelectedNode() {
  flowNodes.value = flowNodes.value.map((node) => {
    const nodeData = node.data as PlotCanvasNodeData;

    return {
      ...node,
      data: {
        ...nodeData,
        selected: props.selectedId === node.id,
      },
    };
  });
}

/**
 * 画布初始化后适配视口
 */
function handlePaneReady() {
  fitView({ duration: 240, padding: 0.2 });
}

/**
 * 放大画布
 */
function handleZoomIn() {
  zoomIn({ duration: 160 });
}

/**
 * 缩小画布
 */
function handleZoomOut() {
  zoomOut({ duration: 160 });
}

/**
 * 适配画布视口
 */
function handleFitView() {
  fitView({ duration: 240, padding: 0.2 });
}

/**
 * 选择章节节点
 * @param id 章节ID
 */
function handleSelect(id: string) {
  emit('select', id);
}

/**
 * 触发演出微调
 * @param item 章节数据
 */
function handleTune(item: PlotChapterItem) {
  emit('tune', item);
}

defineExpose({
  fitView: handleFitView,
});
</script>

<style scoped>
.plot-canvas {
  position: relative;
  min-height: 460px;
  flex: 1;
  overflow: hidden;
  border: 1px solid #343849;
  border-radius: 8px;
  background: #10121a;
}

.plot-canvas__empty {
  display: flex;
  min-height: 460px;
  align-items: center;
  justify-content: center;
}

.plot-canvas__controls {
  position: absolute;
  right: 16px;
  bottom: 16px;
  z-index: 10;
  display: flex;
  overflow: hidden;
  border: 1px solid #343849;
  border-radius: 6px;
  background: #202330f2;
  box-shadow: 0 4px 16px #00000040;
}

.plot-flow-canvas {
  --vf-handle: #7166f0;
  --vf-node-color: #e7eaf5;
  --vf-connection-path: #7166f0;

  height: 100%;
  min-height: 460px;
}

.plot-flow-canvas :deep(.vue-flow__edge-path) {
  stroke: #7166f0;
  stroke-width: 2.5;
}

.plot-flow-canvas :deep(.vue-flow__edge-text) {
  font-weight: 700;
  fill: #a99df8;
}

.plot-flow-canvas :deep(.vue-flow__edge-textbg) {
  fill: #10121ae0;
}

.plot-flow-canvas :deep(.vue-flow__handle) {
  pointer-events: none;
  opacity: 0;
}
</style>
