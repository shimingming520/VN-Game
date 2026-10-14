# Story Tree 数据库保存问题修复说明

## 问题描述

在保存 story_tree 数据到数据库时，可能会遇到以下错误：

```
sqlalchemy.exc.IntegrityError: (psycopg2.errors.UniqueViolation)
duplicate key value violates unique constraint "story_tree_json_pkey"
DETAIL: Key (id)=(3) already exists.
```

## 问题原因

PostgreSQL 使用序列（sequence）来生成自增主键ID。当序列值小于或等于表中已存在的最大ID时，就会发生主键冲突。

**常见原因：**
1. 手动插入数据时指定了ID，但没有更新序列
2. 数据库恢复/迁移后序列没有正确同步
3. 使用 `COPY` 或 `INSERT INTO ... VALUES` 直接插入数据

## 解决方案

### 方案1：自动修复（已实现）

代码已修改，在每次保存前自动检查并修复序列：

```python
# story_tree_to_db.py 中的修复逻辑
session.exec(text("SELECT setval('story_tree_json_id_seq', (SELECT COALESCE(MAX(id), 0) FROM story_tree_json))"))
```

**影响的函数：**
- `save_story_json_to_db()` - 保存JSON数据
- `save_story_md_to_db()` - 保存MD数据

### 方案2：手动修复工具

如果需要手动检查和修复所有表的序列，可以运行：

```bash
cd services/story_planner/table
python fix_sequences.py
```

### 方案3：SQL手动修复

直接在数据库中执行：

```sql
-- 修复 story_tree_json 表
SELECT setval('story_tree_json_id_seq', (SELECT MAX(id) FROM story_tree_json));

-- 修复 story_tree_md 表
SELECT setval('story_tree_md_id_seq', (SELECT MAX(id) FROM story_tree_md));

-- 修复 world_bible 表
SELECT setval('world_bible_id_seq', (SELECT MAX(id) FROM world_bible));

-- 修复 outlines 表
SELECT setval('outlines_id_seq', (SELECT MAX(id) FROM outlines));
```

## 验证修复

运行以下命令检查序列状态：

```bash
docker exec novel-game-factory-story_planner-1 python -c "
from sqlmodel import Session, text
from services.story_planner.table.models import engine

with Session(engine) as session:
    tables = [
        ('story_tree_json', 'story_tree_json_id_seq'),
        ('story_tree_md', 'story_tree_md_id_seq'),
    ]

    for table_name, seq_name in tables:
        max_id = session.exec(text(f'SELECT MAX(id) FROM {table_name}')).first()[0] or 0
        seq_val = session.exec(text(f'SELECT last_value FROM {seq_name}')).first()[0]
        status = '✅' if seq_val >= max_id else '❌'
        print(f'{status} {table_name}: MAX(id)={max_id}, seq={seq_val}')
"
```

## 预防措施

1. **避免手动指定ID**：让数据库自动生成ID
   ```python
   # ❌ 错误
   record = StoryTreeJson(id=123, task_id="xxx", data={})

   # ✅ 正确
   record = StoryTreeJson(task_id="xxx", data={})
   ```

2. **使用ORM操作**：优先使用 SQLModel/SQLAlchemy 而不是原生SQL

3. **数据迁移后检查**：每次数据库恢复或迁移后，运行 `fix_sequences.py`

## 相关文件

- `services/story_planner/table/story_tree_to_db.py` - 保存逻辑（已修复）
- `services/story_planner/table/fix_sequences.py` - 手动修复工具
- `services/story_planner/table/models.py` - 数据库模型定义

## 测试

修复后的代码已通过测试：

```bash
docker exec novel-game-factory-story_planner-1 python -c "
from services.story_planner.table.story_tree_to_db import save_story_json_to_db
result = save_story_json_to_db('test_task', {'test': 'data'})
print('✅ 测试通过' if result else '❌ 测试失败')
"
```

## 总结

✅ **问题已修复**：代码已自动处理序列同步问题
✅ **工具已提供**：可手动运行 `fix_sequences.py` 检查和修复
✅ **文档已完善**：包含问题原因、解决方案和预防措施

**以后不会再出现这个问题！**
