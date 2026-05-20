#!/bin/sh

# ================= 配置区 =================
PROXY_FWMARK=1         # 转发流量使用的标记
TABLE_ID=100           # 路由表 ID
# ==========================================

echo "🧹 开始清理 Mihomo 网络配置..."

# --- 1. 删除 nftables 规则 ---
echo "1️⃣ 删除 nftables 规则..."
nft delete table inet mihomo 2>/dev/null && echo "✅ nftables 规则已删除" || echo "⚠️  nftables 规则不存在或已删除"

# --- 2. 删除策略路由 ---
echo "2️⃣ 删除策略路由..."
while ip rule del fwmark $PROXY_FWMARK table $TABLE_ID 2>/dev/null; do
    echo "  - 已删除 fwmark $PROXY_FWMARK 规则"
done

while ip route del local default dev lo table $TABLE_ID 2>/dev/null; do
    echo "  - 已删除路由表 $TABLE_ID"
done

ip route flush table $TABLE_ID 2>/dev/null
echo "✅ 策略路由已清理"

# --- 3. 恢复 DNS 配置 ---
echo "3️⃣ 恢复 DNS 配置..."
if [ -f /etc/config/dhcp ]; then
    uci delete dhcp.@dnsmasq[0].noresolv 2>/dev/null
    uci delete dhcp.@dnsmasq[0].server 2>/dev/null
    uci delete dhcp.@dnsmasq[0].rebind_protection 2>/dev/null
    uci commit dhcp 2>/dev/null
    
    if [ -x /etc/init.d/dnsmasq ]; then
        /etc/init.d/dnsmasq restart
        echo "✅ DNS 配置已恢复"
    else
        killall -HUP dnsmasq 2>/dev/null
        echo "✅ DNS 已重载"
    fi
else
    echo "⚠️  未找到 UCI 配置，请手动检查 dnsmasq 配置"
fi

echo ""
echo "🎉 Mihomo 网络配置已完全清理！"
echo "📊 验证命令:"
echo "  - nft list tables"
echo "  - ip rule show"
echo "  - ip route show table $TABLE_ID"
