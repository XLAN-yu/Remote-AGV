# Rover One V1：香橙派 WebSocket → UART 网关

这个目录是香橙派上的 WebSocket→UART 服务。推荐部署中它只监听
`127.0.0.1:8000`，由 nginx 把小车控制页的同源 `/ws` 转发进来；网关校验、限幅和仲裁后，
再把二进制帧写入 STM32 串口。热点、网页与 nginx 的完整安装步骤见
[`../README.md`](../README.md)。

> **重要：网关不能直接驱动任意 STM32 固件。** 实机模式要求 STM32 已按本文实现帧解析、
> CRC 校验、速度闭环、急停锁存和独立的 300 ms 通信看门狗。默认 `dry-run` 只测试网页和
> 网关，回执会明确显示 `simulated: true`、`applied: false`，不会声称电机已动作。

## 快速运行

```bash
cd /opt/rover-one/current/orange_pi/gateway
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-runtime.txt

# 无 STM32 时安全演示（默认也是 dry-run）
ROVER_SERIAL_PORT=dry-run python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

在香橙派本机检查 `http://127.0.0.1:8000/health`。按推荐部署完成 nginx 后，手机连接
`ROVER-ONE` 热点并访问 `http://10.42.0.1`，网页会自动使用
`ws://10.42.0.1/ws`。不要把内部 `8000` 端口直接暴露到热点或公网。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ROVER_SERIAL_PORT` | `dry-run` | 实机串口，如 `/dev/ttyS5`；`dry-run`/`mock` 不打开硬件 |
| `ROVER_BAUD` | `115200` | UART 波特率，必须为 1200–4000000 的整数 |
| `ROVER_DRY_RUN` | 未设置 | `1/true/yes/on` 强制 dry-run，即使提供了串口名 |
| `ROVER_WATCHDOG_MS` | `200` | 网页指令超时，允许 50–200 ms，不能配置得更慢 |
| `ROVER_ALLOWED_ORIGINS` | `*` | WebSocket Origin 白名单，逗号分隔；实机部署建议填网页来源 |
| `ROVER_WEB_ROOT` | 未设置 | 可选的网页构建目录；设置后 FastAPI 在 `/` 托管静态页面 |

例如限制只有小车页面可以连接：

```bash
ROVER_ALLOWED_ORIGINS=http://10.42.0.1,http://rover-one.local
```

## WebSocket 消息

控制端一次只允许一个客户端。第二个客户端会收到
`reason: "controller_busy"` 并以 WebSocket code `4409` 关闭。

网页发送的消息执行严格字段检查，不接受字符串数字、布尔数字、`NaN`、未知字段或越界值：

```json
{"type":"drive","linear":0.20,"angular":0.45,"seq":126}
{"type":"estop","seq":127}
{"type":"clear_estop","seq":128,"confirm":true}
```

- `linear` 范围为 `-1.0..1.0` m/s，`angular` 范围为 `-3.0..3.0` rad/s。
- `seq` 是 uint32；同一连接内必须递增，支持 `4294967295 → 0` 回绕。
- 合法的 `estop` 无条件锁存并发送零速度，即使其序号重复。
- `clear_estop` 必须显式携带 `confirm: true`。实机只会进入“等待 STM32 ACK”状态；只有
  transport sequence 匹配、`acked_type=CLEAR_ESTOP` 且 `result=OK` 时网关才解除锁存。
  清除后仍保持零速度，下一帧 `drive` 才能运动。

所有网关消息都使用 `type: "robot_status"`。网关接收不等于电机执行：

```json
{
  "type": "robot_status",
  "event": "ack",
  "ack": {
    "seq": 126,
    "accepted": true,
    "applied": false,
    "stage": "gateway",
    "queued_to_uart": true,
    "simulated": false,
    "reason": "accepted"
  }
}
```

STM32 回 ACK 后会再发送 `stage: "stm32"` 的回执，只有其中 `applied: true` 才表示适配固件
确认接受。dry-run 始终是 `stage: "dry_run"`、`applied: false`，并仅为页面演示每 250 ms
发送一条带 `simulated: true` 的模拟遥测；实机模式绝不生成伪遥测来掩盖 STM32 状态中断。

急停复位使用同一个网页 `seq` 返回两阶段 ACK：

| 情况 | `stage` | `accepted` / `applied` | `reason` | 网关锁存 |
| --- | --- | --- | --- | --- |
| CLEAR 已进入 UART 队列 | `gateway` | `true / false` | `awaiting_stm32_ack` | 保持 |
| 匹配的 STM32 ACK 为 OK | `stm32` | `true / true` | `estop_cleared` | 清除 |
| STM32 拒绝 | `stm32` | `false / false` | `stm32_rejected_<result>` | 保持 |
| ACK 类型不匹配 | `stm32` | `false / false` | `ack_type_mismatch` | 保持 |
| 1 秒未收到匹配 ACK | `gateway` | `false / false` | `stm32_ack_timeout` | 保持 |
| UART 入队失败 | `gateway` | `false / false` | `serial_queue_failed` | 保持 |
| 网关当前没有锁存 | `gateway` | `false / false` | `estop_not_latched` | 无待处理事务 |
| dry-run 模拟复位 | `dry_run` | `true / false` | `estop_cleared` | 仅清除模拟状态 |

STM32 `STATUS.estop=true` 会无条件把网关同步为锁存状态，适用于网关重启后的状态恢复；
`STATUS.estop=false` 只是遥测，**不能**自动解锁，解锁仍必须完成上述 CLEAR/ACK 事务。
等待 CLEAR ACK 期间周期上报的 `estop=true` 只维持锁存，不会取消该事务；V1 不依据重复的
电平样本推断物理急停按钮的新边沿。

同理，任何 `STATUS.fault_code != 0` 都会锁存 `hardware_fault`、立即插入高优先级零速并拒绝
后续 drive；之后的 `fault_code=0` 不会自动解锁。V1 复用 CLEAR_ESTOP 事务作为明确安全
复位：适配固件只有在急停已释放且故障确实可清除时才能返回 `OK`，匹配的 OK ACK 会同时
清除网关 estop 与 hardware-fault 锁存；否则必须返回 `HARDWARE_FAULT`。

## UART 二进制协议（版本 1）

所有多字节整数和 IEEE-754 float32 使用**小端序**：

| 偏移 | 长度 | 字段 |
| ---: | ---: | --- |
| 0 | 2 | 同步字节 `A5 5A` |
| 2 | 1 | 版本，固定 `01` |
| 3 | 1 | 消息类型 |
| 4 | 1 | flags，当前发送 `00` |
| 5 | 2 | payload 长度 uint16，最大 256 |
| 7 | 4 | transport sequence uint32 |
| 11 | N | payload |
| 11+N | 2 | CRC16，小端 |

CRC 使用 **CRC-16/CCITT-FALSE**：poly `0x1021`、init `0xFFFF`、refin/refout false、
xorout `0x0000`。计算范围从 `version`（偏移 2）到 payload 末尾，不包含同步字节和 CRC；
标准校验串 `123456789` 的结果为 `0x29B1`。

| 类型 | 方向 | payload（C/struct 表示） | 说明 |
| --- | --- | --- | --- |
| `0x01 DRIVE` | Orange Pi → STM32 | `<ff` | linear m/s、angular rad/s；STM32 换算轮速并闭环 |
| `0x02 ESTOP` | Orange Pi → STM32 | `<B`，固定 `01` | 锁存急停并关闭 PWM/STBY |
| `0x03 CLEAR_ESTOP` | Orange Pi → STM32 | `<B`，固定 `A5` | 请求清除软件急停，固件仍需检查物理急停和故障 |
| `0x80 STATUS` | STM32 → Orange Pi | `<HHiiiifffBB` | 见下表 |
| `0x81 ACK` | STM32 → Orange Pi | `<BB` | acked type、result；帧 sequence 回显被确认命令的 transport sequence |

`STATUS` payload 顺序：

1. `battery_mv` uint16；
2. `ultrasonic_mm` uint16，`0xFFFF` 表示不可用；
3. `encoder_fl/fr/rl/rr` 四个 int32 累计计数；
4. `measured_linear_mps`、`measured_angular_rps`、`imu_yaw_rad` 三个 float32；
5. `estop` uint8；
6. `fault_code` uint8，`0` 表示无故障，其余值由固件项目定义。

STM32 应持续发送 `STATUS`，建议 10 Hz、最低不低于 5 Hz。网页在 500 ms 未收到新状态时会把
遥测标记为过期并禁止解锁；`ultrasonic_mm=0xFFFF` 会立即显示“传感器不可用”，不会沿用旧距离。

`ACK result`：`0=OK`、`1=INVALID_PAYLOAD`、`2=ESTOP_LATCHED`、
`3=HARDWARE_FAULT`、`4=UNSUPPORTED`。

浏览器 `seq` 与 UART `transport sequence` 是两个独立序列。这样看门狗和断线产生的零速度帧
也总有新的 UART 序号，不会被 STM32 当成网页重复包丢弃。

## 必须同时实现的停车逻辑

1. 网页应在按住摇杆时以 10–20 Hz 持续发送 `drive`。
2. 网关在最后一个有效 `drive` 后 200 ms 发送零速度，并每 100 ms 重发零速度。
3. 普通非零 `DRIVE` 在串口发送端只保留最新值，不形成 FIFO 积压。急停、零速度、断连和
   看门狗停车会原子丢弃尚未发送的非零速度并走高优先级队列；重连串口的第一帧也是零速度。
   若 CLEAR 事务因断连、客户端退出、服务关闭或 ACK 超时而取消，尚未发送的 CLEAR 也会
   被原子删除后再插入高优先级零速；普通周期看门狗零速不会误删合法等待中的 CLEAR。
4. **UART 线被拔掉时 Orange Pi 已无法把“零速度”传给 STM32。** 因此 STM32 必须独立计时：
   300 ms 没收到 CRC 正确的控制帧就将四路 PWM 清零并关闭 TB6612 `STBY`。没有这段固件，
   不能宣称“串口断开即物理停车”。
5. 物理急停应直接控制电机使能，并且不能靠网页 `clear_estop` 绕过。

## 测试

```bash
cd orange_pi/gateway
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

测试覆盖 CRC/分帧/噪声恢复、严格 JSON 校验、序号回绕、200 ms 看门狗、串口断开状态、
两阶段急停复位、ACK 拒绝/超时、硬件状态单向同步，以及万帧速度积压下的停车优先级。
实车上线前还应在断开 Wi-Fi、拔掉 UART、杀死网关进程和重启香橙派四种条件下，逐项验证
TB6612 `STBY` 实际拉低。
