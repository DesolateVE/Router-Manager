#!/bin/sh

# ================= 配置区 =================
TPROXY_PORT=7893       # Mihomo 的 TProxy 端口
DNS_PORT=1053          # Mihomo 的 DNS 监听端口
ROUTING_MARK=6666      # 重要：需与 config.yaml 中的 routing-mark 一致
PROXY_FWMARK=1         # 转发流量使用的标记
TABLE_ID=100           # 路由表 ID
# ==========================================

echo "🔄 开始初始化 Mihomo IPv4-Only 网络环境..."

# --- 1. 加载内核模块 ---
modprobe nft_tproxy 2>/dev/null

# --- 2. 配置 DNS 转发 (保持不变) ---
if [ -f /etc/config/dhcp ]; then
    uci set dhcp.@dnsmasq[0].noresolv='1' 2>/dev/null
    # dnsmasq 的 server 是 list 类型，使用 add_list 才会正确写入运行时配置
    uci delete dhcp.@dnsmasq[0].server 2>/dev/null
    uci add_list dhcp.@dnsmasq[0].server="127.0.0.1#${DNS_PORT}" 2>/dev/null
    uci set dhcp.@dnsmasq[0].rebind_protection='0' 2>/dev/null
    uci commit dhcp 2>/dev/null
    [ -x /etc/init.d/dnsmasq ] && /etc/init.d/dnsmasq restart
fi

# --- 3. 清理旧规则 ---
ip rule del fwmark $PROXY_FWMARK table $TABLE_ID 2>/dev/null
ip route del local default dev lo table $TABLE_ID 2>/dev/null
nft delete table inet mihomo 2>/dev/null

# --- 4. 设置策略路由 (IPv4 Only) ---
ip rule add fwmark $PROXY_FWMARK table $TABLE_ID
ip route add local default dev lo table $TABLE_ID

# --- 5. 应用 nftables 规则 ---
# OpenWrt 的 nft 版本可能不会从 "table inet mihomo { ... }" 声明自动创建表，
# 所以这里显式 add table/add chain/add rule。
nft -f - <<EOF
add table inet mihomo

add set inet mihomo reserved_ip { type ipv4_addr; flags interval; elements = {
    0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8,
    169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16,
    224.0.0.0/4, 240.0.0.0/4
} }

add chain inet mihomo prerouting { type filter hook prerouting priority mangle; policy accept; }
add rule inet mihomo prerouting meta nfproto != ipv4 return
add rule inet mihomo prerouting meta mark $ROUTING_MARK return
add rule inet mihomo prerouting fib daddr type local return
add rule inet mihomo prerouting ip daddr @reserved_ip return
add rule inet mihomo prerouting udp dport 53 tproxy ip to :$DNS_PORT meta mark set $PROXY_FWMARK accept
add rule inet mihomo prerouting tcp dport 53 tproxy ip to :$DNS_PORT meta mark set $PROXY_FWMARK accept
add rule inet mihomo prerouting meta l4proto { tcp, udp } tproxy ip to :$TPROXY_PORT meta mark set $PROXY_FWMARK accept

add chain inet mihomo output { type route hook output priority mangle; policy accept; }
add rule inet mihomo output meta nfproto != ipv4 return
add rule inet mihomo output meta mark $ROUTING_MARK return
add rule inet mihomo output ip daddr @reserved_ip return
add rule inet mihomo output udp dport 53 meta mark set $PROXY_FWMARK
add rule inet mihomo output tcp dport 53 meta mark set $PROXY_FWMARK
add rule inet mihomo output meta l4proto { tcp, udp } meta mark set $PROXY_FWMARK
EOF

if [ $? -ne 0 ]; then
    echo "❌ nftables TProxy 规则应用失败，请检查 nft_tproxy / kmod-nft-tproxy 是否已安装"
    exit 1
fi

echo "🎉 IPv4-Only TProxy 规则应用成功！"
echo "💡 IPv6 流量现在将绕过代理直连，且不会产生多播连接日志。"
