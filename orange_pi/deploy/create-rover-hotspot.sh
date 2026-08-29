#!/usr/bin/env bash

set -euo pipefail

export LC_ALL=C

readonly DEFAULT_SSID="ROVER-ONE"
readonly DEFAULT_CONNECTION="rover-one-hotspot"
readonly DEFAULT_ADDRESS="10.42.0.1/24"

INTERFACE=""
SSID="$DEFAULT_SSID"
CONNECTION_NAME="$DEFAULT_CONNECTION"
ADDRESS="$DEFAULT_ADDRESS"
PASSWORD_FILE=""
ACTIVATE=1
ASSUME_YES=0
PASSWORD=""
PASSWORD_CONFIRM=""

usage() {
  cat <<'EOF'
Create a dedicated NetworkManager Wi-Fi access point for ROVER-ONE.

Usage:
  sudo ./create-rover-hotspot.sh --interface wlan0 [options]

Required:
  -i, --interface NAME       Wi-Fi interface selected by a human (for example wlan0)

Options:
  -s, --ssid NAME            Access-point SSID (default: ROVER-ONE)
  -a, --address CIDR         Private IPv4/CIDR (default: 10.42.0.1/24)
  -c, --connection NAME      NetworkManager profile (default: rover-one-hotspot)
  -p, --password-file FILE   Read the WPA2 password from a mode 600/400 file
      --no-activate          Create the profile without activating it
  -y, --yes                  Skip the final typed confirmation (automation only)
  -h, --help                 Show this help

If --password-file is omitted, the password is requested twice without echo.
For safety there is deliberately no --password argument: command-line passwords
can leak through shell history and process listings.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

validate_private_cidr() {
  local value="$1"
  local ip prefix o1 o2 o3 o4 octet

  [[ "$value" == */* ]] || die "Address must include a CIDR prefix (for example 10.42.0.1/24)."
  ip="${value%/*}"
  prefix="${value##*/}"
  [[ "$prefix" =~ ^[0-9]+$ ]] || die "Invalid CIDR prefix: $prefix"
  (( prefix >= 16 && prefix <= 30 )) || die "Use a private hotspot prefix between /16 and /30."

  IFS='.' read -r o1 o2 o3 o4 <<<"$ip"
  [[ -n "${o1:-}" && -n "${o2:-}" && -n "${o3:-}" && -n "${o4:-}" ]] || die "Invalid IPv4 address: $ip"
  for octet in "$o1" "$o2" "$o3" "$o4"; do
    [[ "$octet" =~ ^(0|[1-9][0-9]{0,2})$ ]] || die "Invalid IPv4 address: $ip"
    (( 10#$octet <= 255 )) || die "Invalid IPv4 address: $ip"
  done

  if ! ((
    10#$o1 == 10 ||
    (10#$o1 == 172 && 10#$o2 >= 16 && 10#$o2 <= 31) ||
    (10#$o1 == 192 && 10#$o2 == 168)
  )); then
    die "Hotspot address must be in RFC1918 private space."
  fi

  (( 10#$o4 >= 1 && 10#$o4 <= 254 )) || die "The hotspot host address cannot end in .0 or .255."
}

read_password() {
  local file_mode

  if [[ -n "$PASSWORD_FILE" ]]; then
    [[ -f "$PASSWORD_FILE" ]] || die "Password file does not exist: $PASSWORD_FILE"
    file_mode="$(stat -c '%a' -- "$PASSWORD_FILE")"
    (( (8#$file_mode & 077) == 0 )) || die "Password file must not be readable or writable by group/others (use chmod 600)."
    PASSWORD="$(<"$PASSWORD_FILE")"
    [[ "$PASSWORD" != *$'\n'* && "$PASSWORD" != *$'\r'* ]] || die "Password file must contain exactly one line."
  else
    [[ -t 0 ]] || die "No terminal available. Supply a protected --password-file."
    read -r -s -p 'Enter a new WPA2 password (12-63 printable ASCII characters): ' PASSWORD
    printf '\n'
    read -r -s -p 'Repeat the WPA2 password: ' PASSWORD_CONFIRM
    printf '\n'
    [[ "$PASSWORD" == "$PASSWORD_CONFIRM" ]] || die "Passwords do not match."
  fi

  (( ${#PASSWORD} >= 12 && ${#PASSWORD} <= 63 )) || die "WPA2 password must be 12-63 characters."
  [[ "$PASSWORD" =~ ^[[:print:]]+$ ]] || die "WPA2 password must contain printable ASCII characters only."
  [[ "$PASSWORD" != "$SSID" ]] || die "WPA2 password must not equal the SSID."
}

while (( $# > 0 )); do
  case "$1" in
    -i|--interface)
      (( $# >= 2 )) || die "$1 requires a value."
      INTERFACE="$2"
      shift 2
      ;;
    -s|--ssid)
      (( $# >= 2 )) || die "$1 requires a value."
      SSID="$2"
      shift 2
      ;;
    -a|--address)
      (( $# >= 2 )) || die "$1 requires a value."
      ADDRESS="$2"
      shift 2
      ;;
    -c|--connection)
      (( $# >= 2 )) || die "$1 requires a value."
      CONNECTION_NAME="$2"
      shift 2
      ;;
    -p|--password-file)
      (( $# >= 2 )) || die "$1 requires a value."
      PASSWORD_FILE="$2"
      shift 2
      ;;
    --no-activate)
      ACTIVATE=0
      shift
      ;;
    -y|--yes)
      ASSUME_YES=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1 (use --help)"
      ;;
  esac
done

(( EUID == 0 )) || die "Run this script with sudo."
[[ -n "$INTERFACE" ]] || die "--interface is required. Inspect 'nmcli device status' and choose the Wi-Fi interface yourself."
[[ "$INTERFACE" =~ ^[A-Za-z0-9_.:-]+$ ]] || die "Invalid interface name: $INTERFACE"
[[ -d "/sys/class/net/$INTERFACE" ]] || die "Network interface not found: $INTERFACE"
[[ -n "$SSID" && ${#SSID} -le 32 && "$SSID" =~ ^[[:print:]]+$ ]] || die "SSID must be 1-32 printable ASCII bytes."
[[ "$CONNECTION_NAME" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || die "Connection name may contain only letters, digits, dot, underscore and dash."
validate_private_cidr "$ADDRESS"

require_command nmcli
require_command stat

systemctl is-active --quiet NetworkManager || die "NetworkManager is not active. Do not run this script on a system managed by another network stack."

device_type="$(nmcli -g GENERAL.TYPE device show "$INTERFACE" 2>/dev/null || true)"
[[ "$device_type" == "wifi" ]] || die "$INTERFACE is not reported as a Wi-Fi device by NetworkManager (type: ${device_type:-unknown})."

ap_capability="$(nmcli -g WIFI-PROPERTIES.AP device show "$INTERFACE" 2>/dev/null || true)"
[[ "$ap_capability" == "yes" ]] || die "$INTERFACE does not report Wi-Fi AP capability."

[[ "$(nmcli -t -f WIFI general 2>/dev/null)" == "enabled" ]] || die "Wi-Fi radio is disabled. Enable it deliberately with 'sudo nmcli radio wifi on', then retry."

if nmcli -g UUID connection show "$CONNECTION_NAME" >/dev/null 2>&1; then
  die "A NetworkManager profile named '$CONNECTION_NAME' already exists. It was not changed. Inspect it with: nmcli connection show '$CONNECTION_NAME'"
fi

read_password
trap 'unset PASSWORD PASSWORD_CONFIRM' EXIT

active_connection="$(nmcli -g GENERAL.CONNECTION device show "$INTERFACE" 2>/dev/null || true)"
printf '\nThe following dedicated hotspot profile will be created:\n'
printf '  interface:  %s\n' "$INTERFACE"
printf '  current:    %s\n' "${active_connection:---}"
printf '  profile:    %s\n' "$CONNECTION_NAME"
printf '  SSID:       %s\n' "$SSID"
printf '  IPv4:       %s (NetworkManager shared mode provides DHCP/DNS)\n' "$ADDRESS"
printf '  activate:   %s\n\n' "$([[ "$ACTIVATE" == 1 ]] && printf yes || printf no)"

if [[ -n "$active_connection" && "$active_connection" != "--" ]]; then
  printf 'WARNING: activating the hotspot will disconnect "%s" from %s.\n' "$active_connection" "$INTERFACE" >&2
  printf 'No other connection profiles will be deleted or edited.\n' >&2
fi

if (( ASSUME_YES == 0 )); then
  [[ -t 0 ]] || die "Confirmation requires a terminal; use --yes only after verifying the interface."
  read -r -p "Type the Wi-Fi interface name '$INTERFACE' to continue: " confirmation
  [[ "$confirmation" == "$INTERFACE" ]] || die "Confirmation did not match; nothing was changed."
fi

nmcli connection add \
  type wifi \
  ifname "$INTERFACE" \
  con-name "$CONNECTION_NAME" \
  ssid "$SSID" >/dev/null

nmcli connection modify "$CONNECTION_NAME" \
  connection.interface-name "$INTERFACE" \
  connection.autoconnect yes \
  connection.autoconnect-priority 100 \
  802-11-wireless.mode ap \
  802-11-wireless.band bg \
  802-11-wireless.powersave 2 \
  802-11-wireless-security.key-mgmt wpa-psk \
  802-11-wireless-security.psk "$PASSWORD" \
  ipv4.method shared \
  ipv4.addresses "$ADDRESS" \
  ipv4.never-default yes \
  ipv6.method disabled

printf 'Created NetworkManager profile "%s".\n' "$CONNECTION_NAME"

if (( ACTIVATE == 1 )); then
  nmcli connection up "$CONNECTION_NAME" ifname "$INTERFACE" >/dev/null
  printf 'Hotspot is active. Assigned address(es):\n'
  nmcli -g IP4.ADDRESS device show "$INTERFACE" | sed '/^$/d; s/^/  /'
else
  printf 'Profile was not activated. Activate it later with:\n'
  printf '  sudo nmcli connection up %q ifname %q\n' "$CONNECTION_NAME" "$INTERFACE"
fi

printf '\nOpen http://%s after joining "%s".\n' "${ADDRESS%/*}" "$SSID"
printf 'The page may connect to /ws automatically, but driving remains locked until the operator explicitly enables control.\n'
