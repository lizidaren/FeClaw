/**
 * feclaw-seccomp-enforcer.c
 *
 * FeClaw 沙箱 seccomp 强制执行器
 *
 * 固定路径为 /seccomp-enforcer（通过 bwrap --ro-bind 注入沙箱）。
 * 读取 /seccomp.bpf（原始 sock_filter 数组），安装 seccomp 白名单，
 * 然后 exec 目标程序。
 *
 * 确保所有沙箱内的代码（Python / bash / 任意二进制）都在 seccomp 白名单保护下。
 *
 * 执行顺序：
 *   1. open /seccomp.bpf → 读取 BPF 字节码
 *   2. prctl(NO_NEW_PRIVS)           — 锁死
 *   3. prctl(SECCOMP, 白名单 BPF)    — 装 seccomp
 *   4. execvp(argv[1], argv + 1)     — 运行目标程序
 *
 * 安全属性：
 * - 静态编译（gcc -static），无 .so 依赖，LD_PRELOAD 无效
 * - 如果 /seccomp.bpf 不存在/无效 → 拒绝运行
 * - /seccomp.bpf 由父进程（FeClaw 后端）写入 tmpfs 并通过 bwrap --ro-bind 注入，
 *   只读、TOCTOU 安全（bwrap 挂载时已经固定内容）
 *
 * 编译安装：
 *   sudo gcc -static -O2 -Wall -Wextra -o /usr/local/libexec/feclaw/seccomp-enforcer \
 *     deploy/seccomp-enforcer/seccomp-enforcer.c
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/prctl.h>
#include <linux/filter.h>
#include <linux/seccomp.h>

#define BPF_PATH "/seccomp.bpf"

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "Usage: seccomp-enforcer <command> [args...]\n");
        return 1;
    }

    /* ── 1. 读取 seccomp BPF ── */
    int fd = open(BPF_PATH, O_RDONLY);
    if (fd < 0) {
        fprintf(stderr, "[seccomp] open %s: %s\n", BPF_PATH, strerror(errno));
        return 1;
    }

    unsigned char buf[65536];
    ssize_t total = 0;
    while (total < (ssize_t)sizeof(buf)) {
        ssize_t n = read(fd, buf + total, sizeof(buf) - total);
        if (n < 0) {
            if (errno == EINTR) continue;
            fprintf(stderr, "[seccomp] read: %s\n", strerror(errno));
            close(fd);
            return 1;
        }
        if (n == 0) break;
        total += n;
    }
    close(fd);

    if (total < 8 || (total % sizeof(struct sock_filter)) != 0) {
        fprintf(stderr, "[seccomp] invalid BPF: %zd bytes\n", total);
        return 1;
    }

    /* ── 2. NO_NEW_PRIVS ── */
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) {
        perror("[seccomp] PR_SET_NO_NEW_PRIVS");
        return 1;
    }

    /* ── 3. 安装 seccomp 白名单 ── */
    struct sock_fprog prog;
    prog.len = total / sizeof(struct sock_filter);
    prog.filter = (struct sock_filter *)buf;

    if (prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &prog) != 0) {
        perror("[seccomp] PR_SET_SECCOMP");
        fprintf(stderr, "[seccomp] %zd bytes, %u instructions\n",
                total, prog.len);
        return 1;
    }

    /* ── 4. exec 目标程序 ── */
    execvp(argv[1], argv + 1);
    fprintf(stderr, "[seccomp] execvp %s: %s\n", argv[1], strerror(errno));
    return 1;
}
