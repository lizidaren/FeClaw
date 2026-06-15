#!/usr/bin/env python3
"""上传公共模板文件到 COS /public/feclaw/templates/"""

import sys, os, json
sys.path.insert(0, "/home/lch/Projects/FeClaw")
os.chdir("/home/lch/Projects/FeClaw")
from dotenv import load_dotenv
load_dotenv()

from services.storage_service import StorageService

TEMPLATES_DIR = "public/feclaw/templates"
COS_PREFIX = "feclaw/public/feclaw/templates"  # 公共路径（含 cos_prefix）

def upload_template(app_id: str):
    """上传一个 App 模板到 COS"""
    local_dir = f"{TEMPLATES_DIR}/{app_id}"
    if not os.path.isdir(local_dir):
        print(f"❌ 本地模板目录不存在: {local_dir}")
        return False
    
    s = StorageService()
    uploaded = 0
    
    for root, dirs, files in os.walk(local_dir):
        for fname in files:
            local_path = os.path.join(root, fname)
            rel_path = os.path.relpath(local_path, TEMPLATES_DIR)
            cos_key = f"{COS_PREFIX}/{rel_path}"
            
            with open(local_path, "rb") as f:
                content = f.read()
            
            s.put_object(cos_key, content)
            print(f"  ✅ {rel_path} → COS:{cos_key} ({len(content)} bytes)")
            uploaded += 1
    
    print(f"\n📦 {app_id}: {uploaded} files uploaded to COS")
    return True


if __name__ == "__main__":
    app_id = sys.argv[1] if len(sys.argv) > 1 else "vocab-app"
    upload_template(app_id)
