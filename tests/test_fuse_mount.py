#!/usr/bin/env python3
"""
独立 FUSE 挂载测试 - 不依赖 FeClaw 主服务
"""
import os
import sys
import time

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.vfs_fuse_daemon import start_fuse_background, unmount_fuse
from services.virtual_filesystem import VirtualFileSystem

# 使用用户目录下的路径（避免 /mnt 权限问题）
FUSE_USER = "fuse-test"
TMP_DIR = "/tmp/feclaw-fuse-test"
MOUNT_DIR = f"{TMP_DIR}/mnt"
# 新 FUSE 结构：根显示 agents/user_workspaces/public
TEST_DIR = f"{MOUNT_DIR}/user_workspaces/{FUSE_USER}/workspace"


def main():
    print("FUSE test starting...")

    # 使用明确的 user_id，避免 COS 路径拼接出错
    vfs = VirtualFileSystem(user_id=FUSE_USER)

    # 清理旧的挂载（可能残留 /dev/fuse 占用）
    import subprocess
    subprocess.run(["fusermount3", "-u", MOUNT_DIR], capture_output=True)
    subprocess.run(["fusermount3", "-uz", MOUNT_DIR], capture_output=True)  # lazy unmount

    os.makedirs(TMP_DIR, exist_ok=True)
    os.makedirs(MOUNT_DIR, exist_ok=True)
    thread = start_fuse_background(vfs, MOUNT_DIR, 60)
    time.sleep(2)  # 等待挂载完成

    print(f"FUSE mount at {MOUNT_DIR}")

    # 先用 VFS API 创建一些测试文件
    vfs.echo("Hello from FUSE!", "/workspace/fuse-test.txt")

    # 测试 1: ls
    print("\nTest 1: ls")
    os.system(f"ls {MOUNT_DIR} 2>&1")

    # 测试 2: 列出 workspace
    print("\nTest 2: ls workspace")
    os.system(f"ls -la {TEST_DIR} 2>&1")

    # 测试 3: cat
    print("\nTest 3: cat file")
    os.system(f"cat {TEST_DIR}/fuse-test.txt 2>&1")

    # 测试 4: mkdir + rmdir
    print("\nTest 4: mkdir + rmdir")
    try:
        os.mkdir(f"{TEST_DIR}/fusetestdir")
        print(f"✅ mkdir: {TEST_DIR}/fusetestdir")
        os.rmdir(f"{TEST_DIR}/fusetestdir")
        print(f"✅ rmdir: {TEST_DIR}/fusetestdir")
    except Exception as e:
        print(f"❌ mkdir/rmdir failed: {e}")

    # 测试 5: rm
    print("\nTest 5: rm file")
    try:
        os.remove(f"{TEST_DIR}/fuse-test.txt")
        print(f"✅ rm: fuse-test.txt")
    except Exception as e:
        print(f"❌ rm failed: {e}")

    # 测试 6: 从 FUSE 端创建文件
    print("\nTest 6: create file via FUSE")
    try:
        with open(f"{TEST_DIR}/fuse-new.txt", "w") as f:
            f.write("Created via FUSE!")
        with open(f"{TEST_DIR}/fuse-new.txt", "r") as f:
            content = f.read()
        print(f"✅ Created and read back: {content}")
        os.remove(f"{TEST_DIR}/fuse-new.txt")
    except Exception as e:
        print(f"❌ create/read via FUSE failed: {e}")

    # 清理
    print(f"\nUnmounting {MOUNT_DIR}...")
    unmount_fuse(MOUNT_DIR)
    print("Done!")


if __name__ == "__main__":
    main()
