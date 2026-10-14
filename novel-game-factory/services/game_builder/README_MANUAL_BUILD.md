# 手动游戏构建工具

## 简介

`manual_build.py` 是一个独立的游戏构建脚本，不依赖Celery任务系统，可以直接运行来构建游戏包。

## 使用方法

### 基本用法

```bash
python manual_build.py <task_id>
```

### 指定域名

```bash
python manual_build.py <task_id> <domain>
```

### 强制构建（跳过确认）

```bash
python manual_build.py <task_id> <domain> --force
```

## 参数说明

- `task_id`: 游戏项目ID（必需）
  - 例如: `douluodalu_c55d8e88`

- `domain`: 域名配置（可选，默认为 `localhost`）
  - 例如: `example.com`

- `--force`: 强制构建标志（可选）
  - 跳过缺少目录的确认提示
  - 适用于自动化脚本

## 使用示例

### 示例 1: 基本构建

```bash
python manual_build.py douluodalu_c55d8e88
```

### 示例 2: 指定域名

```bash
python manual_build.py douluodalu_c55d8e88 game.example.com
```

### 示例 3: 强制构建

```bash
python manual_build.py douluodalu_c55d8e88 localhost --force
```

### 示例 4: 在容器中运行

```bash
docker exec novel-game-factory-game_builder-1 \
  python /app/services/game_builder/manual_build.py douluodalu_c55d8e88
```

### 示例 5: 本地运行（指定STORAGE_PATH）

```bash
STORAGE_PATH=./storage python services/game_builder/manual_build.py douluodalu_c55d8e88 --force
```

## 环境变量

- `STORAGE_PATH`: 存储路径（默认: `/storage`）
  - 源目录: `$STORAGE_PATH/outputs/<task_id>`
  - 输出目录: `$STORAGE_PATH/built_games/<task_id>`

## 前置条件

构建前需要确保以下资源已生成：

1. **必需目录**:
   - `images/` - 图片资源（角色、场景、背景）
   - `audios/` - 音频资源（对话音频）

2. **必需文件**:
   - `story_tree.json` - 故事树结构

## 输出结构

构建成功后，输出目录结构如下：

```
built_games/<task_id>/
├── chapter_1_<node_id>/     # 第1章节点
│   ├── index.html
│   ├── game.js
│   ├── images/
│   └── audios/
├── chapter_2_<node_id>/     # 第2章节点
│   └── ...
├── ...
├── nginx.conf               # Nginx配置文件
├── story_tree.json          # 故事树副本
└── index.html               # 游戏首页
```

## 错误处理

### 错误: 源目录不存在

```
❌ 错误: 源目录不存在
   路径: /storage/outputs/<task_id>
```

**解决方法**:
1. 检查task_id是否正确
2. 确认该任务的资源已经生成完成
3. 检查STORAGE_PATH环境变量配置

### 警告: 缺少目录

```
⚠️  警告: 缺少以下目录: audios
   构建可能会失败或不完整
```

**解决方法**:
1. 使用 `--force` 参数强制构建
2. 或者等待资源生成完成后再构建

## 与Celery任务的区别

| 特性 | manual_build.py | tasks.py (Celery) |
|------|----------------|-------------------|
| 运行方式 | 直接运行 | Celery任务队列 |
| 依赖 | 无需Celery | 需要Celery和Redis |
| 适用场景 | 手动测试、调试 | 自动化流水线 |
| 错误处理 | 直接输出 | 任务失败重试 |
| 交互性 | 支持交互确认 | 无交互 |

## 注意事项

1. **路径配置**: 确保STORAGE_PATH环境变量正确配置
2. **资源完整性**: 构建前检查所有必需资源是否已生成
3. **权限问题**: 确保有写入输出目录的权限
4. **模板目录**: 确保game_template/shijuexiaoshuo目录存在

## 调试技巧

### 查看详细错误

脚本会自动打印详细的错误堆栈信息。

### 检查源目录内容

脚本会自动检查并显示源目录中的内容：

```
🔍 检查源目录内容...
   ✅ images/ 存在
   ⚠️  audios/ 不存在
```

### 查看构建产物

构建成功后会显示生成的节点数量和文件列表。

## 常见问题

### Q: 为什么需要这个脚本？

A: 用于手动测试和调试游戏构建过程，不依赖Celery任务系统。

### Q: 可以在容器外运行吗？

A: 可以，但需要正确配置STORAGE_PATH环境变量。

## 清理共享 PVC 上的旧产物

`game-builder` 镜像内置了安全清理工具。它运行在容器内，默认挂载根目录为
`/storage`，默认只预览 `outputs` 和 `built_games` 下超过 7 天未更新的直属条目，
不会删除任何内容。工具会保护 Celery 正在执行或已排队的 game-builder 任务。

```bash
# 先预览（推荐）
python -m services.game_builder.cleanup_storage --days 7

# 确认列表无误后执行删除
python -m services.game_builder.cleanup_storage --days 7 --apply

# 只清理 outputs，或只处理某一个 task
python -m services.game_builder.cleanup_storage --root outputs --days 3 --apply
python -m services.game_builder.cleanup_storage --task-id <task_id> --days 3 --apply
```

只删除直属条目；目录的年龄按其内部最新文件的修改时间计算。发现无法读取的条目、
符号链接或活动任务时会跳过并输出警告。除非确认没有活动任务，不要使用
`--skip-active-check`。

### Q: 构建失败怎么办？

A: 检查错误信息，确认资源完整性，查看详细错误堆栈。

### Q: 如何在自动化脚本中使用？

A: 使用 `--force` 参数跳过交互确认：

```bash
python manual_build.py <task_id> localhost --force
```

## 更新日志

- 2026-01-26: 初始版本
  - 支持基本构建功能
  - 支持域名配置
  - 支持强制模式
  - 支持资源检查
