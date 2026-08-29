#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly PACKAGE_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"
readonly INSTALL_ROOT="/opt/rover-one"
readonly RELEASES_DIR="${INSTALL_ROOT}/releases"
readonly CURRENT_LINK="${INSTALL_ROOT}/current"
readonly PREVIOUS_LINK="${INSTALL_ROOT}/previous"
readonly CONFIG_DIR="/etc/rover-one"
readonly WHEELHOUSE="${PACKAGE_ROOT}/wheelhouse/py311-linux-aarch64"

temporary_release=""
previous_target=""

die() {
  printf '错误：%s\n' "$*" >&2
  exit 1
}

note() {
  printf '==> %s\n' "$*"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "缺少系统命令：$1"
}

cleanup_failed_install() {
  if [[ -n "${temporary_release}" && -d "${temporary_release}" ]]; then
    case "${temporary_release}" in
      "${RELEASES_DIR}"/.*.installing.*) rm -rf -- "${temporary_release}" ;;
      *) printf '拒绝清理意外路径：%s\n' "${temporary_release}" >&2 ;;
    esac
  fi
}

rollback_current_link() {
  if [[ -n "${previous_target}" && -d "${previous_target}" ]]; then
    ln -sfn -- "${previous_target}" "${CURRENT_LINK}.rollback"
    mv -Tf -- "${CURRENT_LINK}.rollback" "${CURRENT_LINK}"
    systemctl restart rover-gateway.service >/dev/null 2>&1 || true
  fi
}

trap cleanup_failed_install ERR INT TERM

[[ ${EUID} -eq 0 ]] || die "请使用 sudo bash ./orange_pi/deploy/install-offline.sh 运行"
[[ -f "${PACKAGE_ROOT}/VERSION" ]] || die "找不到 VERSION；请从正式离线 ZIP 解压后的根目录运行"
[[ -f "${PACKAGE_ROOT}/SHA256SUMS" ]] || die "找不到 SHA256SUMS；离线包不完整"
[[ -f "${PACKAGE_ROOT}/web/index.html" ]] || die "找不到 web/index.html"
[[ -d "${WHEELHOUSE}" ]] || die "缺少 Python 3.11 arm64 wheelhouse；请重新生成完整离线包"

for command_name in sha256sum python3 systemctl nginx useradd usermod install cp mv ln readlink curl ip grep; do
  require_command "${command_name}"
done

machine_arch="$(uname -m)"
case "${machine_arch}" in
  aarch64|arm64) ;;
  *) die "此包只支持 64 位 ARM 香橙派，当前架构为 ${machine_arch}" ;;
esac

python_version="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
[[ "${python_version}" == "3.11" ]] || die "此包的离线 wheels 要求 Python 3.11，当前为 ${python_version}"

version="$(tr -d '\r\n' < "${PACKAGE_ROOT}/VERSION")"
[[ "${version}" =~ ^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$ ]] || die "VERSION 格式无效"
release_dir="${RELEASES_DIR}/${version}"
temporary_release="${RELEASES_DIR}/.${version}.installing.$$"

note "校验离线包文件"
(cd -- "${PACKAGE_ROOT}" && sha256sum -c SHA256SUMS)

if [[ -e "${release_dir}" ]]; then
  die "版本 ${version} 已存在于 ${release_dir}；请使用新版本号重新打包"
fi

if ! id rover >/dev/null 2>&1; then
  note "创建受限的 rover 服务账户"
  useradd --system --create-home --home-dir /var/lib/rover-one --shell /usr/sbin/nologin rover
fi
usermod -aG dialout rover

install -d -m 0755 -- "${RELEASES_DIR}"
install -d -m 0755 -- "${temporary_release}/web" "${temporary_release}/orange_pi/gateway" "${temporary_release}/orange_pi/deploy"

note "安装本地静态网页和网关源码"
cp -a -- "${PACKAGE_ROOT}/web/." "${temporary_release}/web/"
cp -a -- "${PACKAGE_ROOT}/orange_pi/gateway/." "${temporary_release}/orange_pi/gateway/"
cp -a -- "${PACKAGE_ROOT}/orange_pi/deploy/." "${temporary_release}/orange_pi/deploy/"
install -m 0644 -- "${PACKAGE_ROOT}/orange_pi/README.md" "${temporary_release}/orange_pi/README.md"

note "从随包 wheelhouse 创建离线 Python 环境"
python3 -m venv "${temporary_release}/orange_pi/gateway/.venv"
"${temporary_release}/orange_pi/gateway/.venv/bin/python" -m pip install \
  --disable-pip-version-check \
  --no-index \
  --find-links "${WHEELHOUSE}" \
  -r "${temporary_release}/orange_pi/gateway/requirements-runtime.txt"
"${temporary_release}/orange_pi/gateway/.venv/bin/python" -c 'import fastapi, serial, uvicorn'

chown -R root:root -- "${temporary_release}"
chmod -R go-w -- "${temporary_release}"
mv -- "${temporary_release}" "${release_dir}"
temporary_release=""

if [[ -L "${CURRENT_LINK}" ]]; then
  previous_target="$(readlink -f -- "${CURRENT_LINK}")"
  case "${previous_target}" in
    "${RELEASES_DIR}"/*) ;;
    *) die "现有 current 链接不在 ${RELEASES_DIR} 内，拒绝覆盖" ;;
  esac
fi

ln -sfn -- "${release_dir}" "${CURRENT_LINK}.next"
mv -Tf -- "${CURRENT_LINK}.next" "${CURRENT_LINK}"

install -d -m 0750 -o root -g rover -- "${CONFIG_DIR}"
if [[ ! -f "${CONFIG_DIR}/gateway.env" ]]; then
  install -m 0600 -o root -g rover \
    "${release_dir}/orange_pi/deploy/gateway.env.example" \
    "${CONFIG_DIR}/gateway.env"
  note "已创建 ${CONFIG_DIR}/gateway.env（默认仍为 dry-run）"
else
  note "保留现有 ${CONFIG_DIR}/gateway.env"
fi

install -m 0644 -- \
  "${release_dir}/orange_pi/deploy/systemd/rover-gateway.service" \
  /etc/systemd/system/rover-gateway.service
install -m 0644 -- \
  "${release_dir}/orange_pi/deploy/nginx/rover-one.conf" \
  /etc/nginx/sites-available/rover-one
ln -sfn -- /etc/nginx/sites-available/rover-one /etc/nginx/sites-enabled/rover-one

# Remove only the legacy unit created by earlier ROVER-ONE packages.
if [[ -f /etc/systemd/system/rover-web.service ]] \
  && grep -q 'ROVER-ONE Vinext web application' /etc/systemd/system/rover-web.service; then
  systemctl disable --now rover-web.service >/dev/null 2>&1 || true
  rm -f -- /etc/systemd/system/rover-web.service
fi

nginx -t
systemctl daemon-reload
systemctl enable rover-gateway.service nginx.service >/dev/null
systemctl restart rover-gateway.service

gateway_ready=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl --fail --silent --show-error http://127.0.0.1:8000/health >/dev/null; then
    gateway_ready=1
    break
  fi
  sleep 0.25
done

if [[ ${gateway_ready} -ne 1 ]]; then
  rollback_current_link
  die "新网关健康检查失败；已尝试恢复旧版本。请查看 journalctl -u rover-gateway"
fi

if [[ -n "${previous_target}" && -d "${previous_target}" ]]; then
  ln -sfn -- "${previous_target}" "${PREVIOUS_LINK}.next"
  mv -Tf -- "${PREVIOUS_LINK}.next" "${PREVIOUS_LINK}"
fi

if ip -4 address show | grep -q '10\.42\.0\.1/'; then
  systemctl restart nginx.service
  note "本地网页已启动：http://10.42.0.1"
else
  note "应用已安装；热点尚未持有 10.42.0.1，因此没有强行启动 nginx"
  note "下一步运行 create-rover-hotspot.sh，再执行：sudo systemctl restart nginx"
fi

note "安装完成。实机前请核对 ${CONFIG_DIR}/gateway.env，并保持 ROVER_DRY_RUN=1 完成安全测试"
