# FeClaw 沙箱网络隔离 — 部署配置

> 设计文档: `docs/sandbox_network_isolation.md`
> 服务文件: `deploy/systemd/`
> 权限文件: `deploy/sudoers/`

## 前置条件

- Linux 内核支持 network namespaces (`CONFIG_NET_NS=y`)
- `iptables` 已安装 — `sudo apt install iptables`
- `bwrap` (bubblewrap) 已安装 — `sudo apt install bubblewrap`
- WSL2: 支持 netns（WSL1 不支持，将自动回退到 `--share-net`）

## 快速部署

```bash
# 从 repo 复制到系统目录
sudo mkdir -p /usr/local/libexec/feclaw
sudo cp scripts/init_sandbox_netns.sh /usr/local/libexec/feclaw/
sudo cp scripts/cleanup_sandbox_netns.sh /usr/local/libexec/feclaw/
sudo chmod +x /usr/local/libexec/feclaw/*.sh

# 安装 systemd oneshot 服务
sudo cp deploy/systemd/feclaw-netns.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable feclaw-netns.service
sudo systemctl start feclaw-netns.service
sudo systemctl status feclaw-netns.service

# 安装 sudoers 权限（后端运行时使用）
sudo cp deploy/sudoers/feclaw-sandbox /etc/sudoers.d/feclaw-sandbox
sudo chmod 440 /etc/sudoers.d/feclaw-sandbox
sudo visudo -c -f /etc/sudoers.d/feclaw-sandbox
```

## 组件说明

### systemd oneshot: `feclaw-netns.service`

在系统启动时（以 root）执行一次 `init_sandbox_netns.sh`：
- 创建 `feclaw-sandbox` network namespace
- 创建 veth pair `fl-host` ↔ `fl-sbx`
- 配置 iptables 规则（自定义链 `FECLAW-SBX-FWD`）
- 配置 DNS（`/etc/netns/feclaw-sandbox/resolv.conf`）
- 禁用 IPv6
- `RemainAfterExit=yes`：执行完毕后保持"active"状态

### sudoers: `/etc/sudoers.d/feclaw-sandbox`

后端（用户 `lch`）通过 `sudo -n` 执行特权操作，精确放行：

```
# 仅放行沙箱执行
lch ALL=(root) NOPASSWD: /usr/bin/ip netns exec feclaw-sandbox /usr/bin/bwrap *
# 仅放行状态检查
lch ALL=(root) NOPASSWD: /usr/bin/ip netns list
```

### 后端集成

`SandboxManager._build_bwrap_command()` 会：

```python
if NetworkIsolationManager.check():
    # 使用 netns: sudo -n ip netns exec feclaw-sandbox bwrap ...
    cmd = get_netns_prefix() + bwrap_opts + [python_bin, script]
else:
    # 回退: 无 sudo，bwrap 默认 --share-net
    cmd = bwrap_opts + [python_bin, script]
```

## 验证

```bash
# 运行完整测试套件
sudo bash scripts/test_sandbox_netns.sh

# 或手动验证关键项：

# 1. netns 存在
ip netns list | grep feclaw-sandbox

# 2. veth 存在
ip link show fl-host

# 3. 公网访问正常（通过 nat）
sudo ip netns exec feclaw-sandbox curl -s --connect-timeout 5 https://api.github.com | head -2

# 4. 宿主机服务被阻断
sudo ip netns exec feclaw-sandbox timeout 3 bash -c \
  'echo "" | nc -w 2 10.200.0.1 8080' 2>&1 && echo "⚠️ 未阻断" || echo "✅ 正确阻断"

# 5. bwrap 内部完整验证
sudo ip netns exec feclaw-sandbox bwrap --dev /dev --proc /proc --tmpfs /tmp \
  --ro-bind /usr /usr --ro-bind /bin /bin --ro-bind /lib /lib --ro-bind-try /lib64 /lib64 \
  --ro-bind /etc/ssl /etc/ssl --ro-bind-try /etc/resolv.conf /etc/resolv.conf \
  --ro-bind-try /usr/local /usr/local --ro-bind-try /usr/local/bin /usr/local/bin \
  --ro-bind-try /etc/alternatives /etc/alternatives \
  --setenv HOME /tmp --setenv PYTHONDONTWRITEBYTECODE 1 \
  python3 -c "
import socket
# 测试 INPUT
try:
    socket.socket().connect(('10.200.0.1', 8080))
    print('❌ INPUT 应阻断')
except: print('✅ INPUT 阻断')

# 测试 FORWARD
try:
    socket.socket().connect(('192.168.1.1', 80))
    print('❌ FORWARD 应阻断')
except: print('✅ FORWARD 阻断')

# 测试公网
import urllib.request
r = urllib.request.urlopen('https://api.github.com', timeout=5)
print(f'✅ 公网可达 ({r.status})')
" 2>&1
```

## 回退

如果网络隔离导致问题，可以安全禁用：

```bash
# 停止并禁用 systemd 服务
sudo systemctl stop feclaw-netns.service
sudo systemctl disable feclaw-netns.service

# 清理 netns 和 iptables 规则
sudo /usr/local/libexec/feclaw/cleanup_sandbox_netns.sh
```

回退后，`NetworkIsolationManager.check()` 返回 `False`，SandboxManager 自动回到 `--share-net` 行为，**无需修改代码或重启后端**。

## 注意事项

1. **升级注意事项**：更新 `init_sandbox_netns.sh` 后需重新 `sudo systemctl restart feclaw-netns.service` 使新规则生效
2. **多用户环境**：sudoers 中的 `lch` 需替换为实际后端运行用户
3. **WSL2 重启**：Windows 重启或 WSL2 内核更新后需要重新运行 init 脚本（systemd 会自动处理）
4. **bwrap 版本**：`bwrap >= 0.4.0`；Ubuntu 20.04 自带 0.4.0（已测试）
5. **iptables 持久化**：WSL2 重启后 iptables 规则会丢失，依赖 systemd oneshot 重建
