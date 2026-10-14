#!/usr/bin/env python3
"""
重置世界观表（删除并重新创建）
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import SQLModel
from models import engine, Worldview

# 删除世界观表
print("正在删除世界观表...")
SQLModel.metadata.tables[Worldview.__tablename__].drop(engine, checkfirst=True)

# 重新创建所有表（包括世界观表）
from models import init_db
init_db()

print("✅ 世界观表已重置")