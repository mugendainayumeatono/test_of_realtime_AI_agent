"""
src/agent_common/radio.py

无线电通信子系统核心模块。
面向对象的无线电广播抽象与多频道物理隔离支持：
1. RadioBase (ABC): 模拟现实无线电信道广播系统的抽象基类。
   - 多频道管理：通过频道名 (channel_name) 区分不同通信频道，频道间通信互不干扰。
   - 广播机制：任何端点发送数据后，其他加入了该频道的端点均可收到；发送端点不必接收自己发送的内容。
   - 频道生命周期接口：
     * create_channel: 创建/初始化通信频道
     * register (join_channel): 注册加入频道，以从该频道接收数据
     * leave_channel (unregister): 退出频道
     * destroy_channel: 销毁通信频道
   - 异步数据收发接口：
     * send: 异步发送数据（调用发送函数前自动记录无线电日志）
     * receive: 异步接收数据
   - 统一无线电日志接口：
     * log: 记录无线电日志，统一转发至 Master 进程归档至 logs/radio.log，
       转发至 Webview 在网页实时显示并通过 GET /api/radio_log 供 CLI 查询。
   - 纯虚函数：
     * _execute_send: 底层具体发送执行函数（纯虚函数）
     * _execute_receive: 底层具体接收执行函数（纯虚函数）
2. IPCRadio: 沿用现有 multiprocessing.Pipe 与 IPCChannel 的 IPC 派生实现。
   - 支持客户端端点模式 (Client Endpoint) 与中继枢纽模式 (Hub/Relay)。
   - 严格沿用已有的 ctrl_sim_radio, sim_pilot_radio, sim_webview_radio, master_sim_radio 管道拓扑。
3. InMemoryRadio: 纯内存广播派生实现，供单进程/测试环境使用。
"""

import asyncio
from abc import ABC, abstractmethod
from collections import deque
import inspect
import json
import logging
from multiprocessing.connection import Connection
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

try:
    from agent_common.ipc import IPCChannel
except (ImportError, ModuleNotFoundError):
    from .ipc import IPCChannel

logger = logging.getLogger("radio")

DEFAULT_RADIO_CHANNEL = "default"


class RadioBase(ABC):
    """
    模拟现实无线电广播系统的抽象基类。
    提供多频道隔离、广播机制、频道生命周期管理以及发送前无线电日志记录。
    """

    def __init__(
        self,
        endpoint_id: str = "radio_endpoint",
        log_sender: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ):
        self.endpoint_id = endpoint_id
        self._channels: Set[str] = set()
        self._joined_channels: Set[str] = set()
        self._handlers: Dict[str, List[Callable]] = {}
        self._queues: Dict[str, asyncio.Queue] = {}
        self._log_sender = log_sender
        self._lock = threading.RLock()

        # 默认初始化 default 频道
        self.create_channel(DEFAULT_RADIO_CHANNEL)

    def create_channel(self, channel_name: str, **kwargs) -> bool:
        """
        创建/初始化一个通信频道。
        输入参数：channel_name (频道名)
        """
        with self._lock:
            self._channels.add(channel_name)
            if channel_name not in self._handlers:
                self._handlers[channel_name] = []
            if channel_name not in self._queues:
                self._queues[channel_name] = asyncio.Queue()
        return True

    def register(self, channel_name: str, handler: Optional[Callable] = None) -> bool:
        """
        注册加入指定频道。调用此函数后，端点加入该频道并可以开始从中接收数据。
        输入参数：channel_name (频道名), handler (可选的回调函数，支持同步或异步)
        """
        with self._lock:
            if channel_name not in self._channels:
                self.create_channel(channel_name)
            self._joined_channels.add(channel_name)
            if handler is not None and handler not in self._handlers[channel_name]:
                self._handlers[channel_name].append(handler)
        return True

    def join_channel(self, channel_name: str, handler: Optional[Callable] = None) -> bool:
        """register 的别名"""
        return self.register(channel_name, handler)

    def leave_channel(self, channel_name: str, handler: Optional[Callable] = None) -> bool:
        """
        退出指定频道。调用后不再从该频道接收广播数据。
        """
        with self._lock:
            if channel_name in self._handlers:
                if handler is not None:
                    if handler in self._handlers[channel_name]:
                        self._handlers[channel_name].remove(handler)
                    if not self._handlers[channel_name]:
                        self._joined_channels.discard(channel_name)
                else:
                    self._handlers[channel_name].clear()
                    self._joined_channels.discard(channel_name)
            else:
                self._joined_channels.discard(channel_name)
        return True

    def unregister(self, channel_name: str, handler: Optional[Callable] = None) -> bool:
        """leave_channel 的别名"""
        return self.leave_channel(channel_name, handler)

    def destroy_channel(self, channel_name: str) -> bool:
        """
        销毁指定的通信频道。清理该频道关联的队列和处理程序。
        """
        with self._lock:
            self._channels.discard(channel_name)
            self._joined_channels.discard(channel_name)
            self._handlers.pop(channel_name, None)
            self._queues.pop(channel_name, None)
        return True

    def is_channel_active(self, channel_name: str) -> bool:
        """检查频道是否存在"""
        return channel_name in self._channels

    def is_joined(self, channel_name: str) -> bool:
        """检查当前端点是否已加入该频道"""
        return channel_name in self._joined_channels

    def log(
        self,
        text: Optional[str] = None,
        data: Optional[Any] = None,
        channel_name: Optional[str] = None,
        source: Optional[str] = None,
        msg_type: str = "voice",
        timestamp: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        记录无线电日志。
        要求：
        1. 转发到 master 进程记录到独立文件 (logs/radio.log)；
        2. 转发到 webview 在网页上显示；
        3. cli 可以通过 webview 暴露的接口获取。
        """
        payload = {
            "source": source or self.endpoint_id,
            "type": msg_type,
            "text": text,
            "data": data,
            "channel": channel_name or DEFAULT_RADIO_CHANNEL,
            "timestamp": timestamp if timestamp is not None else time.time(),
        }
        if self._log_sender:
            try:
                res = self._log_sender(payload)
                if inspect.isawaitable(res):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(res)
                    except RuntimeError:
                        asyncio.run(res)
            except Exception as e:
                logger.error(f"Error in RadioBase._log_sender: {e}")
        return payload

    async def send(
        self,
        channel_name: str,
        data: Any,
        source: Optional[str] = None,
        msg_type: str = "voice",
        **kwargs,
    ) -> Any:
        """
        在指定频道上异步发送数据。
        规范要求：必须在调用发送函数前记录无线电 log！
        """
        sender_id = source or self.endpoint_id
        if isinstance(data, dict):
            text = data.get("text")
            content_data = data.get("data")
            msg_type = data.get("type", msg_type)
            sender_id = data.get("source", sender_id)
        elif isinstance(data, str):
            text = data
            content_data = None
        else:
            text = str(data)
            content_data = data

        ts = kwargs.get("timestamp", time.time())

        # 1. 规范要求：在调用底层发送函数前，使用现有无线电 log 系统记录（使用统一时间戳）
        self.log(
            text=text,
            data=content_data,
            channel_name=channel_name,
            source=sender_id,
            msg_type=msg_type,
            timestamp=ts,
        )

        payload = {
            "channel": channel_name,
            "source": sender_id,
            "origin": self.endpoint_id,
            "type": msg_type,
            "text": text,
            "data": content_data,
            "timestamp": ts,
        }

        # 2. 调用纯虚函数执行具体底层发送
        return await self._execute_send(channel_name, payload, **kwargs)

    async def receive(self, channel_name: str, timeout: Optional[float] = None) -> Any:
        """
        在指定频道上异步接收数据。
        必须已注册加入该频道。
        """
        if channel_name not in self._joined_channels:
            raise RuntimeError(
                f"Endpoint [{self.endpoint_id}] has not registered to channel '{channel_name}'"
            )
        return await self._execute_receive(channel_name, timeout=timeout)

    def _put_to_queue(self, queue: asyncio.Queue, payload: Dict[str, Any]) -> None:
        """线程安全地向 asyncio.Queue 投递数据包"""
        try:
            loop = asyncio.get_running_loop()
            queue.put_nowait(payload)
        except RuntimeError:
            loop = getattr(self, "_loop", None)
            if loop and loop.is_running():
                loop.call_soon_threadsafe(queue.put_nowait, payload)
            else:
                try:
                    queue.put_nowait(payload)
                except Exception:
                    pass

    def _dispatch_coroutine_handler(self, coro) -> None:
        """安全调度协程回调处理程序"""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            loop = getattr(self, "_loop", None)
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(coro, loop)
            else:
                threading.Thread(target=lambda: asyncio.run(coro), daemon=True).start()

    def _dispatch_received(self, channel_name: str, payload: Dict[str, Any]) -> None:
        """
        接收到来自底层的数据包并分发：
        1. 检查发送源：发送端点不必接收自己发送出去的内容（基于 endpoint_id 和 origin/source 双重判定）。
        2. 检查订阅状态：仅加入该频道的端点接收数据。
        3. 放入对应频道的异步接收队列中，并调用已注册的处理程序。
        """
        origin = payload.get("origin")
        sender = payload.get("source")
        if origin == self.endpoint_id or sender == self.endpoint_id:
            # 发送端点不接收自己发送的内容
            return

        with self._lock:
            if channel_name not in self._joined_channels:
                # 未加入该频道，忽略
                return
            queue = self._queues.get(channel_name)
            handlers = list(self._handlers.get(channel_name, []))

        # 放入队列供 await receive() 读取
        if queue is not None:
            self._put_to_queue(queue, payload)

        # 触发注册的回调处理程序
        for h in handlers:
            try:
                if inspect.iscoroutinefunction(h):
                    self._dispatch_coroutine_handler(h(payload))
                else:
                    res = h(payload)
                    if inspect.isawaitable(res):
                        self._dispatch_coroutine_handler(res)
            except Exception as e:
                logger.error(
                    f"Error invoking radio handler on channel [{channel_name}] for endpoint [{self.endpoint_id}]: {e}"
                )

    @abstractmethod
    async def _execute_send(self, channel_name: str, payload: Dict[str, Any], **kwargs) -> Any:
        """纯虚函数：执行底层发送通信（由派生类具体实现 IPC/HTTP 等）"""
        pass

    @abstractmethod
    async def _execute_receive(self, channel_name: str, timeout: Optional[float] = None, **kwargs) -> Any:
        """纯虚函数：执行底层接收通信（由派生类具体实现 IPC/HTTP 等）"""
        pass


class InMemoryRadio(RadioBase):
    """
    纯内存无线电广播实现，适用于单元测试与无管道环境。
    """
    _bus_registry: Dict[str, List["InMemoryRadio"]] = {}
    _bus_lock = threading.Lock()

    def __init__(self, endpoint_id: str = "in_memory_radio", bus_id: str = "default_bus"):
        super().__init__(endpoint_id=endpoint_id)
        self.bus_id = bus_id
        with self._bus_lock:
            self._bus_registry.setdefault(self.bus_id, []).append(self)

    async def _execute_send(self, channel_name: str, payload: Dict[str, Any], **kwargs) -> Any:
        with self._bus_lock:
            peers = list(self._bus_registry.get(self.bus_id, []))

        for peer in peers:
            if peer is not self:
                peer._dispatch_received(channel_name, payload)

        return {"status": "success", "relayed": True}

    async def _execute_receive(self, channel_name: str, timeout: Optional[float] = None, **kwargs) -> Any:
        if channel_name not in self._queues:
            self._queues[channel_name] = asyncio.Queue()
        queue = self._queues[channel_name]
        if timeout is not None:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        return await queue.get()

    def close(self) -> None:
        with self._bus_lock:
            if self.bus_id in self._bus_registry and self in self._bus_registry[self.bus_id]:
                self._bus_registry[self.bus_id].remove(self)


class IPCRadio(RadioBase):
    """
    基于 multiprocessing.Pipe 与 IPCChannel 的 IPC 派生无线电广播类。
    
    工作模式：
    1. 客户端模式 (Client Mode):
       - 控制器 (agent_controller)、飞行员 (agent_pilot)、Web 界面 (webview) 等端点。
       - 持有单条连接到模拟器中继的无线电 IPC 管道。
       - 发送时通过管道 notify("radio_broadcast", payload)，并监听传入的广播与日志。
    2. 枢纽中继模式 (Hub / Relay Mode):
       - 模拟器 (airplane_simulator) 作为无线电中继站。
       - 持有到所有业务端点 (controller, pilot, webview, master) 的无线电管道。
       - 维护多频道成员表，负责在频道成员间广播转发，并负责将无线电日志广播给 Master 和 Webview。
    """

    def __init__(
        self,
        endpoint_id: str,
        connection: Optional[Connection] = None,
        channel: Optional[IPCChannel] = None,
        peer_connections: Optional[Dict[str, Connection]] = None,
        peer_channels: Optional[Dict[str, IPCChannel]] = None,
        is_hub: bool = False,
    ):
        self.is_hub = is_hub
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.connection = connection
        self.ipc_channel: Optional[IPCChannel] = channel
        self.peer_connections = peer_connections or {}
        self.peer_channels: Dict[str, IPCChannel] = peer_channels or {}
        self._hub_channel_members: Dict[str, Set[str]] = {}
        self._recent_logged_sigs: Set[Tuple[Any, ...]] = set()
        self._recent_sigs_order: deque = deque(maxlen=1000)

        super().__init__(endpoint_id=endpoint_id)

        # 客户端管道
        if self.connection and not self.ipc_channel:
            self.ipc_channel = IPCChannel(self.connection, name=f"{endpoint_id}:radio")

        # Hub 管道拓扑 (peer_name -> IPCChannel)
        if self.peer_connections:
            for name, conn in self.peer_connections.items():
                if conn and name not in self.peer_channels:
                    self.peer_channels[name] = IPCChannel(conn, name=f"hub<->{name}:radio")
        self._setup_ipc_handlers()

    def _setup_ipc_handlers(self) -> None:
        """注册底层 IPC 消息处理器"""
        if self.is_hub:
            # Hub 模式：监听各个 Peer 发来的广播、日志与频道生命周期请求
            for peer_name, ch in self.peer_channels.items():
                self._bind_hub_handlers(peer_name, ch)
        else:
            # Client 模式：监听从 Hub 转发来的广播消息
            if self.ipc_channel:
                self.ipc_channel.register_handler("radio_broadcast", self._ipc_on_broadcast)
                self.ipc_channel.register_handler("radio_log", self._ipc_on_log)
                self.ipc_channel.register_handler("add_radio_log", self._ipc_on_log)

    def _bind_hub_handlers(self, peer_name: str, ch: IPCChannel) -> None:
        """为 Hub 端各 peer 通道绑定处理器"""
        ch.register_handler("radio_broadcast", self._hub_on_broadcast)
        ch.register_handler("radio_log", self._hub_on_log)
        ch.register_handler("radio_create", self._hub_on_create)
        ch.register_handler("radio_register", self._hub_on_register)
        ch.register_handler("radio_leave", self._hub_on_leave)
        ch.register_handler("radio_destroy", self._hub_on_destroy)

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """启动后台 IPC 通道监听"""
        if loop is not None:
            self._loop = loop
        elif self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None

        if self.ipc_channel:
            self.ipc_channel.start(loop=self._loop)

        for ch in self.peer_channels.values():
            ch.start(loop=self._loop)

    def close(self) -> None:
        """关闭所有 IPC 通道"""
        if self.ipc_channel:
            self.ipc_channel.close()
        for ch in self.peer_channels.values():
            ch.close()

    # --- 频道生命周期重写（同步通知 Hub） ---

    def create_channel(self, channel_name: str, **kwargs) -> bool:
        res = super().create_channel(channel_name, **kwargs)
        if not self.is_hub and self.ipc_channel:
            try:
                self.ipc_channel.notify(
                    "radio_create",
                    {"endpoint": self.endpoint_id, "channel": channel_name},
                )
            except Exception as e:
                logger.debug(f"Failed to notify hub of radio_create: {e}")
        elif self.is_hub:
            self._hub_channel_members.setdefault(channel_name, set())
        return res

    def register(self, channel_name: str, handler: Optional[Callable] = None) -> bool:
        res = super().register(channel_name, handler)
        if not self.is_hub and self.ipc_channel:
            try:
                self.ipc_channel.notify(
                    "radio_register",
                    {"endpoint": self.endpoint_id, "channel": channel_name},
                )
            except Exception as e:
                logger.debug(f"Failed to notify hub of radio_register: {e}")
        elif self.is_hub:
            self._hub_channel_members.setdefault(channel_name, set()).add(self.endpoint_id)
        return res

    def leave_channel(self, channel_name: str, handler: Optional[Callable] = None) -> bool:
        res = super().leave_channel(channel_name, handler)
        if not self.is_hub and self.ipc_channel:
            if not self.is_joined(channel_name):
                try:
                    self.ipc_channel.notify(
                        "radio_leave",
                        {"endpoint": self.endpoint_id, "channel": channel_name},
                    )
                except Exception as e:
                    logger.debug(f"Failed to notify hub of radio_leave: {e}")
        elif self.is_hub:
            if channel_name in self._hub_channel_members:
                self._hub_channel_members[channel_name].discard(self.endpoint_id)
        return res

    def destroy_channel(self, channel_name: str) -> bool:
        res = super().destroy_channel(channel_name)
        if not self.is_hub and self.ipc_channel:
            try:
                self.ipc_channel.notify(
                    "radio_destroy",
                    {"endpoint": self.endpoint_id, "channel": channel_name},
                )
            except Exception as e:
                logger.debug(f"Failed to notify hub of radio_destroy: {e}")
        elif self.is_hub:
            self._hub_channel_members.pop(channel_name, None)
        return res

    # --- 日志记录与转发 ---

    def log(
        self,
        text: Optional[str] = None,
        data: Optional[Any] = None,
        channel_name: Optional[str] = None,
        source: Optional[str] = None,
        msg_type: str = "voice",
        timestamp: Optional[float] = None,
    ) -> Dict[str, Any]:
        payload = super().log(
            text=text,
            data=data,
            channel_name=channel_name,
            source=source,
            msg_type=msg_type,
            timestamp=timestamp,
        )

        if self.is_hub:
            # Hub 自身产生日志时直接转发给 master 与 webview
            self._forward_log_to_master_and_webview(payload)
        else:
            # Client 通过管道发送 radio_log 给 Hub
            if self.ipc_channel:
                try:
                    self.ipc_channel.notify("radio_log", payload)
                except Exception as e:
                    logger.debug(f"Failed to forward radio_log over IPC: {e}")

        return payload

    def _forward_log_to_master_and_webview(self, payload: Dict[str, Any]) -> None:
        """由 Hub 统一转发无线电日志至 Master 归档与 Webview 展示"""
        sig = (payload.get("origin") or payload.get("source"), payload.get("channel"), payload.get("text"), payload.get("timestamp"))
        if not self._add_log_sig_and_check(sig):
            return

        # 1. 转发到 master 进程 (radio_log)
        master_ch = self.peer_channels.get("master")
        if master_ch:
            try:
                master_ch.notify("radio_log", payload)
            except Exception as e:
                logger.error(f"Failed to relay radio log to master via IPC: {e}")

        # 2. 转发到 webview 进程 (add_radio_log)
        wv_ch = self.peer_channels.get("webview")
        if wv_ch:
            try:
                wv_ch.notify("add_radio_log", payload)
            except Exception as e:
                logger.error(f"Failed to relay radio log to webview via IPC: {e}")

    # --- 纯虚函数实现 ---

    async def _execute_send(self, channel_name: str, payload: Dict[str, Any], **kwargs) -> Any:
        """
        具体执行底层 IPC 发送：
        - Hub 模式：将广播分发给加入该频道的所有对端（除发送者外）
        - Client 模式：通过自身 ipc_channel 发送给 Hub
        """
        if self.is_hub:
            origin = payload.get("origin", self.endpoint_id)
            sender = payload.get("source", self.endpoint_id)
            relayed_count = 0
            for name, ch in self.peer_channels.items():
                if name == origin or name == sender or name in ("master",):
                    continue
                members = self._hub_channel_members.get(channel_name)
                # 若频道配置了成员列表，则必须在成员表中；若为 default 频道未配置则默认广播
                if members is not None:
                    if name not in members:
                        continue
                elif channel_name != DEFAULT_RADIO_CHANNEL:
                    continue

                try:
                    ch.notify("radio_broadcast", payload)
                    relayed_count += 1
                except Exception as e:
                    logger.error(f"Hub send failed for peer {name}: {e}")
            return {"status": "success", "relayed": True, "relayed_count": relayed_count}
        else:
            if self.ipc_channel:
                try:
                    self.ipc_channel.notify("radio_broadcast", payload)
                    return {"status": "success", "relayed": True}
                except Exception as e:
                    logger.error(f"Failed to send radio broadcast via IPC: {e}")
                    return {"status": "error", "message": str(e), "relayed": False}
            return {"status": "error", "message": "No IPC channel available", "relayed": False}

    async def _execute_receive(self, channel_name: str, timeout: Optional[float] = None, **kwargs) -> Any:
        """从对应频道的异步队列读取数据"""
        if channel_name not in self._queues:
            self._queues[channel_name] = asyncio.Queue()
        queue = self._queues[channel_name]
        if timeout is not None:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        return await queue.get()

    # --- 底层 IPC 消息处理回调 ---

    def _ipc_on_broadcast(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Client 端点收到来自 Hub 的无线电广播"""
        channel = params.get("channel", DEFAULT_RADIO_CHANNEL)
        self._dispatch_received(channel, params)
        return {"status": "success"}

    def _ipc_on_log(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """收到日志通知（通常为 Webview 进程）"""
        return {"status": "success"}

    def _add_log_sig_and_check(self, sig: Tuple[Any, ...]) -> bool:
        """检查并记录日志签名，若已存在返回 False (重复)，否则加入并返回 True"""
        if sig in self._recent_logged_sigs:
            return False
        if len(self._recent_sigs_order) >= 1000:
            oldest = self._recent_sigs_order.popleft()
            self._recent_logged_sigs.discard(oldest)
        self._recent_sigs_order.append(sig)
        self._recent_logged_sigs.add(sig)
        return True

    def _hub_on_broadcast(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Hub 中继收到来自某一端点的无线电通信数据"""
        channel = params.get("channel", DEFAULT_RADIO_CHANNEL)
        source = params.get("source", "unknown")
        origin = params.get("origin")

        # 1. 转发日志给 Master (写入 radio.log) 与 Webview (网页展示)（由 forward 内部去重）
        self._forward_log_to_master_and_webview(params)

        # 2. 向所有加入了该频道的对端转发（排除发送端点）
        relayed_count = 0
        for name, ch in self.peer_channels.items():
            if name == origin or name == source or name in ("master",):
                continue
            members = self._hub_channel_members.get(channel)
            # 若频道成员配置了则检查成员，未配置且非 default 则不转发
            if members is not None:
                if name not in members:
                    continue
            elif channel != DEFAULT_RADIO_CHANNEL:
                continue

            try:
                ch.notify("radio_broadcast", params)
                relayed_count += 1
            except Exception as e:
                logger.error(f"Hub failed to broadcast to {name}: {e}")

        # 3. 若 Hub 自身已加入该频道且非发送者，Hub 本身也接收
        if origin != self.endpoint_id and source != self.endpoint_id and channel in self._joined_channels:
            self._dispatch_received(channel, params)

        return {"status": "success", "relayed": True, "relayed_count": relayed_count}

    def _hub_on_log(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Hub 收到端点上报的独立无线电日志"""
        self._forward_log_to_master_and_webview(params)
        return {"status": "success"}

    def _hub_on_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Hub 创建/初始化频道"""
        channel = params.get("channel")
        if channel:
            self._hub_channel_members.setdefault(channel, set())
            super().create_channel(channel)
            logger.debug(f"Hub: created channel [{channel}]")
        return {"status": "success"}

    def _hub_on_register(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Hub 记录端点加入频道"""
        endpoint = params.get("endpoint")
        channel = params.get("channel")
        if endpoint and channel:
            self._hub_channel_members.setdefault(channel, set()).add(endpoint)
            logger.debug(f"Hub: endpoint [{endpoint}] joined channel [{channel}]")
        return {"status": "success"}

    def _hub_on_leave(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Hub 记录端点退出频道"""
        endpoint = params.get("endpoint")
        channel = params.get("channel")
        if endpoint and channel and channel in self._hub_channel_members:
            self._hub_channel_members[channel].discard(endpoint)
            logger.debug(f"Hub: endpoint [{endpoint}] left channel [{channel}]")
        return {"status": "success"}

    def _hub_on_destroy(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Hub 销毁频道"""
        channel = params.get("channel")
        if channel:
            self._hub_channel_members.pop(channel, None)
            self.destroy_channel(channel)
            logger.debug(f"Hub: destroyed channel [{channel}]")
        return {"status": "success"}
