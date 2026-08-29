#!/usr/bin/env bash
set -Eeuo pipefail

readonly INSTALL_ROOT="/opt/rover-one"
readonly RELEASES_DIR="${INSTALL_ROOT}/releases"
readonly CURRENT_LINK="${INSTALL_ROOT}/current"
readonly PREVIOUS_LINK="${INSTALL_ROOT}/previous"

die() {
  printf '错误：%s\n' "$*" >&2
  exit 1
}

[[ ${EUID} -eq 0 ]] || die "请使用 sudo bash rollback-offline.sh 运行"
[[ -L "${CURRENT_LINK}" && -L "${PREVIOUS_LINK}" ]] || die "没有可回退的 previous 版本"

current_target="$(readlink -f -- "${CURRENT_LINK}")"
previous_target="$(readlink -f -- "${PREVIOUS_LINK}")"
case "${current_target}" in "${RELEASES_DIR}"/*) ;; *) die "current 链接目标无效" ;; esac
case "${previous_target}" in "${RELEASES_DIR}"/*) ;; *) die "previous 链接目标无效" ;; esac
[[ -d "${current_target}" && -d "${previous_target}" ]] || die "版本目录不存在"

ln -sfn -- "${previous_target}" "${CURRENT_LINK}.next"
mv -Tf -- "${CURRENT_LINK}.next" "${CURRENT_LINK}"
ln -sfn -- "${current_target}" "${PREVIOUS_LINK}.next"
mv -Tf -- "${PREVIOUS_LINK}.next" "${PREVIOUS_LINK}"

systemctl daemon-reload
systemctl restart rover-gateway.service
if systemctl is-active --quiet nginx.service; then
  systemctl reload nginx.service
fi

printf '已回退到：%s\n' "${previous_target}"
