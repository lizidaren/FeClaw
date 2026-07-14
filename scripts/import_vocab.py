"""导入高考 3500 词汇到 MySQL"""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from models.database import SessionLocal, VocabularyWord, engine, Base

def import_vocab(json_path: str):
    # 确保表存在（checkfirst 避免重复创建）
    VocabularyWord.__table__.create(bind=engine, checkfirst=True)

    with open(json_path, encoding="utf-8") as f:
        words = json.load(f)

    db = SessionLocal()
    try:
        count = 0
        for item in words:
            existing = db.query(VocabularyWord).filter(
                VocabularyWord.word == item["word"]
            ).first()
            if not existing:
                w = VocabularyWord(
                    word=item["word"],
                    pronunciation=item.get("pronunciation", ""),
                    part_of_speech=item.get("part_of_speech", ""),
                    meaning=item.get("meaning", ""),
                    tags=",".join(item.get("tags", ["gaokao-3500"])),
                )
                db.add(w)
                count += 1
        db.commit()
        print(f"导入完成: {count} 条新记录")
        print(f"数据库总计: {db.query(VocabularyWord).count()} 条")
    finally:
        db.close()

if __name__ == "__main__":
    import_vocab(sys.argv[1])
