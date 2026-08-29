# Remote-AGV

ROVER ONE 的本地离线遥控网页与 Orange Pi 网关部署源码。

## 在线演示

推送到 GitHub 后，GitHub Pages 工作流会发布静态控制界面。该页面用于演示界面和交互，不能通过互联网直接连接真实小车。

真实小车控制仍应保持在局域网：手机连接 `ROVER-ONE` 热点后，打开 `http://10.42.0.1`。网页经 Orange Pi 的同源 WebSocket 发送线速度与角速度，STM32 负责校验、看门狗和电机闭环。

## 本机预览

安装 Node.js 后，在仓库根目录运行：

```powershell
npm install
npm run build:rover-offline
python -m http.server 3001 --bind 127.0.0.1 --directory rover-offline/dist
```

然后打开 `http://127.0.0.1:3001/`。也可以双击 `启动本地遥控网页.cmd`。

## 香橙派安装

构建完整离线包：

```powershell
.\build-rover-offline-package.ps1
```

将生成的 ZIP 复制到 Orange Pi 并解压，按 [orange_pi/README.md](orange_pi/README.md) 完成安装、热点设置和 UART 配置。

## 安全边界

- 公共网页不应绕过小车局域网、命令仲裁或串口安全帧。
- 网关 200 ms 无有效网页指令即归零；STM32 300 ms 无有效串口帧即失能。
- 网页急停不能替代直接使电机驱动失能的物理急停。

