"""SQLAlchemy database models."""
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
from common.config import DATABASE_URL
from common.db_pool import worker_pool_kwargs

Base = declarative_base()
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_timeout=3,
    pool_recycle=1800,
    pool_use_lifo=True,
    pool_reset_on_return="rollback",
    connect_args={
        "connect_timeout": 5,
        "options": (
            "-c statement_timeout=30000 "
            "-c lock_timeout=5000 "
            "-c idle_in_transaction_session_timeout=60000 "
            "-c application_name=novel-game-business-models"
        ),
    },
    **worker_pool_kwargs(default_pool_size=5, default_max_overflow=5),
)
SessionLocal = sessionmaker(bind=engine)


class Novel(Base):
    """Novel metadata table."""
    __tablename__ = "novels"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    author = Column(String(255))
    file_path = Column(String(512), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    story_nodes = relationship("StoryNode", back_populates="novel")


class StoryNode(Base):
    """Story tree nodes - represents plot structure."""
    __tablename__ = "story_nodes"

    id = Column(Integer, primary_key=True, index=True)
    novel_id = Column(Integer, ForeignKey("novels.id"), nullable=False)
    node_type = Column(String(50))  # chapter, scene, dialogue, etc.
    content = Column(Text)
    sequence = Column(Integer)
    parent_id = Column(Integer, ForeignKey("story_nodes.id"), nullable=True)

    novel = relationship("Novel", back_populates="story_nodes")
    assets = relationship("Asset", back_populates="story_node")


class Asset(Base):
    """Asset table - stores generated audio, scene, character assets."""
    __tablename__ = "assets"

    id = Column(Integer, primary_key=True, index=True)
    story_node_id = Column(Integer, ForeignKey("story_nodes.id"), nullable=False)
    asset_type = Column(String(50))  # audio, scene, character
    file_path = Column(String(512), nullable=False)
    metadata = Column(Text)  # JSON string for additional info
    created_at = Column(DateTime, default=datetime.utcnow)

    story_node = relationship("StoryNode", back_populates="assets")


class GameBuild(Base):
    """Game build records."""
    __tablename__ = "game_builds"

    id = Column(Integer, primary_key=True, index=True)
    novel_id = Column(Integer, ForeignKey("novels.id"), nullable=False)
    build_path = Column(String(512), nullable=False)
    status = Column(String(50))  # pending, building, completed, failed
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


def init_db():
    """Initialize database tables."""
    Base.metadata.create_all(bind=engine)
