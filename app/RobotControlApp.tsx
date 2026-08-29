"use client";

import {
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import styles from "./robot-control.module.css";

type TransportMode = "demo" | "live";
type ConnectionState = "demo" | "connecting" | "connected" | "disconnected";
type ConnectionProfile = "demo" | "same-origin" | "hotspot" | "manual";
type ControlState = "locked" | "armed" | "estop";
type SpeedKey = "precision" | "standard" | "sport";
type Vector = { x: number; y: number };

type Telemetry = {
  batteryVoltage: number;
  batteryPercent: number;
  distanceCm: number | null;
  serialConnected: boolean;
  simulated: boolean;
  faultCode: number;
  latencyMs: number | null;
  packets: number;
  lastUpdate: number;
};

const SPEED_PRESETS: Record<SpeedKey, { label: string; hint: string; linear: number; angular: number }> = {
  precision: { label: "精细", hint: "25%", linear: 0.15, angular: 0.45 },
  standard: { label: "标准", hint: "60%", linear: 0.3, angular: 0.9 },
  sport: { label: "高速", hint: "100%", linear: 0.5, angular: 1.4 },
};

const DEFAULT_TELEMETRY: Telemetry = {
  batteryVoltage: 12.4,
  batteryPercent: 84,
  distanceCm: 86,
  serialConnected: true,
  simulated: true,
  faultCode: 0,
  latencyMs: 18,
  packets: 126,
  lastUpdate: Date.now(),
};

const HOTSPOT_WEB_URL = "http://10.42.0.1";
const HOTSPOT_ENDPOINT = "ws://10.42.0.1/ws";
const ENDPOINT_STORAGE_KEY = "rover-one-ws-url";
const AUTO_CONNECT_STORAGE_KEY = "rover-one-auto-connect";
const TELEMETRY_STALE_MS = 500;

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const record = (value: unknown): Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
const interactiveTarget = (target: EventTarget | null) =>
  target instanceof HTMLElement && Boolean(target.closest("input, textarea, select, button, [contenteditable='true']"));

function estimateBatteryPercent(voltage: number) {
  return Math.round(clamp(((voltage - 10.5) / 2.1) * 100, 0, 100));
}

function normalizeJoystick(x: number, y: number): Vector {
  const magnitude = Math.hypot(x, y);
  if (magnitude <= 0.08) return { x: 0, y: 0 };
  const limited = Math.min(1, magnitude);
  const scaled = (limited - 0.08) / 0.92;
  return { x: (x / magnitude) * scaled, y: (y / magnitude) * scaled };
}

function deriveWebSocketUrl() {
  if (typeof window === "undefined") return "ws://orangepi.local/ws";
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}/ws`;
}

function normalizedHostname(hostname: string) {
  return hostname.trim().toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
}

function isPrivateIpv4(hostname: string) {
  const octets = hostname.split(".").map(Number);
  if (octets.length !== 4 || octets.some((octet) => !Number.isInteger(octet) || octet < 0 || octet > 255)) return false;
  return octets[0] === 10
    || (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31)
    || (octets[0] === 192 && octets[1] === 168);
}

function isDirectRobotHost(hostname: string) {
  const host = normalizedHostname(hostname);
  return host === "rover-one.local"
    || host === "orangepi.local"
    || isPrivateIpv4(host)
    || /^(?:fc|fd)[0-9a-f]{2}:/.test(host);
}

function mustStartInDemo(hostname: string) {
  const host = normalizedHostname(hostname);
  return host === "localhost"
    || host.endsWith(".localhost")
    || host === "127.0.0.1"
    || host === "::1"
    || host === "chatgpt.site"
    || host.endsWith(".chatgpt.site")
    || host === "chatgpt.com"
    || host.endsWith(".chatgpt.com");
}

export function RobotControlApp() {
  const [transport, setTransport] = useState<TransportMode>("demo");
  const [connection, setConnection] = useState<ConnectionState>("demo");
  const [connectionProfile, setConnectionProfile] = useState<ConnectionProfile>("demo");
  const [controlState, setControlState] = useState<ControlState>("locked");
  const [speedKey, setSpeedKey] = useState<SpeedKey>("standard");
  const [vector, setVector] = useState<Vector>({ x: 0, y: 0 });
  const [telemetry, setTelemetry] = useState<Telemetry>(DEFAULT_TELEMETRY);
  const [endpoint, setEndpoint] = useState("");
  const [endpointDraft, setEndpointDraft] = useState("");
  const [autoConnect, setAutoConnect] = useState(true);
  const [directHostDetected, setDirectHostDetected] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [reconnectKey, setReconnectKey] = useState(0);
  const [armProgress, setArmProgress] = useState(0);
  const [resetPending, setResetPending] = useState(false);
  const [clock, setClock] = useState(DEFAULT_TELEMETRY.lastUpdate);
  const [lastCommand, setLastCommand] = useState({ linear: 0, angular: 0, seq: 0 });
  const [toast, setToast] = useState<string | null>(null);

  const joystickRef = useRef<HTMLDivElement>(null);
  const pointerIdRef = useRef<number | null>(null);
  const vectorRef = useRef<Vector>({ x: 0, y: 0 });
  const inputActiveRef = useRef(false);
  const keysRef = useRef(new Set<string>());
  const speedRef = useRef(SPEED_PRESETS.standard);
  const controlStateRef = useRef<ControlState>("locked");
  const linkReadyRef = useRef(false);
  const wsRef = useRef<WebSocket | null>(null);
  const seqRef = useRef(0);
  const pendingPacketsRef = useRef(new Map<number, number>());
  const armTickerRef = useRef<number | null>(null);
  const armStartedRef = useRef(0);
  const suppressArmClickRef = useRef(false);
  const resetPendingRef = useRef(false);
  const resetSeqRef = useRef<number | null>(null);
  const toastTimerRef = useRef<number | null>(null);

  const showToast = useCallback((message: string) => {
    setToast(message);
    if (toastTimerRef.current) window.clearTimeout(toastTimerRef.current);
    toastTimerRef.current = window.setTimeout(() => setToast(null), 2600);
  }, []);

  const clearArmTicker = useCallback(() => {
    if (armTickerRef.current) window.clearInterval(armTickerRef.current);
    armTickerRef.current = null;
  }, []);

  useEffect(() => {
    const stored = window.localStorage.getItem(ENDPOINT_STORAGE_KEY);
    const storedAutoConnect = window.localStorage.getItem(AUTO_CONNECT_STORAGE_KEY);
    const autoConnectEnabled = storedAutoConnect === null || storedAutoConnect === "true";
    const directHost = isDirectRobotHost(window.location.hostname) && !mustStartInDemo(window.location.hostname);
    const initial = directHost ? deriveWebSocketUrl() : stored || HOTSPOT_ENDPOINT;

    // Hydrate browser-only host and storage state after the server render.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setAutoConnect(autoConnectEnabled);
    setDirectHostDetected(directHost);
    setEndpoint(initial);
    setEndpointDraft(initial);
    if (directHost && autoConnectEnabled) {
      setConnectionProfile("same-origin");
      setTelemetry((current) => ({ ...current, serialConnected: false, simulated: false, latencyMs: null, packets: 0, lastUpdate: 0 }));
      setConnection("connecting");
      setTransport("live");
    }
    return () => {
      if (toastTimerRef.current) window.clearTimeout(toastTimerRef.current);
    };
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (transport !== "demo") return;
    const timer = window.setInterval(() => {
      setTelemetry((current) => ({
        ...current,
        batteryVoltage: clamp(current.batteryVoltage + (Math.random() - 0.52) * 0.015, 11.8, 12.5),
        batteryPercent: Math.round(clamp(current.batteryPercent + (Math.random() - 0.55) * 0.2, 68, 92)),
        distanceCm: Math.round(clamp((current.distanceCm ?? 86) + (Math.random() - 0.5) * 4, 54, 118)),
        latencyMs: Math.round(14 + Math.random() * 13),
        packets: current.packets + 1,
        lastUpdate: Date.now(),
      }));
    }, 900);
    return () => window.clearInterval(timer);
  }, [transport]);

  const resetMotion = useCallback(() => {
    inputActiveRef.current = false;
    pointerIdRef.current = null;
    keysRef.current.clear();
    vectorRef.current = { x: 0, y: 0 };
    setVector({ x: 0, y: 0 });
    setLastCommand((current) => ({ ...current, linear: 0, angular: 0 }));
  }, []);

  const sendPacket = useCallback((type: string, payload: Record<string, unknown>) => {
    seqRef.current = (seqRef.current + 1) >>> 0;
    const seq = seqRef.current;
    const packet = { type, ...payload, seq };
    if (transport === "demo") {
      if (type === "drive") {
        setTelemetry((current) => ({
          ...current,
          latencyMs: Math.round(12 + Math.random() * 15),
          packets: current.packets + 1,
          lastUpdate: Date.now(),
        }));
      }
      return seq;
    }
    const socket = wsRef.current;
    if (socket?.readyState === WebSocket.OPEN) {
      const isNonZeroDrive = type === "drive" && (payload.linear !== 0 || payload.angular !== 0);
      if (isNonZeroDrive && socket.bufferedAmount > 0) {
        socket.send(JSON.stringify({ type: "drive", linear: 0, angular: 0, seq }));
        socket.close(4002, "control backlog");
        showToast("控制链路出现指令积压，已断开并锁定");
        return seq;
      }
      socket.send(JSON.stringify(packet));
      const now = performance.now();
      for (const [pendingSeq, sentAt] of pendingPacketsRef.current) {
        if (now - sentAt > 5000 || pendingPacketsRef.current.size > 128) pendingPacketsRef.current.delete(pendingSeq);
      }
      pendingPacketsRef.current.set(seq, now);
    }
    return seq;
  }, [showToast, transport]);

  const sendDrive = useCallback((nextVector: Vector) => {
    const preset = speedRef.current;
    const linear = Number(clamp(-nextVector.y * preset.linear, -preset.linear, preset.linear).toFixed(3));
    const angular = Number(clamp(-nextVector.x * preset.angular, -preset.angular, preset.angular).toFixed(3));
    const seq = sendPacket("drive", { linear, angular });
    setLastCommand({ linear, angular, seq });
  }, [sendPacket]);

  const sendZeroBurst = useCallback(() => {
    resetMotion();
    sendDrive({ x: 0, y: 0 });
    window.setTimeout(() => sendDrive({ x: 0, y: 0 }), 45);
    window.setTimeout(() => sendDrive({ x: 0, y: 0 }), 100);
  }, [resetMotion, sendDrive]);

  useEffect(() => {
    if (transport !== "live" || !endpoint) return;
    let cancelled = false;
    let retryTimer: number | null = null;
    let retryCount = 0;
    const pendingPackets = pendingPacketsRef.current;

    const openSocket = () => {
      if (cancelled) return;
      setConnection("connecting");
      let socket: WebSocket;
      try {
        socket = new WebSocket(endpoint);
      } catch {
        setConnection("disconnected");
        return;
      }
      wsRef.current = socket;
      socket.onopen = () => {
        if (cancelled) return;
        retryCount = 0;
        setConnection("connected");
        clearArmTicker();
        setArmProgress(0);
        if (controlStateRef.current !== "estop") {
          controlStateRef.current = "locked";
          setControlState("locked");
        }
        resetMotion();
        setTelemetry((current) => ({ ...current, serialConnected: false, latencyMs: null, lastUpdate: 0 }));
        if (controlStateRef.current === "estop") {
          seqRef.current = (seqRef.current + 1) >>> 0;
          socket.send(JSON.stringify({ type: "estop", seq: seqRef.current }));
        }
        seqRef.current = (seqRef.current + 1) >>> 0;
        const zeroSeq = seqRef.current;
        socket.send(JSON.stringify({ type: "drive", linear: 0, angular: 0, seq: zeroSeq }));
        setLastCommand({ linear: 0, angular: 0, seq: zeroSeq });
        showToast(controlStateRef.current === "estop"
          ? "WebSocket 已连接；已重新发送急停锁存"
          : "WebSocket 已连接；控制保持锁定，等待车辆状态");
      };
      socket.onmessage = (event) => {
        let message: Record<string, unknown>;
        try {
          message = JSON.parse(String(event.data)) as Record<string, unknown>;
        } catch {
          return;
        }
        const data = Object.keys(record(message.data)).length ? record(message.data) : message;
        const gateway = record(message.gateway);
        const statusTelemetry = record(message.telemetry);
        const ack = record(message.ack);
        const voltage = finite(statusTelemetry.battery_v)
          ? statusTelemetry.battery_v
          : finite(statusTelemetry.batteryVoltage)
            ? statusTelemetry.batteryVoltage
            : finite(data.batteryVoltage)
              ? data.batteryVoltage
              : finite(data.voltage)
                ? data.voltage
                : undefined;
        let distance: number | null | undefined;
        if ("ultrasonic_m" in statusTelemetry) {
          distance = finite(statusTelemetry.ultrasonic_m) ? statusTelemetry.ultrasonic_m * 100 : null;
        } else if (finite(statusTelemetry.distanceCm)) {
          distance = statusTelemetry.distanceCm;
        } else if (statusTelemetry.distanceCm === null) {
          distance = null;
        } else if (finite(data.distanceCm)) {
          distance = data.distanceCm;
        } else if (data.distanceCm === null) {
          distance = null;
        } else if (finite(data.distance)) {
          distance = data.distance;
        }
        const percentage = finite(statusTelemetry.batteryPercent)
          ? statusTelemetry.batteryPercent
          : finite(data.batteryPercent)
            ? data.batteryPercent
            : undefined;
        const uart = typeof gateway.serial_ready === "boolean"
          ? gateway.serial_ready
          : typeof statusTelemetry.serialReady === "boolean"
            ? statusTelemetry.serialReady
            : typeof data.serialConnected === "boolean"
              ? data.serialConnected
              : typeof data.uart === "boolean"
                ? data.uart
                : typeof data.serial === "string"
                  ? data.serial.toLowerCase() === "connected"
                  : undefined;
        const simulated = typeof gateway.simulated === "boolean"
          ? gateway.simulated
          : typeof statusTelemetry.simulated === "boolean"
            ? statusTelemetry.simulated
            : undefined;
        const gatewayEstop = typeof gateway.estop_latched === "boolean"
          ? gateway.estop_latched
          : typeof statusTelemetry.estop === "boolean"
            ? statusTelemetry.estop
            : undefined;
        const hardwareFaultLatched = gateway.hardware_fault_latched === true;
        const statusFaultCode = hardwareFaultLatched
          ? finite(gateway.hardware_fault_code)
            ? Math.max(1, Math.trunc(gateway.hardware_fault_code))
            : 1
          : finite(statusTelemetry.fault_code)
            ? Math.max(0, Math.trunc(statusTelemetry.fault_code))
            : finite(gateway.hardware_fault_code)
              ? Math.max(0, Math.trunc(gateway.hardware_fault_code))
              : undefined;
        const ackSeq = finite(ack.seq)
          ? ack.seq
          : finite(message.ackSeq)
            ? message.ackSeq
            : finite(data.ackSeq)
              ? data.ackSeq
              : undefined;
        let latency: number | null | undefined;
        if (ackSeq !== undefined) {
          const sentAt = pendingPackets.get(ackSeq);
          if (sentAt !== undefined) {
            latency = Math.round(performance.now() - sentAt);
          }
          for (const pendingSeq of pendingPackets.keys()) {
            if (pendingSeq <= ackSeq) pendingPackets.delete(pendingSeq);
          }
        }
        const ackReason = typeof ack.reason === "string" ? ack.reason : "";
        const ackStage = typeof ack.stage === "string" ? ack.stage : "";
        if ((gatewayEstop === true || hardwareFaultLatched) && controlStateRef.current !== "estop") {
          resetMotion();
          resetPendingRef.current = false;
          resetSeqRef.current = null;
          controlStateRef.current = "estop";
          setResetPending(false);
          setControlState("estop");
          showToast(hardwareFaultLatched ? "STM32 报告硬件故障，控制已锁定" : "设备报告急停已锁定");
        }
        const isResetAck = resetPendingRef.current
          && resetSeqRef.current !== null
          && ackSeq === resetSeqRef.current;
        const isResetConfirmation = isResetAck
          && ack.accepted === true
          && ackReason === "estop_cleared"
          && ((ackStage === "stm32" && ack.applied === true)
            || (ackStage === "dry_run" && (ack.simulated === true || simulated === true)));
        if (isResetConfirmation) {
          resetPendingRef.current = false;
          resetSeqRef.current = null;
          controlStateRef.current = "locked";
          setResetPending(false);
          setControlState("locked");
          showToast("急停已解除，控制仍保持锁定");
        } else if (isResetAck && ackReason === "already_clear") {
          resetPendingRef.current = false;
          resetSeqRef.current = null;
          setResetPending(false);
          seqRef.current = (seqRef.current + 1) >>> 0;
          socket.send(JSON.stringify({ type: "estop", seq: seqRef.current }));
          showToast("网关急停状态不同步，已重新锁存；请确认后再次复位");
        } else if (isResetAck && ack.accepted === false) {
          resetPendingRef.current = false;
          resetSeqRef.current = null;
          setResetPending(false);
          showToast(`急停复位失败：${ackReason || "设备未确认"}`);
        }

        const messageType = typeof message.type === "string" ? message.type.toLowerCase() : "";
        const messageEvent = typeof message.event === "string" ? message.event.toLowerCase() : "";
        const isRobotStatus = ["status", "robot_status", "telemetry"].includes(messageType);
        const isTelemetryFrame = messageType === "telemetry" || messageEvent === "telemetry";
        const resetsTelemetryFreshness = messageEvent === "connected" || messageEvent === "serial_state";
        if (!isRobotStatus) {
          if (latency !== undefined) setTelemetry((current) => ({ ...current, latencyMs: latency ?? null }));
          return;
        }
        setTelemetry((current) => ({
          batteryVoltage: voltage === undefined ? current.batteryVoltage : clamp(voltage, 0, 30),
          batteryPercent: percentage === undefined
            ? voltage === undefined ? current.batteryPercent : estimateBatteryPercent(voltage)
            : Math.round(clamp(percentage, 0, 100)),
          distanceCm: distance === undefined ? current.distanceCm : distance === null ? null : Math.round(clamp(distance, 0, 1000)),
          serialConnected: uart ?? current.serialConnected,
          simulated: simulated ?? current.simulated,
          faultCode: statusFaultCode ?? current.faultCode,
          latencyMs: latency === undefined ? current.latencyMs : latency,
          packets: current.packets + (isTelemetryFrame ? 1 : 0),
          lastUpdate: isTelemetryFrame ? Date.now() : resetsTelemetryFreshness ? 0 : current.lastUpdate,
        }));
      };
      socket.onerror = () => socket.close();
      socket.onclose = () => {
        if (cancelled) return;
        wsRef.current = null;
        clearArmTicker();
        setArmProgress(0);
        resetMotion();
        if (controlStateRef.current === "estop") {
          resetPendingRef.current = false;
          resetSeqRef.current = null;
          setResetPending(false);
        } else {
          controlStateRef.current = "locked";
          setControlState("locked");
        }
        setConnection("disconnected");
        const delay = Math.min(8000, 1000 * 2 ** retryCount++);
        retryTimer = window.setTimeout(openSocket, delay);
      };
    };

    openSocket();
    return () => {
      cancelled = true;
      if (retryTimer) window.clearTimeout(retryTimer);
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        seqRef.current = (seqRef.current + 1) >>> 0;
        const seq = seqRef.current;
        wsRef.current.send(JSON.stringify({ type: "drive", linear: 0, angular: 0, seq }));
      }
      wsRef.current?.close();
      wsRef.current = null;
      pendingPackets.clear();
    };
  }, [clearArmTicker, endpoint, reconnectKey, resetMotion, showToast, transport]);

  useEffect(() => {
    const reconnectSafely = () => {
      if (transport !== "live" || !endpoint || !navigator.onLine) return;

      clearArmTicker();
      setArmProgress(0);
      sendZeroBurst();
      if (controlStateRef.current !== "estop") {
        controlStateRef.current = "locked";
        setControlState("locked");
      }

      if (wsRef.current?.readyState === WebSocket.OPEN) return;
      setReconnectKey((key) => key + 1);
      showToast("网络已恢复，正在安全重连；控制仍保持锁定");
    };

    window.addEventListener("online", reconnectSafely);
    window.addEventListener("focus", reconnectSafely);
    return () => {
      window.removeEventListener("online", reconnectSafely);
      window.removeEventListener("focus", reconnectSafely);
    };
  }, [clearArmTicker, endpoint, sendZeroBurst, showToast, transport]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (controlState === "armed" && inputActiveRef.current) sendDrive(vectorRef.current);
    }, 66);
    return () => window.clearInterval(timer);
  }, [controlState, sendDrive]);

  const telemetryFresh = transport === "demo" || clock - telemetry.lastUpdate < TELEMETRY_STALE_MS;
  const linkReady = transport === "demo" || (connection === "connected" && telemetry.serialConnected && telemetryFresh && telemetry.faultCode === 0);
  const canArm = controlState === "locked" && linkReady;

  useEffect(() => {
    linkReadyRef.current = linkReady;
  }, [linkReady]);

  useEffect(() => {
    if (controlState === "armed" && !linkReady) {
      clearArmTicker();
      // Safety transitions intentionally synchronise UI state immediately.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      sendZeroBurst();
      controlStateRef.current = "locked";
      setControlState("locked");
      showToast("控制链路异常，已归零并锁定");
    }
  }, [clearArmTicker, controlState, linkReady, sendZeroBurst, showToast]);

  useEffect(() => {
    const stopForPageState = () => {
      if (controlState === "armed") sendZeroBurst();
    };
    const onVisibility = () => {
      if (document.hidden) stopForPageState();
    };
    window.addEventListener("blur", stopForPageState);
    window.addEventListener("beforeunload", stopForPageState);
    window.addEventListener("pagehide", stopForPageState);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("blur", stopForPageState);
      window.removeEventListener("beforeunload", stopForPageState);
      window.removeEventListener("pagehide", stopForPageState);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [controlState, sendZeroBurst]);

  const applyKeyboardVector = useCallback(() => {
    const keys = keysRef.current;
    const x = (keys.has("arrowright") || keys.has("d") ? 1 : 0) - (keys.has("arrowleft") || keys.has("a") ? 1 : 0);
    const y = (keys.has("arrowdown") || keys.has("s") ? 1 : 0) - (keys.has("arrowup") || keys.has("w") ? 1 : 0);
    const next = normalizeJoystick(x, y);
    inputActiveRef.current = keys.size > 0;
    vectorRef.current = next;
    setVector(next);
    if (keys.size > 0) sendDrive(next);
  }, [sendDrive]);

  useEffect(() => {
    const driveKeys = new Set(["w", "a", "s", "d", "arrowup", "arrowdown", "arrowleft", "arrowright"]);
    const onKeyDown = (event: KeyboardEvent) => {
      if (interactiveTarget(event.target)) return;
      const key = event.key.toLowerCase();
      if (key === " " || key === "spacebar") {
        event.preventDefault();
        if (controlState === "armed") {
          sendZeroBurst();
          showToast("已发送停车指令");
        }
        return;
      }
      if (!driveKeys.has(key) || controlState !== "armed") return;
      event.preventDefault();
      if (keysRef.current.has(key)) return;
      keysRef.current.add(key);
      applyKeyboardVector();
    };
    const onKeyUp = (event: KeyboardEvent) => {
      const key = event.key.toLowerCase();
      if (!driveKeys.has(key)) return;
      event.preventDefault();
      keysRef.current.delete(key);
      if (keysRef.current.size === 0) sendZeroBurst();
      else applyKeyboardVector();
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
    };
  }, [applyKeyboardVector, controlState, sendZeroBurst, showToast]);

  const updateJoystick = useCallback((clientX: number, clientY: number, sendImmediately = false) => {
    const element = joystickRef.current;
    if (!element) return;
    const rect = element.getBoundingClientRect();
    const radius = rect.width / 2;
    const next = normalizeJoystick((clientX - (rect.left + radius)) / radius, (clientY - (rect.top + radius)) / radius);
    inputActiveRef.current = true;
    vectorRef.current = next;
    setVector(next);
    if (sendImmediately) sendDrive(next);
  }, [sendDrive]);

  const onJoystickDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (controlState !== "armed") return;
    event.preventDefault();
    pointerIdRef.current = event.pointerId;
    event.currentTarget.setPointerCapture(event.pointerId);
    updateJoystick(event.clientX, event.clientY, true);
    if (navigator.vibrate) navigator.vibrate(16);
  };

  const onJoystickMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (pointerIdRef.current !== event.pointerId || controlState !== "armed") return;
    event.preventDefault();
    updateJoystick(event.clientX, event.clientY);
  };

  const releaseJoystick = (event?: ReactPointerEvent<HTMLDivElement>) => {
    if (event && pointerIdRef.current !== event.pointerId) return;
    if (pointerIdRef.current !== null && event?.currentTarget.hasPointerCapture(pointerIdRef.current)) {
      event.currentTarget.releasePointerCapture(pointerIdRef.current);
    }
    if (inputActiveRef.current) sendZeroBurst();
  };

  const pressDirection = (next: Vector, event: ReactPointerEvent<HTMLButtonElement>) => {
    if (controlState !== "armed") return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    inputActiveRef.current = true;
    vectorRef.current = next;
    setVector(next);
    sendDrive(next);
  };

  const releaseDirection = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    sendZeroBurst();
  };

  const beginArmHold = () => {
    if (!canArm || armTickerRef.current) return;
    armStartedRef.current = performance.now();
    setArmProgress(0.02);
    armTickerRef.current = window.setInterval(() => {
      const progress = clamp((performance.now() - armStartedRef.current) / 850, 0, 1);
      if (!linkReadyRef.current || controlStateRef.current !== "locked") {
        clearArmTicker();
        setArmProgress(0);
        return;
      }
      setArmProgress(progress);
      if (progress >= 1) {
        clearArmTicker();
        suppressArmClickRef.current = true;
        window.setTimeout(() => { suppressArmClickRef.current = false; }, 500);
        controlStateRef.current = "armed";
        setControlState("armed");
        setArmProgress(0);
        showToast(transport === "demo" ? "演示控制已启用" : "控制已启用，松手即停");
        if (navigator.vibrate) navigator.vibrate(20);
      }
    }, 30);
  };

  const cancelArmHold = () => {
    if (controlState !== "armed") setArmProgress(0);
    clearArmTicker();
  };

  useEffect(() => () => clearArmTicker(), [clearArmTicker]);

  useEffect(() => {
    if (!canArm && armTickerRef.current) {
      clearArmTicker();
      setArmProgress(0);
    }
  }, [canArm, clearArmTicker]);

  const engageEstop = () => {
    if (controlState === "estop") return;
    clearArmTicker();
    setArmProgress(0);
    sendZeroBurst();
    sendPacket("estop", {});
    controlStateRef.current = "estop";
    setControlState("estop");
    resetPendingRef.current = false;
    resetSeqRef.current = null;
    setResetPending(false);
    showToast(
      transport === "demo"
        ? "演示急停已锁定"
        : connection === "connected" && wsRef.current?.readyState === WebSocket.OPEN
          ? "急停指令已发送，等待硬件执行"
          : "链路不可用，本地已锁定；依赖硬件看门狗停车",
    );
    if (navigator.vibrate) navigator.vibrate(60);
  };

  const resetEstop = () => {
    if (transport === "live" && wsRef.current?.readyState !== WebSocket.OPEN) {
      resetPendingRef.current = false;
      setResetPending(false);
      showToast("链路未连接，无法发送急停复位请求");
      return;
    }
    const resetSeq = sendPacket("clear_estop", { confirm: true });
    resetSeqRef.current = resetSeq;
    resetMotion();
    if (transport === "demo") {
      resetPendingRef.current = false;
      resetSeqRef.current = null;
      controlStateRef.current = "locked";
      setControlState("locked");
      showToast("急停已解除，控制保持锁定");
    } else {
      resetPendingRef.current = true;
      setResetPending(true);
      showToast("复位请求已发送，等待设备确认");
    }
  };

  const connectEndpoint = (candidate: string, profile: Exclude<ConnectionProfile, "demo">) => {
    if (!/^wss?:\/\//i.test(candidate)) {
      showToast("地址必须以 ws:// 或 wss:// 开头");
      return;
    }
    sendZeroBurst();
    if (controlStateRef.current !== "estop") {
      controlStateRef.current = "locked";
      setControlState("locked");
    }
    setTelemetry((current) => ({ ...current, serialConnected: false, simulated: false, latencyMs: null, packets: 0, lastUpdate: 0 }));
    window.localStorage.setItem(ENDPOINT_STORAGE_KEY, candidate);
    window.localStorage.setItem(AUTO_CONNECT_STORAGE_KEY, String(autoConnect));
    setEndpoint(candidate);
    setEndpointDraft(candidate);
    setConnectionProfile(profile);
    setConnection("connecting");
    setTransport("live");
    setReconnectKey((key) => key + 1);
    setSettingsOpen(false);
  };

  const connectLive = () => {
    const candidate = endpointDraft.trim();
    const isHotspot = candidate.replace(/\/$/, "").toLowerCase() === HOTSPOT_ENDPOINT.toLowerCase();
    const isSameOrigin = directHostDetected && candidate === deriveWebSocketUrl();
    if (isHotspot && !directHostDetected) {
      sendZeroBurst();
      showToast("正在打开小车热点控制页");
      window.location.assign(HOTSPOT_WEB_URL);
      return;
    }
    connectEndpoint(candidate, isHotspot ? "hotspot" : isSameOrigin ? "same-origin" : "manual");
  };

  const connectHotspot = () => {
    setEndpointDraft(HOTSPOT_ENDPOINT);
    if (directHostDetected) {
      connectEndpoint(deriveWebSocketUrl(), "same-origin");
      return;
    }
    sendZeroBurst();
    showToast("正在打开小车热点控制页");
    window.location.assign(HOTSPOT_WEB_URL);
  };

  const updateAutoConnectPreference = (enabled: boolean) => {
    setAutoConnect(enabled);
    window.localStorage.setItem(AUTO_CONNECT_STORAGE_KEY, String(enabled));
  };

  const useDemo = () => {
    sendZeroBurst();
    setTransport("demo");
    setConnection("demo");
    setConnectionProfile("demo");
    setTelemetry((current) => ({ ...current, distanceCm: current.distanceCm ?? 86, serialConnected: true, simulated: true, faultCode: 0, lastUpdate: Date.now() }));
    setAutoConnect(false);
    window.localStorage.setItem(AUTO_CONNECT_STORAGE_KEY, "false");
    if (controlStateRef.current !== "estop") {
      controlStateRef.current = "locked";
      setControlState("locked");
    }
    setSettingsOpen(false);
    showToast("已进入交互演示，不会发送真实指令");
  };

  const direction = useMemo(() => {
    if (Math.abs(vector.x) < 0.05 && Math.abs(vector.y) < 0.05) return "停止";
    if (Math.abs(vector.y) >= Math.abs(vector.x)) return vector.y < 0 ? "前进" : "后退";
    return vector.x < 0 ? "左转" : "右转";
  }, [vector]);

  const profileLabel = connectionProfile === "same-origin"
    ? "小车同源直连"
    : connectionProfile === "hotspot"
      ? "热点直连"
      : connectionProfile === "manual"
        ? "自定义直连"
        : "演示链路";

  const status = useMemo(() => {
    if (telemetry.faultCode !== 0) return { title: "STM32 硬件故障", copy: resetPending ? "复位请求已发送，等待 STM32 确认故障确已排除。" : `故障码 ${telemetry.faultCode} 已锁定运动；排除硬件原因后执行急停复位。`, tone: "danger" };
    if (controlState === "estop") return { title: "急停锁定", copy: resetPending ? "复位请求已发送，正在等待设备确认。" : "运动指令已归零，解除后仍需重新启用控制。", tone: "danger" };
    if (transport === "live" && connection === "disconnected") return { title: "连接中断", copy: "页面已归零并锁定，车辆应由 Orange Pi / STM32 看门狗停车。", tone: "danger" };
    if (transport === "live" && connection === "connecting") return { title: `正在${profileLabel}`, copy: "尝试建立 WebSocket 链路，恢复后仍保持锁定，不会继续上次运动。", tone: "warning" };
    if (controlState === "armed") return { title: "控制已启用", copy: "按住摇杆、方向按钮或 WASD 驾驶；松手、失焦、断连都会归零。", tone: "active" };
    if (!linkReady) return { title: "链路未就绪", copy: "需要 WebSocket、UART 与新鲜遥测同时正常，才能启用控制。", tone: "warning" };
    return {
      title: "车辆待命",
      copy: transport === "demo"
        ? "当前为交互演示，所有操作均不会发送到真实车辆。"
        : telemetry.simulated
          ? "网关处于仿真模式，不会驱动真实 STM32；可长按启用测试控制链路。"
          : `${profileLabel}正常，长按启用后才会发送非零速度。`,
      tone: "ready",
    };
  }, [connection, controlState, linkReady, profileLabel, resetPending, telemetry.faultCode, telemetry.simulated, transport]);

  const connectionText = transport === "demo"
    ? "演示链路"
    : connection === "connected"
      ? `${profileLabel}在线`
      : connection === "connecting"
        ? `${profileLabel}中`
        : `${profileLabel}中断`;
  const liveAge = Math.max(0, clock - telemetry.lastUpdate);
  const batteryWarning = telemetry.batteryPercent < 20;
  const distanceWarning = telemetry.distanceCm === null || telemetry.distanceCm < 35;
  const latencyWarning = telemetry.latencyMs !== null && telemetry.latencyMs > 200;

  const joystickStyle = { "--joy-x": `${vector.x * 86}px`, "--joy-y": `${vector.y * 86}px` } as CSSProperties;
  const armStyle = { "--arm-progress": `${armProgress * 100}%` } as CSSProperties;

  return (
    <main className={styles.shell}>
      <header className={styles.header}>
        <div className={styles.brand}>
          <span className={styles.brandMark} aria-hidden="true">R1</span>
          <div><strong>ROVER ONE</strong><small>热点本地离线遥控 · V1</small></div>
        </div>
        <div className={styles.headerStatus} aria-label="连接状态">
          <span className={`${styles.statusChip} ${transport === "demo" || connection === "connected" ? styles.good : connection === "connecting" ? styles.warn : styles.bad}`}><i /> {connectionText}</span>
          <span className={`${styles.statusChip} ${telemetry.faultCode !== 0 ? styles.bad : telemetry.serialConnected && telemetryFresh ? transport === "live" && telemetry.simulated ? styles.warn : styles.good : styles.bad}`}>
            <i /> {telemetry.faultCode !== 0 ? `STM32 故障 ${telemetry.faultCode}` : transport === "live" && telemetry.simulated ? "网关 仿真" : `UART ${telemetry.serialConnected && telemetryFresh ? "正常" : "异常"}`}
          </span>
          <button className={styles.connectionButton} type="button" onClick={() => setSettingsOpen(true)}>连接设置 <span aria-hidden="true">↗</span></button>
        </div>
      </header>

      <section className={`${styles.safetyBanner} ${styles[status.tone]}`} role="status" aria-live="polite">
        <span className={styles.bannerIcon} aria-hidden="true">{status.tone === "danger" ? "!" : status.tone === "active" ? "›" : "•"}</span>
        <div><strong>{status.title}</strong><p>{status.copy}</p></div>
        {controlState === "estop" && <button type="button" onClick={resetEstop} disabled={resetPending}>{resetPending ? "等待确认" : "解除并锁定"}</button>}
        {transport === "demo" && controlState !== "estop" && <span className={styles.demoTag}>DEMO · NO OUTPUT</span>}
        {transport === "live" && controlState !== "estop" && (
          <span className={styles.liveTag}>{profileLabel}{autoConnect || connectionProfile === "same-origin" ? " · AUTO" : ""}{telemetry.simulated ? " · SIM" : ""}</span>
        )}
      </section>

      <section className={styles.hero}>
        <div>
          <p className={styles.eyebrow}>ORANGE PI · STM32 CONTROL</p>
          <h1>小车控制台</h1>
          <p>网页只发送期望线速度与角速度，四路电机 PID 和硬件保护由 STM32 完成。</p>
        </div>
        <button className={`${styles.estop} ${controlState === "estop" ? styles.estopActive : ""}`} type="button" onClick={engageEstop}>
          <span aria-hidden="true">■</span>
          <span><strong>{controlState === "estop" ? "急停已锁定" : "紧急停车"}</strong><small>立即归零并锁定控制</small></span>
        </button>
      </section>

      <section className={styles.workspace}>
        <article className={styles.controlCard}>
          <div className={styles.cardHeading}>
            <div><span>MANUAL DRIVE</span><strong>虚拟摇杆</strong></div>
            <div className={styles.directionState}><i className={controlState === "armed" ? styles.moving : ""} />{direction}</div>
          </div>
          <div
            ref={joystickRef}
            className={`${styles.joystick} ${controlState !== "armed" ? styles.joystickLocked : ""}`}
            style={joystickStyle}
            onPointerDown={onJoystickDown}
            onPointerMove={onJoystickMove}
            onPointerUp={releaseJoystick}
            onPointerCancel={releaseJoystick}
            role="application"
            aria-label={controlState === "armed" ? "虚拟摇杆，拖动控制小车" : "虚拟摇杆已锁定"}
          >
            <span className={styles.cardinalNorth}>前</span><span className={styles.cardinalSouth}>后</span>
            <span className={styles.cardinalWest}>左</span><span className={styles.cardinalEast}>右</span>
            <div className={styles.joystickRings} /><div className={styles.joystickKnob}><span /></div>
            {controlState !== "armed" && <div className={styles.joystickLock}><span aria-hidden="true">⌁</span>{controlState === "estop" ? "急停锁定" : "尚未启用"}</div>}
          </div>

          <div className={styles.speedReadout} aria-label="目标速度">
            <span><small>目标线速度</small><strong>{lastCommand.linear.toFixed(2)} <i>m/s</i></strong></span>
            <span><small>目标角速度</small><strong>{lastCommand.angular.toFixed(2)} <i>rad/s</i></strong></span>
            <span className={styles.sequenceReadout}><small>最新序号</small><strong>#{lastCommand.seq.toString().padStart(4, "0")}</strong></span>
          </div>

          <div className={styles.controlFooter}>
            <div className={styles.speedSelector} role="group" aria-label="速度档位">
              {Object.entries(SPEED_PRESETS).map(([key, preset]) => (
                <button key={key} type="button" className={speedKey === key ? styles.selected : ""} onClick={() => { const nextKey = key as SpeedKey; sendZeroBurst(); speedRef.current = SPEED_PRESETS[nextKey]; setSpeedKey(nextKey); }} disabled={controlState === "estop"}>
                  <span>{preset.label}</span><small>{preset.hint}</small>
                </button>
              ))}
            </div>
            <button
              type="button"
              className={`${styles.armButton} ${controlState === "armed" ? styles.armed : ""}`}
              style={armStyle}
              disabled={!canArm && controlState !== "armed"}
              onPointerDown={beginArmHold}
              onPointerUp={cancelArmHold}
              onPointerLeave={cancelArmHold}
              onPointerCancel={cancelArmHold}
              onKeyDown={(event) => { if (event.key === "Enter") beginArmHold(); }}
              onKeyUp={(event) => { if (event.key === "Enter") cancelArmHold(); }}
              onClick={() => {
                if (suppressArmClickRef.current) {
                  suppressArmClickRef.current = false;
                  return;
                }
                if (controlState === "armed") {
                  sendZeroBurst();
                  controlStateRef.current = "locked";
                  setControlState("locked");
                  showToast("控制已锁定");
                }
              }}
            >
              <span className={styles.armFill} /><span className={styles.armText}>{controlState === "armed" ? "锁定控制" : canArm ? "长按启用控制" : "等待链路就绪"}</span>
            </button>
          </div>

          <div className={styles.directionPad} aria-label="方向按钮">
            <span /><button type="button" aria-label="前进" disabled={controlState !== "armed"} onPointerDown={(event) => pressDirection({ x: 0, y: -1 }, event)} onPointerUp={releaseDirection} onPointerCancel={releaseDirection}>↑</button><span />
            <button type="button" aria-label="左转" disabled={controlState !== "armed"} onPointerDown={(event) => pressDirection({ x: -1, y: 0 }, event)} onPointerUp={releaseDirection} onPointerCancel={releaseDirection}>←</button>
            <button type="button" className={styles.stopButton} aria-label="停止" disabled={controlState !== "armed"} onPointerDown={() => sendZeroBurst()}>STOP</button>
            <button type="button" aria-label="右转" disabled={controlState !== "armed"} onPointerDown={(event) => pressDirection({ x: 1, y: 0 }, event)} onPointerUp={releaseDirection} onPointerCancel={releaseDirection}>→</button>
            <span /><button type="button" aria-label="后退" disabled={controlState !== "armed"} onPointerDown={(event) => pressDirection({ x: 0, y: 1 }, event)} onPointerUp={releaseDirection} onPointerCancel={releaseDirection}>↓</button><span />
          </div>
          <p className={styles.keyboardHint}><kbd>W A S D</kbd> / <kbd>方向键</kbd> 驾驶 · <kbd>Space</kbd> 停车 · 松手即停</p>
        </article>

        <aside className={styles.telemetry}>
          <div className={styles.cardHeading}>
            <div><span>LIVE TELEMETRY</span><strong>车辆状态</strong></div>
            <small className={!telemetryFresh ? styles.stale : ""}>{telemetryFresh ? `${transport === "demo" ? "模拟" : "更新"} ${liveAge < 1000 ? "刚刚" : `${Math.round(liveAge / 1000)}s 前`}` : "遥测过期"}</small>
          </div>
          <div className={styles.metricGrid}>
            <article className={batteryWarning ? styles.metricWarning : ""}>
              <div className={styles.metricTop}><small>电池电压</small><span aria-hidden="true">BAT</span></div>
              <strong>{telemetry.batteryVoltage.toFixed(1)} <i>V</i></strong>
              <div className={styles.meter}><span style={{ width: `${telemetry.batteryPercent}%` }} /></div>
              <p>{telemetry.batteryPercent}% · {batteryWarning ? "请尽快充电" : "电量充足"}</p>
            </article>
            <article className={telemetry.distanceCm === null ? styles.metricDanger : distanceWarning ? styles.metricWarning : ""}>
              <div className={styles.metricTop}><small>前方距离</small><span aria-hidden="true">SONAR</span></div>
              <strong>{telemetry.distanceCm ?? "--"} <i>cm</i></strong>
              <div className={styles.distanceScale}><i /><i /><i /><i /><i /></div>
              <p>{telemetry.distanceCm === null ? "超声波传感器不可用" : distanceWarning ? "进入警戒距离" : "通道安全"}</p>
            </article>
            <article className={!telemetry.serialConnected || !telemetryFresh || telemetry.faultCode !== 0 ? styles.metricDanger : transport === "live" && telemetry.simulated ? styles.metricWarning : ""}>
              <div className={styles.metricTop}><small>串口链路</small><span aria-hidden="true">UART</span></div>
              <strong className={styles.textMetric}>{telemetry.faultCode !== 0 ? `故障 ${telemetry.faultCode}` : transport === "live" && telemetry.simulated ? "仿真" : telemetry.serialConnected && telemetryFresh ? "正常" : "异常"}</strong>
              <span className={styles.linkLine}><i /> {transport === "demo" ? "虚拟 STM32" : telemetry.simulated ? "Orange Pi 仿真网关" : "Orange Pi ↔ STM32"}</span>
              <p>{telemetry.faultCode !== 0 ? "排除故障后再执行复位" : transport === "live" && telemetry.simulated ? "未连接真实 STM32" : telemetry.serialConnected && telemetryFresh ? "状态帧持续接收" : "禁止发送运动指令"}</p>
            </article>
            <article className={latencyWarning ? styles.metricWarning : ""}>
              <div className={styles.metricTop}><small>控制延迟</small><span aria-hidden="true">ACK</span></div>
              <strong>{telemetry.latencyMs ?? "--"} <i>ms</i></strong>
              <div className={styles.signalBars}><i /><i /><i /><i /></div>
              <p>{latencyWarning ? "链路延迟偏高" : telemetry.latencyMs === null ? "等待回执" : "链路响应良好"}</p>
            </article>
          </div>

          <section className={styles.safetyChain}>
            <div className={styles.subheading}><div><span>FAIL-SAFE</span><strong>三级停车链路</strong></div><small>本地策略</small></div>
            <ol>
              <li><span>01</span><div><strong>浏览器控制心跳</strong><small>按住驾驶时约 15 Hz 发送速度</small></div><b>66 ms</b></li>
              <li><span>02</span><div><strong>Orange Pi 看门狗</strong><small>后端超时应发布零速度</small></div><b>200 ms</b></li>
              <li><span>03</span><div><strong>STM32 硬件停车</strong><small>串口超时归零 PWM 并关闭 STBY</small></div><b>300 ms</b></li>
            </ol>
          </section>
          <div className={styles.sessionLine}>
            <span><i className={telemetryFresh ? styles.onlineDot : styles.offlineDot} /> 接收 {telemetry.packets.toLocaleString("zh-CN")} 帧</span>
            <span>{endpoint || "同源 /ws"}</span>
          </div>
        </aside>
      </section>

      <footer className={styles.footer}><span>网页急停不能替代物理急停按钮</span><span>页面由小车本地提供 · 无需互联网</span></footer>

      {settingsOpen && (
        <div className={styles.modalBackdrop} role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) setSettingsOpen(false); }}>
          <section className={styles.modal} role="dialog" aria-modal="true" aria-labelledby="connection-title">
            <div className={styles.modalHeading}>
              <div><span>CONNECTION</span><h2 id="connection-title">连接车辆</h2><p>实车模式不会在连接失败时自动切回演示。</p></div>
              <button type="button" aria-label="关闭" onClick={() => setSettingsOpen(false)}>×</button>
            </div>
            <div className={styles.modeCards}>
              <button type="button" className={transport === "demo" ? styles.modeSelected : ""} onClick={useDemo}>
                <span className={styles.modeIcon}>D</span><div><strong>交互演示</strong><small>模拟遥测，不发送真实指令</small></div><i>{transport === "demo" ? "当前" : "进入"}</i>
              </button>
              <div className={`${styles.modeCard} ${transport === "live" ? styles.modeSelected : ""}`}>
                <span className={styles.modeIcon}>WS</span><div><strong>实车 WebSocket</strong><small>{transport === "live" ? profileLabel : "连接 Orange Pi 网页后端"}</small></div><i>{transport === "live" ? connectionText : "未连接"}</i>
              </div>
            </div>
            <section className={styles.hotspotConnect}>
              <div className={styles.hotspotCopy}>
                <span>ROVER-ONE WI-FI</span>
                <strong>连接热点后，打开本地离线控制页</strong>
                <small>浏览器不能替你切换 Wi-Fi。先连接 <code>ROVER-ONE</code>，再打开 <code>10.42.0.1</code>；网页和控制链路都在小车本地运行，无需互联网。页面会自动连接同源 <code>/ws</code>，但不会自动解锁。</small>
              </div>
              <button type="button" onClick={connectHotspot}><span aria-hidden="true">⌁</span> 打开小车控制页</button>
              <code className={styles.hotspotEndpoint}>{HOTSPOT_WEB_URL}</code>
            </section>
            <label className={styles.endpointField}>
              <span>WebSocket 地址</span>
              <div><input value={endpointDraft} onChange={(event) => setEndpointDraft(event.target.value)} placeholder="ws://orangepi.local/ws" spellCheck={false} /><button type="button" onClick={() => setEndpointDraft(deriveWebSocketUrl())}>同源</button></div>
              <small>Orange Pi 同源托管时使用 <code>ws://设备地址/ws</code>；HTTPS 页面连接局域网设备需可信 <code>wss://</code>。</small>
            </label>
            <label className={styles.autoConnectOption}>
              <input type="checkbox" checked={autoConnect} onChange={(event) => updateAutoConnectPreference(event.target.checked)} />
              <span><strong>保存自动连接偏好</strong><small>网络恢复或页面重新获得焦点时自动重连；重连只发送零速度，控制始终保持锁定。本机与公共预览页刷新后仍从演示模式开始。</small></span>
            </label>
            <div className={styles.protocolPreview}><span>发送示例</span><code>{`{"type":"drive","linear":0.20,"angular":0.45,"seq":126}`}</code></div>
            <div className={styles.modalActions}><button type="button" onClick={() => setSettingsOpen(false)}>取消</button><button type="button" className={styles.primaryAction} onClick={connectLive}>连接实车</button></div>
          </section>
        </div>
      )}

      {toast && <div className={styles.toast} role="status"><span aria-hidden="true">✓</span>{toast}</div>}
    </main>
  );
}
