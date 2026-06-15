#!/usr/bin/env python3
"""
考向库 ORM 模型
独立数据库 data/kaoxiang.db，支持后续多学科扩展。
"""

import os
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, JSON
from sqlalchemy.orm import sessionmaker, declarative_base
from datetime import datetime

KaoXiangBase = declarative_base()

# 数据库路径（相对于项目根目录）
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'kaoxiang.db')

engine = create_engine(
    f'sqlite:///{DB_PATH}',
    connect_args={'check_same_thread': False},
    echo=False
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_kaoxiang_session():
    return SessionLocal()


def init_kaoxiang_db():
    """创建表"""
    KaoXiangBase.metadata.create_all(bind=engine)


class KaoxiangKaodian(KaoXiangBase):
    """考向库 - 考点级考频数据（结构化）
    
    subject 字段支持多学科扩展："math", "physics", "chemistry" 等
    """
    __tablename__ = 'kaoxiang_kaodian'

    id = Column(Integer, primary_key=True, autoincrement=True)
    subject = Column(String(20), nullable=False, index=True, default='math')
    topic_full = Column(String(200), index=True)           # "专题一 集合与常用逻辑用语"
    section = Column(String(50))                            # "1.1"
    kaodian = Column(String(200), nullable=False, index=True)  # "集合的基本运算"
    exam_examples = Column(Text, default='[]')             # JSON: ["真题1", "真题2"]
    exam_trend = Column(String(500))                       # "集合的交集/并集/综合运算"
    exam_frequency = Column(String(20))                    # "8考"
    core_competency = Column(String(200))                  # "数学运算"
    source_name = Column(String(200))                      # 来源书籍全名
    source_file = Column(String(200))                      # 来源文件名
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'subject': self.subject,
            'topic_full': self.topic_full,
            'section': self.section,
            'kaodian': self.kaodian,
            'exam_examples': self.exam_examples,
            'exam_trend': self.exam_trend,
            'exam_frequency': self.exam_frequency,
            'core_competency': self.core_competency,
            'source_name': self.source_name,
        }
