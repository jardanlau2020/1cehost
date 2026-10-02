#!/usr/bin/env bash
# 代理引导：装 sing-box → 验证真能出去 → 把结果写进 $GITHUB_ENV。
#
# 为什么要「真验证」而不是看进程：安装脚本本身是可失败的，装到一半挂掉时
# sing-box 进程可能还在，但代理并不通。老实现只 `pgrep -f sing-box` 就当通，
# 结果整个续期跑在死代理上，最终表现成「面板连不上」这种没法定位的失败。
# 这里的口径是：真经代理连一次 api.ipify.org 拿到 IP，才算通。
#
# 本脚本是 renew-kit composite action 的 setup-command，默认 continue-on-error，
# 所以任何一步失败都不会中断续期 —— 大不了退回直连。
set -uo pipefail

PORT="${ICEHOST_PROXY_PORT:-1080}"
PROBE_URL="${ICEHOST_PROXY_PROBE_URL:-https://api.ipify.org}"
INSTALLER="${ICEHOST_PROXY_INSTALLER:-https://main.ssss.nyc.mn/setup_proxy.sh}"

echo "── 1/3 安装代理 ──"
if command -v wget >/dev/null 2>&1; then
  bash <(wget -qO- "$INSTALLER") || echo "⚠️ 安装脚本返回非零（继续验证）"
else
  bash <(curl -fsSL "$INSTALLER") || echo "⚠️ 安装脚本返回非零（继续验证）"
fi

echo "── 2/3 验证出口 ──"
proxy=""
if pgrep -f sing-box >/dev/null 2>&1; then
  for i in 1 2 3; do
    ip="$(curl -s --max-time 15 -x "socks5h://127.0.0.1:${PORT}" "$PROBE_URL" || true)"
    if [ -n "$ip" ]; then
      proxy="socks5://127.0.0.1:${PORT}"
      echo "✅ 第 ${i} 次探测成功，出口 IP: ${ip}"
      break
    fi
    echo "⚠️ 第 ${i} 次探测不通，3 秒后重试…"
    sleep 3
  done
  [ -n "$proxy" ] || echo "⚠️ sing-box 进程在但代理 3 次都不通 → 回退直连"
else
  echo "no proxy, direct mode"
fi

echo "── 3/3 导出给后续步骤 ──"
# 走 GITHUB_ENV 而不是 export：export 只活在本 step 的 shell 里，
# 后面的「Run renewal script」step 是另一个进程，看不到。
if [ -n "$proxy" ] && [ -n "${GITHUB_ENV:-}" ]; then
  echo "PROXY_SERVER=${proxy}" >> "$GITHUB_ENV"
  echo "PROXY_SERVER=${proxy} 已写入 GITHUB_ENV"
else
  echo "本次不挂代理（PROXY_SERVER 未设置）"
fi
