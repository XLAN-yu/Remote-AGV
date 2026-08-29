# ROVER ONE 香橙派本地离线部署

最终使用方式：手机连接 `ROVER-ONE` Wi-Fi，打开 `http://10.42.0.1`。网页、WebSocket 网关和
UART 桥接都在香橙派本机运行；没有云端依赖，也不需要互联网。页面加载后自动连接同源 `/ws`，
但**自动连接不等于自动解锁**，刷新、断线或重启后仍须人工长按启用控制。

```text
手机浏览器（ROVER-ONE 热点）
  └─ HTTP / WebSocket
       └─ nginx 10.42.0.1:80
            ├─ /          → /opt/rover-one/current/web 静态网页
            ├─ /ws        → FastAPI 127.0.0.1:8000
            └─ /health    → FastAPI 127.0.0.1:8000
                                  └─ 3.3 V UART → STM32
```

香橙派不再运行 Vinext/Node 服务，旧的 `rover-web.service` 已移除。即使网关进程暂时退出，nginx
仍能打开网页并显示断线状态。

## 离线包与支持基线

仓库根目录运行：

```powershell
.\build-rover-offline-package.ps1
```

或双击 `生成小车离线包.cmd`。生成物位于 `dist-packages/`，其中包含：

- 预构建的纯静态遥控网页；
- 网关源码和部署配置；
- Debian/Armbian 12、Python 3.11、ARM64 的 Python wheelhouse；
- 包内逐文件 `SHA256SUMS` 和 ZIP 外置 `.sha256`；
- 断网安装、验证与版本回退脚本。

完整包面向 64 位 Debian 12 / Armbian 基线，并假定系统镜像已安装 systemd、NetworkManager、
nginx、Python 3.11 venv、curl、unzip。安装脚本不会运行 `apt`、`npm` 或联网 `pip`。如果连这些
系统组件也必须从空白卡完全断网安装，应制作预制 SD 卡镜像，而不是使用跨发行版 ZIP。

生成的 ZIP 根目录 `README.md` 是面向现场安装者的完整步骤。

## 1. 安装离线包

把 ZIP 和 `.sha256` 复制到香橙派，使用实际版本文件名：

```bash
sha256sum -c ROVER-ONE-local-offline-v1.0.0.zip.sha256
unzip ROVER-ONE-local-offline-v1.0.0.zip -d rover-one-offline
cd rover-one-offline
sudo bash ./orange_pi/deploy/install-offline.sh
```

安装位置采用可回退版本目录：

```text
/opt/rover-one/releases/<版本>
/opt/rover-one/current  -> 当前版本
/opt/rover-one/previous -> 上一版本
/etc/rover-one/gateway.env
```

安装器保留已有 `gateway.env`，新装时默认使用 `ROVER_DRY_RUN=1`。它会从随包 wheelhouse 创建
虚拟环境，启动网关并检查 `http://127.0.0.1:8000/health`；新版本失败会尝试恢复旧链接。

## 2. 创建专用热点

必须先人工确认 Wi-Fi 接口名，不同镜像可能为 `wlan0`、`wlan1` 等：

```bash
nmcli device status
nmcli -f WIFI-PROPERTIES.AP device show wlan0
```

在通过同一个无线接口 SSH 时，启用热点会立刻断开 SSH。首次配置应使用串口终端、显示器键盘或
有线网口。把示例接口换成已确认的名称：

```bash
cd /opt/rover-one/current/orange_pi/deploy
sudo bash ./create-rover-hotspot.sh --interface wlan0
sudo systemctl restart nginx
```

脚本隐藏输入并要求两次输入 12–63 位热点密码，不会把密码写进仓库或命令参数。默认配置：

- SSID：`ROVER-ONE`
- 地址：`10.42.0.1/24`
- NetworkManager `shared` 模式，向手机提供 DHCP/DNS
- 配置名：`rover-one-hotspot`
- 开机自动连接

若修改默认地址，必须同步修改 nginx 的精确监听地址、`ROVER_ALLOWED_ORIGINS` 和访问说明。

## 3. 配置 UART

编辑受保护的配置：

```bash
sudoedit /etc/rover-one/gateway.env
```

默认示例：

```ini
ROVER_SERIAL_PORT=/dev/ttyS5
ROVER_BAUD=115200
ROVER_WATCHDOG_MS=200
ROVER_ALLOWED_ORIGINS=http://10.42.0.1,http://rover-one.local
ROVER_DRY_RUN=1
```

串口设备名只是示例，必须根据当前镜像、设备树 overlay 和引脚文档确认；还要检查目标 UART 没有被
内核控制台或 `serial-getty` 占用。不要为图省事批量禁用所有串口。

### 3.3 V 接线

```text
Orange Pi UART TXD  ─────> STM32 UART RX
Orange Pi UART RXD  <───── STM32 UART TX
Orange Pi GND       ────── STM32 GND
```

两端 GPIO 必须是 3.3 V 逻辑电平，通信双方必须共地；不要把 5 V 接到 UART 引脚。

先保持 dry-run 完成网页和 WebSocket 测试。断电并架空车轮，确认 UART、STM32 看门狗和物理急停
后，才把 `ROVER_DRY_RUN` 改成 `0`：

```bash
sudo systemctl restart rover-gateway
```

## 4. 手机使用

1. 加入 `ROVER-ONE`，接受“无互联网但保持连接”。
2. 打开 `http://10.42.0.1`。
3. 页面自动连接本机 `/ws`，无需手填 IP。
4. 确认 WebSocket、UART 和遥测均正常后，人工长按启用控制。

Wi-Fi 标准本身不能保证连接后自动弹出浏览器，本方案也不伪装公共强制门户；建议把地址加入手机
书签或打印二维码。控制页不能通过 `file://` 双击运行，因为网关会拒绝 `Origin: null`，避免任意
本地网页尝试连接小车。

## 5. 验证与日志

```bash
sudo bash /opt/rover-one/current/orange_pi/deploy/verify-offline.sh
systemctl --no-pager --full status rover-gateway nginx
curl --fail http://127.0.0.1:8000/health
curl --fail http://10.42.0.1/
curl --fail http://10.42.0.1/health
journalctl -u rover-gateway -b --no-pager
```

预期只有 nginx 在热点 `10.42.0.1:80` 对手机提供入口，FastAPI 只监听
`127.0.0.1:8000`，系统中没有 Node/Vinext 网页服务。

可选的 `.local` 名称需要系统已安装 avahi：

```bash
sudo hostnamectl set-hostname rover-one
sudo install -m 0644 /opt/rover-one/current/orange_pi/deploy/avahi/rover-one.service \
  /etc/avahi/services/rover-one.service
sudo systemctl enable --now avahi-daemon
```

部分手机不解析 `.local`，固定地址 `http://10.42.0.1` 始终是首选回退。

## 6. 更新与回退

用更高版本号重新生成 ZIP，例如：

```powershell
.\build-rover-offline-package.ps1 -Version 1.0.1
```

复制到香橙派并再次运行安装脚本即可。回到上一版本：

```bash
sudo bash /opt/rover-one/current/orange_pi/deploy/rollback-offline.sh
```

回退只切换已验证的 release 链接，不删除 `/etc/rover-one/gateway.env`、热点或历史版本。

## 安全看门狗

- 浏览器约 15 Hz 持续发指令，松手、失焦、切后台、关闭页或断链时尝试立即归零。
- 网页 500 ms 未收到新状态就锁定驾驶。
- 网关默认 200 ms 未收到有效驾驶包即向 STM32 发零速度。
- STM32 必须独立执行约 300 ms 串口超时，PWM 归零并关闭 TB6612 `STBY`。
- 物理急停应直接切断驱动使能，网页急停不能替代物理急停。

热点控制入口不得转发到公网。正式配置只允许
`http://10.42.0.1,http://rover-one.local`，不要加入 `null` 或 `*`。若未来接入上级网络，必须另加
TLS、身份认证、防火墙与控制权仲裁。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 手机提示热点无互联网 | 选择保持连接；这是纯本地热点的正常表现。 |
| `10.42.0.1` 打不开 | 检查 `ip -4 addr`、`systemctl status nginx`、`nginx -t`。 |
| 首页能开但显示断线 | 检查 `curl 127.0.0.1:8000/health` 和 `journalctl -u rover-gateway`。 |
| UART 异常 | 核对设备树、设备名、权限、波特率、TX/RX 交叉、共地及 console/getty 占用。 |
| 安装器拒绝 Python | 完整包固定支持 Python 3.11；换匹配镜像或为目标 ABI 重新制作 wheelhouse。 |
| `.local` 不可用 | 直接使用 `http://10.42.0.1`。 |
