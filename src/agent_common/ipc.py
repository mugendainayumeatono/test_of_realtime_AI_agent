"""
src/agent_common/ipc.py

基于 multiprocessing.Pipe 的高效双向进程间通信 (IPC) 模块。
提供面向对象的 IPCChannel 抽象，支持：
1. 双向异步 RPC 调用 (async call) 与同步阻塞调用 (call_sync)
2. 单向通知广播 (notify)
3. 异常自动序列化与跨进程传播
4. asyncio 事件循环无缝集成与多线程安全
"""

import asyncio
import logging
import threading
import uuid
from multiprocessing.connection import Connection
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("ipc")


class IPCChannel:
    """
    双向管道通信通道封装。
    支持在 asyncio 事件循环中异步调用，也支持在普通线程中同步阻塞调用。
    """

    def __init__(self, conn: Connection, name: str = "ipc_channel"):
        self.conn = conn
        self.name = name
        self._handlers: Dict[str, Callable] = {}
        self._pending_futures: Dict[str, asyncio.Future] = {}
        self._pending_events: Dict[str, Tuple[threading.Event, Dict[str, Any]]] = {}
        self._send_lock = threading.Lock()
        self._recv_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stopped = threading.Event()

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """启动后台接收线程并绑定事件循环（若有）"""
        if loop is not None:
            self._loop = loop
        elif self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None

        if self._recv_thread is not None and self._recv_thread.is_alive():
            return

        self._stopped.clear()
        self._recv_thread = threading.Thread(
            target=self._recv_loop,
            name=f"IPCRecv-{self.name}",
            daemon=True
        )
        self._recv_thread.start()
        logger.debug(f"IPCChannel [{self.name}] started.")

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """显式绑定或更新 asyncio 事件循环"""
        self._loop = loop

    def register_handler(self, action: str, handler: Callable) -> None:
        """注册动作请求或通知的处理函数 (支持 sync 或 async 函数)"""
        self._handlers[action] = handler

    def unregister_handler(self, action: str) -> None:
        """注销动作处理函数"""
        self._handlers.pop(action, None)

    def _recv_loop(self) -> None:
        """后台持续读取管道消息的监听循环"""
        while not self._stopped.is_set():
            try:
                # 使用短超时 poll 避免阻断线程退出
                if not self.conn.poll(0.05):
                    continue
                msg = self.conn.recv()
            except (EOFError, OSError, BrokenPipeError):
                break
            except Exception as e:
                if not self._stopped.is_set():
                    logger.error(f"Error reading from IPCChannel [{self.name}]: {e}")
                break

            self._dispatch_incoming_message(msg)

        # 管道断开或关闭，清空所有挂起的请求
        self._cleanup_pending()
        logger.debug(f"IPCChannel [{self.name}] receive loop terminated.")

    def _dispatch_incoming_message(self, msg: Any) -> None:
        """根据消息类型派发处理"""
        if not isinstance(msg, dict):
            logger.warning(f"Ignored non-dict message from [{self.name}]: {msg}")
            return

        msg_type = msg.get("type")
        msg_id = msg.get("id")

        if msg_type == "res":
            # 处理响应消息
            if msg_id in self._pending_futures:
                fut = self._pending_futures.pop(msg_id)
                if not fut.done():
                    if self._loop and self._loop.is_running():
                        if msg.get("status") == "success":
                            self._loop.call_soon_threadsafe(fut.set_result, msg.get("data"))
                        else:
                            err = RuntimeError(msg.get("error", "Unknown IPC error"))
                            self._loop.call_soon_threadsafe(fut.set_exception, err)
                    else:
                        if msg.get("status") == "success":
                            fut.set_result(msg.get("data"))
                        else:
                            fut.set_exception(RuntimeError(msg.get("error", "Unknown IPC error")))

            elif msg_id in self._pending_events:
                event, box = self._pending_events.pop(msg_id)
                box["response"] = msg
                event.set()

        elif msg_type == "req":
            # 处理请求消息 (需要返回 response)
            action = msg.get("action")
            params = msg.get("params", {})
            handler = self._handlers.get(action)

            if handler is None:
                err_resp = {
                    "type": "res",
                    "id": msg_id,
                    "status": "error",
                    "error": f"Action '{action}' not supported by [{self.name}]"
                }
                self._send_raw(err_resp)
                return

            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._execute_async_handler(msg_id, handler, params),
                    self._loop
                )
            else:
                threading.Thread(
                    target=self._execute_sync_handler,
                    args=(msg_id, handler, params),
                    daemon=True
                ).start()

        elif msg_type == "notify":
            # 处理单向通知消息 (无需返回 response)
            action = msg.get("action")
            params = msg.get("params", {})
            handler = self._handlers.get(action)
            if handler is not None:
                if self._loop and self._loop.is_running():
                    if asyncio.iscoroutinefunction(handler):
                        asyncio.run_coroutine_threadsafe(handler(params), self._loop)
                    else:
                        self._loop.call_soon_threadsafe(handler, params)
                else:
                    try:
                        if asyncio.iscoroutinefunction(handler):
                            asyncio.run(handler(params))
                        else:
                            handler(params)
                    except Exception as e:
                        logger.error(f"Error in notify handler '{action}' in [{self.name}]: {e}")

    async def _execute_async_handler(self, msg_id: str, handler: Callable, params: Dict[str, Any]) -> None:
        """在 asyncio 事件循环中执行 handler 并返回结果"""
        try:
            if asyncio.iscoroutinefunction(handler):
                result = await handler(params)
            else:
                result = handler(params)
            resp = {
                "type": "res",
                "id": msg_id,
                "status": "success",
                "data": result
            }
        except Exception as e:
            logger.error(f"Handler execution failed for request {msg_id} in [{self.name}]: {e}")
            resp = {
                "type": "res",
                "id": msg_id,
                "status": "error",
                "error": str(e)
            }
        self._send_raw(resp)

    def _execute_sync_handler(self, msg_id: str, handler: Callable, params: Dict[str, Any]) -> None:
        """在普通线程中执行 handler 并返回结果"""
        try:
            if asyncio.iscoroutinefunction(handler):
                result = asyncio.run(handler(params))
            else:
                result = handler(params)
            resp = {
                "type": "res",
                "id": msg_id,
                "status": "success",
                "data": result
            }
        except Exception as e:
            logger.error(f"Handler execution failed for request {msg_id} in [{self.name}]: {e}")
            resp = {
                "type": "res",
                "id": msg_id,
                "status": "error",
                "error": str(e)
            }
        self._send_raw(resp)

    def _send_raw(self, msg: Dict[str, Any]) -> bool:
        """线程安全地发送底层管道消息"""
        if self._stopped.is_set():
            return False
        try:
            with self._send_lock:
                self.conn.send(msg)
            return True
        except (EOFError, OSError, BrokenPipeError):
            return False
        except Exception as e:
            logger.error(f"Failed to send raw message in [{self.name}]: {e}")
            return False

    async def call(self, action: str, params: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Any:
        """
        异步 RPC 调用对方注册的 action，返回执行结果或抛出异常。
        """
        if self._stopped.is_set():
            raise ConnectionError(f"IPCChannel [{self.name}] is closed.")

        loop = self._loop or asyncio.get_running_loop()
        req_id = str(uuid.uuid4())
        fut = loop.create_future()
        self._pending_futures[req_id] = fut

        req_msg = {
            "type": "req",
            "id": req_id,
            "action": action,
            "params": params or {}
        }

        sent = self._send_raw(req_msg)
        if not sent:
            self._pending_futures.pop(req_id, None)
            raise ConnectionError(f"Failed to send request '{action}' over [{self.name}]. Pipe broken.")

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_futures.pop(req_id, None)
            raise TimeoutError(f"IPC call '{action}' on [{self.name}] timed out after {timeout}s.")
        except Exception:
            self._pending_futures.pop(req_id, None)
            raise

    def call_sync(self, action: str, params: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Any:
        """
        同步阻塞 RPC 调用对方注册的 action，返回执行结果或抛出异常。
        """
        if self._stopped.is_set():
            raise ConnectionError(f"IPCChannel [{self.name}] is closed.")

        req_id = str(uuid.uuid4())
        event = threading.Event()
        box: Dict[str, Any] = {}
        self._pending_events[req_id] = (event, box)

        req_msg = {
            "type": "req",
            "id": req_id,
            "action": action,
            "params": params or {}
        }

        sent = self._send_raw(req_msg)
        if not sent:
            self._pending_events.pop(req_id, None)
            raise ConnectionError(f"Failed to send request '{action}' over [{self.name}]. Pipe broken.")

        if not event.wait(timeout=timeout):
            self._pending_events.pop(req_id, None)
            raise TimeoutError(f"Sync IPC call '{action}' on [{self.name}] timed out after {timeout}s.")

        resp = box.get("response", {})
        if resp.get("status") == "success":
            return resp.get("data")
        else:
            raise RuntimeError(resp.get("error", "Unknown IPC error"))

    def notify(self, action: str, params: Optional[Dict[str, Any]] = None) -> bool:
        """发送单向通知，不等待响应"""
        msg = {
            "type": "notify",
            "action": action,
            "params": params or {}
        }
        return self._send_raw(msg)

    def _cleanup_pending(self) -> None:
        """管道异常终止时，通知所有挂起等待的 Future/Event 抛出异常"""
        for req_id, fut in list(self._pending_futures.items()):
            if not fut.done():
                if self._loop and self._loop.is_running():
                    self._loop.call_soon_threadsafe(
                        fut.set_exception,
                        ConnectionResetError(f"IPC connection [{self.name}] closed.")
                    )
                else:
                    fut.set_exception(ConnectionResetError(f"IPC connection [{self.name}] closed."))
        self._pending_futures.clear()

        for req_id, (event, box) in list(self._pending_events.items()):
            box["response"] = {
                "status": "error",
                "error": f"IPC connection [{self.name}] closed unexpectedly."
            }
            event.set()
        self._pending_events.clear()

    def close(self) -> None:
        """关闭通道并释放资源"""
        self._stopped.set()
        try:
            self.conn.close()
        except Exception:
            pass
        self._cleanup_pending()
        if self._recv_thread is not None and self._recv_thread.is_alive():
            if threading.current_thread() != self._recv_thread:
                self._recv_thread.join(timeout=0.2)
        logger.debug(f"IPCChannel [{self.name}] closed.")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
