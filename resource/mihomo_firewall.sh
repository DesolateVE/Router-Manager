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
nft -f - <<EOF
table inet mihomo {
    set reserved_ip {
        type ipv4_addr
        flags interval
        elements = {
            0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 
            169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 
            224.0.0.0/4, 240.0.0.0/4
        }
    }

    chain prerouting {
        type filter hook prerouting priority mangle; policy accept;
        
        # 1. 关键：只处理 IPv4。如果是 IPv6 流量，直接返回，不进入代理
        meta nfproto != ipv4 return

        # 2. 排除 Mihomo 自身发出的流量，避免代理回环
        meta mark $ROUTING_MARK return

        # 2.1 放行真正访问本机地址的连接；被策略路由重定向到 lo 的外部流量仍需继续进入 TProxy
        fib daddr type local return
        
        # 3. 排除内网/保留地址
        ip daddr @reserved_ip return

        # 4. 劫持 DNS (UDP/TCP)
        udp dport 53 tproxy to :$DNS_PORT meta mark set $PROXY_FWMARK accept
        tcp dport 53 tproxy to :$DNS_PORT meta mark set $PROXY_FWMARK accept
        
        # 5. TProxy 劫持其余所有 IPv4 TCP/UDP 流量
        meta l4proto { tcp, udp } tproxy to :$TPROXY_PORT meta mark set $PROXY_FWMARK accept
    }
    
    chain output {
        type route hook output priority mangle; policy accept;
        
        # 1. 只处理 IPv4
        meta nfproto != ipv4 return

        # 2. 排除 Mihomo 自身流量（核心：防止死循环）
        meta mark $ROUTING_MARK return
        
        # 3. 排除保留地址（避免对内网流量打标增加无谓开销）
        ip daddr @reserved_ip return
        
        # 4. 本机 DNS 劫持
        udp dport 53 meta mark set $PROXY_FWMARK
        tcp dport 53 meta mark set $PROXY_FWMARK
        
        # 5. 本机流量打标，使其重新路由并进入 prerouting 链进行 TProxy 截获
        meta l4proto { tcp, udp } meta mark set $PROXY_FWMARK
    }
}
EOF

echo "🎉 IPv4-Only TProxy 规则应用成功！"
echo "💡 IPv6 流量现在将绕过代理直连，且不会产生多播连接日志。"