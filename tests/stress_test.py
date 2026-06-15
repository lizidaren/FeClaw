#!/usr/bin/env python3
"""
FUSE 性能压测
对比 FUSE 直通（COS 直操作） vs VFS 仿真（mock/unlock）的延迟
"""
import os, sys, time, threading, subprocess, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.virtual_filesystem import VirtualFileSystem
from services.vfs_fuse_daemon import start_fuse_background, unmount_fuse

MOUNT = "/tmp/feclaw-perf-test"
USER = "perf-test"
vfs = VirtualFileSystem(user_id=USER)
SAMPLE_SIZE = 100  # 每种测试跑多少遍

results = {}

def fmt_ms(sec):
    return f"{sec*1000:.2f}ms"

def run_fuse():
    """挂载 FUSE"""
    subprocess.run(["fusermount3", "-u", MOUNT], capture_output=True)
    subprocess.run(["fusermount3", "-uz", MOUNT], capture_output=True)
    os.makedirs(MOUNT, exist_ok=True)
    import trio
    from services.vfs_fuse_daemon import VFSFuseDaemon
    async def _main():
        ops = VFSFuseDaemon(vfs, MOUNT, 60, cos_prefix="feclaw/")
        pyfuse3_init = __import__("pyfuse3").init
        pyfuse3_init(ops, MOUNT, set())
        await __import__("pyfuse3").main()
    trio.run(_main)

t = threading.Thread(target=run_fuse, daemon=True)
t.start()
time.sleep(3)

FUSE_DIR = f"{MOUNT}/user_workspaces/{USER}/workspace"

# ========== 1. 空文件创建 ==========
print("\n📝 1. 空文件创建")
for label, fn in [
    ("VFS touch()",    lambda: vfs.touch("/workspace/perf_vfs_t.txt")),
    ("FUSE open(w)",   lambda: open(f"{FUSE_DIR}/perf_fuse_t.txt", "w").close()),
]:
    times = []
    for i in range(SAMPLE_SIZE):
        # 清理
        try: vfs.rm(f"/workspace/perf_{label[:3].lower()}_{i}.txt", force=True)
        except: pass
        path = f"/workspace/perf_{label[:3].lower()}_{i}.txt"
        fpath = f"{FUSE_DIR}/perf_{label[:3].lower()}_{i}.txt"
        
        start = time.perf_counter()
        if "touch" in label:
            vfs.touch(path)
        else:
            open(fpath, "w").close()
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    
    avg = sum(times) / len(times)
    results[f"创建 {label}"] = fmt_ms(avg)
    print(f"  {label:25s}  avg={fmt_ms(avg):>8s}  min={fmt_ms(min(times)):>8s}  max={fmt_ms(max(times)):>8s}")

# ========== 2. 写小文件（1KB） ==========
print("\n✏️ 2. 写入 1KB 文件")
data_1k = "x" * 1024
for label, writer in [
    ("VFS echo()", lambda p: vfs.echo(data_1k, p)),
    ("FUSE write", lambda p: open(p, "w").write(data_1k)),
]:
    times = []
    for i in range(SAMPLE_SIZE):
        p = f"/workspace/perf_w_{i}.txt" if "VFS" in label else f"{FUSE_DIR}/perf_w_{i}.txt"
        start = time.perf_counter()
        writer(p)
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    
    avg = sum(times) / len(times)
    results[f"写1K {label.split()[0]}"] = fmt_ms(avg)
    print(f"  {label:25s}  avg={fmt_ms(avg):>8s}  min={fmt_ms(min(times)):>8s}  max={fmt_ms(max(times)):>8s}")

# ========== 3. 读小文件（1KB） ==========
print("\n📖 3. 读取 1KB 文件")
for label, reader in [
    ("VFS read_file()", lambda p: vfs.read_file(p)),
    ("FUSE read", lambda p: open(p, "rb").read()),
]:
    times = []
    for i in range(min(SAMPLE_SIZE, 20)):  # 20 次够
        p = f"/workspace/perf_w_{i}.txt" if "VFS" in label else f"{FUSE_DIR}/perf_w_{i}.txt"
        start = time.perf_counter()
        reader(p)
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    
    avg = sum(times) / len(times)
    results[f"读1K {label.split()[0]}"] = fmt_ms(avg)
    print(f"  {label:25s}  avg={fmt_ms(avg):>8s}  min={fmt_ms(min(times)):>8s}  max={fmt_ms(max(times)):>8s}")

# ========== 4. 写大文件（1MB） ==========
print("\n💾 4. 写入 1MB 文件")
data_1m = "y" * (1024*1024)
for label, writer in [
    ("VFS echo()", lambda p: vfs.echo(data_1m, p)),
    ("FUSE write", lambda p: open(p, "w").write(data_1m)),
]:
    times = []
    for i in range(5):  # 大文件少跑几次
        p = f"/workspace/perf_big_{i}.txt" if "VFS" in label else f"{FUSE_DIR}/perf_big_{i}.txt"
        start = time.perf_counter()
        writer(p)
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    
    avg = sum(times) / len(times)
    results[f"写1M {label.split()[0]}"] = fmt_ms(avg)
    mbps = f"{1/avg:.1f}MB/s"
    print(f"  {label:25s}  avg={fmt_ms(avg):>8s}  ≈ {mbps}")

# ========== 5. 读大文件（1MB） ==========
print("\n💾 5. 读取 1MB 文件")
for label, reader in [
    ("VFS read_file()", lambda p: vfs.read_file(p)),
    ("FUSE read", lambda p: open(p, "rb").read()),
]:
    times = []
    for i in range(5):
        p = f"/workspace/perf_big_{i}.txt" if "VFS" in label else f"{FUSE_DIR}/perf_big_{i}.txt"
        start = time.perf_counter()
        reader(p)
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    
    avg = sum(times) / len(times)
    results[f"读1M {label.split()[0]}"] = fmt_ms(avg)
    mbps = f"{1/avg:.1f}MB/s"
    print(f"  {label:25s}  avg={fmt_ms(avg):>8s}  ≈ {mbps}")

# ========== 6. ls 延迟 ==========
print("\n📂 6. 目录列表")
for label, lister in [
    ("VFS ls()", lambda: vfs.ls("/workspace")),
    ("FUSE ls",  lambda: subprocess.run(["ls", FUSE_DIR], capture_output=True)),
]:
    times = []
    for i in range(20):
        start = time.perf_counter()
        lister()
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    
    avg = sum(times) / len(times)
    results[f"ls {label.split()[0]}"] = fmt_ms(avg)
    print(f"  {label:25s}  avg={fmt_ms(avg):>8s}  min={fmt_ms(min(times)):>8s}  max={fmt_ms(max(times)):>8s}")

# ========== 7. 并发写入（模拟竞态） ==========
print("\n⚡ 7. 并发写入（10 线程同时写同一文件）")
for label, writer in [
    ("VFS echo()", lambda p, d: vfs.echo(d, p)),
    ("FUSE write", lambda p, d: open(p, "w").write(d)),
]:
    path = f"/workspace/perf_concurrent.txt" if "VFS" in label else f"{FUSE_DIR}/perf_concurrent.txt"
    
    def worker(thread_id, path=path, label=label):
        for _ in range(5):
            writer(path, f"thread-{thread_id}-data\n")
    
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    start = time.perf_counter()
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed = time.perf_counter() - start
    
    results[f"并发10线程 {label.split()[0]}"] = fmt_ms(elapsed)
    print(f"  {label:25s}  10线程×5次 = {fmt_ms(elapsed)}")

# ========== 报告 ==========
print("\n" + "="*60)
print("📊 FUSE vs VFS 性能报告")
print("="*60)
print(f"{'测试项':30s} {'VFS':>14s} {'FUSE':>14s}")
print("-"*60)
pairs = ["创建", "写1K", "读1K", "写1M", "读1M", "ls", "并发10线程"]
for p in pairs:
    v = results.get(p + " VFS", "")
    f = results.get(p + " FUSE", "")
    if not f:
        vv = results.get(p + " VFS touch()", "")
        ff = results.get(p + " FUSE open(w)", "")
        if vv and ff:
            print(f"{p:30s} {vv:>14s} {ff:>14s}")
    else:
        print(f"{p:30s} {v:>14s} {f:>14s}")

# 清理
print("\n🧹 Cleaning up...")
for pfx in ["perf_vfs_t", "perf_fuse_t", "perf_w_", "perf_big_"]:
    for i in range(100):
        try: vfs.rm(f"/workspace/{pfx}{i}.txt", force=True)
        except: pass
try: vfs.rm("/workspace/perf_concurrent.txt", force=True)
except: pass
unmount_fuse(MOUNT)
print("Done!")
