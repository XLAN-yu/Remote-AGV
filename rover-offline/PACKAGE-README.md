# ROVER ONE 本地离线遥控包

这个包把小车遥控网页、FastAPI→UART 网关、nginx 配置和 Python 3.11 ARM64 wheels 放在一起。
安装完成后，网页由香橙派自己提供：手机连接 `ROVER-ONE` 热点并打开
`http://10.42.0.1` 即可，不经过云端，也不需要互联网或 Node.js。

> 连接 Wi-Fi 本身不会可靠地替用户自动打开浏览器。打开上述地址后，网页会自动连接同源
> `/ws`；为防止意外运动，刷新、重连或重新开机后仍必须人工长按“启用控制”。

## 支持基线

完整断网安装包面向：

- Orange Pi Zero 3 / 64 位 ARM（`aarch64`）
- Debian 12 或对应 Armbian 基线
- Python 3.11
- 已随系统镜像安装：systemd、NetworkManager、nginx、Python venv、curl、unzip

包内包含应用层 Python wheels，但不打包发行版的系统组件。如果目标是一张完全空白、且从未准备过
这些系统组件的 SD 卡，请先制作已包含上述组件的系统镜像；普通 ZIP 无法安全替代跨发行版的系统镜像。

## 1. 校验与解压

把 ZIP 和同名 `.sha256` 文件复制到香橙派。先在 ZIP 所在目录校验：

```bash
sha256sum -c ROVER-ONE-local-offline-v1.0.0.zip.sha256
unzip ROVER-ONE-local-offline-v1.0.0.zip -d rover-one-offline
cd rover-one-offline
```

如果文件名中的版本不同，请使用实际文件名。安装脚本还会逐个核验包内 `SHA256SUMS`。

## 2. 断网安装应用

```bash
sudo bash ./orange_pi/deploy/install-offline.sh
```

安装器不会运行 `apt`、`npm` 或联网 `pip`。它会：

- 安装到 `/opt/rover-one/releases/<版本>`；
- 原子更新 `/opt/rover-one/current`；
- 从随包 wheelhouse 创建 Python 虚拟环境；
- 保留已有 `/etc/rover-one/gateway.env`；
- 安装 `rover-gateway.service` 和 nginx 本地静态站；
- 用 `127.0.0.1:8000/health` 做启动检查，失败时尝试恢复旧版本。

默认环境仍是 `ROVER_DRY_RUN=1`，不会声称电机已执行指令。

## 3. 创建小车热点

先在串口终端、显示器键盘或有线网络下确认 Wi-Fi 接口名，因为开启热点会断开同一无线接口上的 SSH：

```bash
nmcli device status
nmcli -f WIFI-PROPERTIES.AP device show wlan0
sudo bash ./orange_pi/deploy/create-rover-hotspot.sh --interface wlan0
sudo systemctl restart nginx
```

脚本会隐藏输入并要求两次输入新的热点密码。不要把密码写入 ZIP、命令历史或网页源码。

## 4. 手机使用

1. 手机连接 `ROVER-ONE`，出现“此网络无互联网”时选择保持连接。
2. 浏览器打开 `http://10.42.0.1`。
3. 页面自动连接本地 `/ws`，但保持锁定。
4. 确认状态正常后，人工长按启用控制。

网页不能通过双击 `index.html` 的 `file://` 方式正式控车；该来源会被网关拒绝，以免任意本地网页
尝试连接小车。

## 5. UART 与实机启用

编辑：

```bash
sudoedit /etc/rover-one/gateway.env
```

先保留：

```ini
ROVER_SERIAL_PORT=/dev/ttyS5
ROVER_BAUD=115200
ROVER_WATCHDOG_MS=200
ROVER_ALLOWED_ORIGINS=http://10.42.0.1,http://rover-one.local
ROVER_DRY_RUN=1
```

串口设备名必须依据当前镜像和设备树确认。架空车轮，验证网页失焦、切后台、拔 Wi-Fi 后的停车，
再确认 STM32 独立执行 300 ms 串口超时。全部通过后才把 `ROVER_DRY_RUN` 改为 `0` 并运行：

```bash
sudo systemctl restart rover-gateway
```

## 6. 验证、日志与回退

```bash
sudo bash ./orange_pi/deploy/verify-offline.sh
systemctl --no-pager --full status rover-gateway nginx
journalctl -u rover-gateway -b --no-pager
```

升级后如需回到上一个已安装版本：

```bash
sudo bash /opt/rover-one/current/orange_pi/deploy/rollback-offline.sh
```

回退不会删除热点、UART 配置或历史版本。

## 安全边界

- nginx 只监听热点地址 `10.42.0.1:80`；网关只监听 `127.0.0.1:8000`。
- 不允许 `Origin: null` 或通配来源，控制端口不要转发到公网。
- 网页急停不能替代物理急停；物理急停应直接使 TB6612 失能。
- Linux 200 ms 与 STM32 300 ms 看门狗必须独立有效。
