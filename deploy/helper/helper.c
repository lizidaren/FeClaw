/**
 * feclaw-netns-helper.c
 *
 * FeClaw 沙箱网络命名空间辅助工具
 *
 * 单一职责：以最小的 root 权限窗口进入 feclaw-sandbox 网络命名空间，
 * 应用资源限制，丢弃所有特权，然后 exec bwrap。
 *
 * seccomp 白名单由 bwrap 沙箱内的 Python bootstrap 代码安装
 *（因为 bwrap 需要 unshare(CLONE_NEWUSER) 建立 user namespace，
 *  而 NO_NEW_PRIVS 会阻止该操作，所以 seccomp 必须在 bwrap
 *  完成沙箱搭建后再注入）。
 *
 * 执行顺序：
 *   1. setns(CLONE_NEWNET)          — 进入共享 netns
 *   2. apply_rlimits()              — 资源限制（读 env，min(env, 硬上限)）
 *   3. safe_clearenv()              — 清除非必要环境变量（保留 PATH）
 *   4. setgroups/setgid/setuid      — 放弃所有特权
 *   5. execvp("bwrap", ...)         — 以 lch 运行
 *
 * 安全属性：
 * - 静态编译（gcc -static），无 .so 依赖，LD_PRELOAD 无效
 * - setns() 需要 CAP_SYS_ADMIN（setuid 提供）
 * - 硬上限编译在二进制中，env 设置无法突破
 *
 * 编译安装：
 *   sudo gcc -static -O2 -Wall -Wextra -o /usr/local/libexec/feclaw/helper deploy/helper/helper.c
 *   sudo chown root:root /usr/local/libexec/feclaw/helper
 *   sudo chmod u+s /usr/local/libexec/feclaw/helper
 *
 * 注意：gcc -o 覆盖旧文件会丢失 setuid 位，每次重编后必须重新 chmod u+s
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <sched.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <grp.h>

/* ====================================================================
 * 编译时硬上限常量 — 不可修改（需重新编译）
 * ==================================================================== */
#define HARD_LIMIT_MEM_MB     512   /* 最大内存 512MB */
#define HARD_LIMIT_CPU_SEC    180   /* 最大 CPU 180s */
#define HARD_LIMIT_NPROC      2048  /* 最大进程数（整个 uid） */
#define HARD_LIMIT_NOFILE     256   /* 最大文件描述符 */

/* 环境变量默认值 */
#define DEFAULT_MEM_MB        128
#define DEFAULT_CPU_SEC       30
#define DEFAULT_NPROC         1024
#define DEFAULT_NOFILE        128

/* 网络命名空间路径 */
#define NETNS_PATH "/var/run/netns/feclaw-sandbox"

/* ====================================================================
 * 辅助函数
 * ==================================================================== */

static unsigned long get_env_ulong(const char *name, unsigned long default_val) {
    const char *val = getenv(name);
    if (!val) return default_val;
    char *end = NULL;
    errno = 0;
    unsigned long result = strtoul(val, &end, 10);
    if (end == val || *end != '\0') return default_val;
    if (result == ULONG_MAX && errno == ERANGE) return default_val;
    return result;
}

static unsigned long min_ul(unsigned long a, unsigned long b) {
    return a < b ? a : b;
}

static void apply_rlimit(int resource, unsigned long soft, unsigned long hard) {
    struct rlimit rl;
    rl.rlim_cur = (rlim_t)soft;
    rl.rlim_max = (rlim_t)hard;
    if (setrlimit(resource, &rl) != 0) {
        fprintf(stderr, "[helper] setrlimit(%d) failed: %s\n", resource, strerror(errno));
    }
}

static void safe_clearenv(void) {
    char *path_save = strdup(getenv("PATH") ? getenv("PATH") : "/usr/bin:/bin");
    if (!path_save) {
        fprintf(stderr, "[helper] clearenv strdup failed\n");
        return;
    }
    clearenv();
    setenv("PATH", path_save, 1);
    free(path_save);
}

/* ====================================================================
 * 主入口
 * ==================================================================== */
int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s <command> [args...]\n", argv[0]);
        return 1;
    }

    /* ── 1. 进入 feclaw-sandbox 网络命名空间 ── */
    int netns_fd = open(NETNS_PATH, O_RDONLY);
    if (netns_fd < 0) {
        perror("[helper] open netns");
        return 1;
    }
    if (setns(netns_fd, CLONE_NEWNET) != 0) {
        perror("[helper] setns");
        close(netns_fd);
        return 1;
    }
    close(netns_fd);

    /* ── 2. 应用资源限制 ── */
    unsigned long env_mem   = get_env_ulong("FECLAW_RLIMIT_MEM_MB",   DEFAULT_MEM_MB);
    unsigned long env_cpu   = get_env_ulong("FECLAW_RLIMIT_CPU_SEC",  DEFAULT_CPU_SEC);
    unsigned long env_proc  = get_env_ulong("FECLAW_RLIMIT_NPROC",    DEFAULT_NPROC);
    unsigned long env_nofile= get_env_ulong("FECLAW_RLIMIT_NOFILE",   DEFAULT_NOFILE);

    unsigned long mem_mb    = min_ul(env_mem,    HARD_LIMIT_MEM_MB);
    unsigned long cpu_sec   = min_ul(env_cpu,    HARD_LIMIT_CPU_SEC);
    unsigned long nproc     = min_ul(env_proc,   HARD_LIMIT_NPROC);
    unsigned long nofile    = min_ul(env_nofile,  HARD_LIMIT_NOFILE);

    apply_rlimit(RLIMIT_AS,     mem_mb * 1024UL * 1024UL, mem_mb * 1024UL * 1024UL);
    apply_rlimit(RLIMIT_CPU,    cpu_sec, cpu_sec);
    apply_rlimit(RLIMIT_NPROC,  nproc,   HARD_LIMIT_NPROC);
    apply_rlimit(RLIMIT_NOFILE, nofile,  HARD_LIMIT_NOFILE);

    /* ── 3. 清理环境变量（保留 PATH）─── */
    safe_clearenv();

    /* ── 4. 放弃所有特权 ── */
    if (setgroups(0, NULL) != 0) {
        perror("[helper] setgroups");
        return 1;
    }
    if (setgid(getgid()) != 0) {
        perror("[helper] setgid");
        return 1;
    }
    if (setuid(getuid()) != 0) {
        perror("[helper] setuid");
        return 1;
    }

    /* ── 5. exec bwrap ── */
    execvp("bwrap", argv + 1);
    perror("[helper] execvp bwrap");
    return 1;
}
