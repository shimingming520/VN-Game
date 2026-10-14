export interface PlotChapterItem {
  chapterLabel?: string;
  cover?: string;
  description?: string;
  id?: string | number;
  link?: string;
  name?: string;
  nodes?: any[];
  options?: any[];
  summary?: string;
  title?: string;
  [key: string]: any;
}

export interface PlotCanvasOption {
  label: string;
  targetId: string;
}

export interface PlotCanvasNodeData {
  chapter: PlotChapterItem;
  cover?: string;
  id: string;
  index: number;
  onSelect: (id: string) => void;
  onTune: (item: PlotChapterItem) => void;
  options: PlotCanvasOption[];
  selected: boolean;
  summary: string;
  title: string;
}
