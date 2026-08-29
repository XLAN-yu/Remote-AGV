#!/usr/bin/env bash
set -Eeuo pipefail

readonly CURRENT_ROOT="/opt/rover-one/current"

failures=0

check() {
  local label="$1"
  shift
  if "$@"; then
    printf '[通过] %s\n' "${label}"
  else
    printf '[失败] %s\n' "${label}" >&2
    failures=$((failures + 1))
  fi
}

check "静态首页存在" test -f "${CURRENT_ROOT}/web/index.html"
check "没有 Node 网页服务" bash -c '! systemctl is-enabled rover-web.service >/dev/null 2>&1'
check "网关服务运行" systemctl is-active --quiet rover-gateway.service
check "网关只在本机健康" curl --fail --silent --show-error http://127.0.0.1:8000/health
check "nginx 配置有效" nginx -t

if ip -4 address show | grep -q '10\.42\.0\.1/'; then
  check "热点网页可访问" curl --fail --silent --show-error http://10.42.0.1/
  check "同源健康接口可访问" curl --fail --silent --show-error http://10.42.0.1/health
else
  printf '[提示] 当前没有 10.42.0.1 热点地址，跳过手机入口检查\n'
fi

if grep -R -E -i 'https?://(unpkg|cdn\.jsdelivr|fonts\.googleapis)' "${CURRENT_ROOT}/web" >/dev/null 2>&1; then
  printf '[失败] 网页包包含远程 CDN 地址\n' >&2
  failures=$((failures + 1))
else
  printf '[通过] 网页包不含常见远程 CDN 地址\n'
fi

exit "${failures}"
