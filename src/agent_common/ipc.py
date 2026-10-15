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
import multiprocessing
import os
import sys
import threading
import time
import traceback
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
        if loop is False:
            self._loop = None
        elif loop is not None:
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
                    target_loop = self._loop or getattr(fut, "_loop", None) or fut.get_loop()
                    if target_loop and target_loop.is_running():
                        if msg.get("status") == "success":
                            target_loop.call_soon_threadsafe(fut.set_result, msg.get("data"))
                        else:
                            err = RuntimeError(msg.get("error", "Unknown IPC error"))
                            target_loop.call_soon_threadsafe(fut.set_exception, err)
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
                    def _run_notify(h: Callable, p: Dict[str, Any], a: str):
                        try:
                            if asyncio.iscoroutinefunction(h):
                                asyncio.run(h(p))
                            else:
                                h(p)
                        except Exception as e:
                            logger.error(f"Error in notify handler '{a}' in [{self.name}]: {e}")
                    threading.Thread(
                        target=_run_notify,
                        args=(handler, params, action),
                        daemon=True
                    ).start()

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


def record_to_dict(record: logging.LogRecord, process_name: Optional[str] = None) -> Dict[str, Any]:
    """
    将标准 logging.LogRecord 转换为跨进程安全的字典表示。
    对异常调用栈进行格式化以确保多进程序列化传输安全，并保留自定义 extra 属性。
    """
    try:
        msg = record.getMessage()
    except Exception:
        msg = str(record.msg)

    exc_text = record.exc_text
    if record.exc_info and not exc_text:
        try:
            exc_text = "".join(traceback.format_exception(*record.exc_info))
        except Exception:
            exc_text = str(record.exc_info)

    proc_name = (
        process_name
        or getattr(record, "processName", None)
        or multiprocessing.current_process().name
    )

    levelno = record.levelno if isinstance(record.levelno, int) else logging.INFO
    levelname = record.levelname or logging.getLevelName(levelno)
    created = record.created if record.created is not None else time.time()
    msecs = getattr(record, "msecs", 0.0)

    data: Dict[str, Any] = {
        "name": record.name or "unknown",
        "msg": msg,
        "args": None,
        "levelname": levelname,
        "levelno": levelno,
        "pathname": record.pathname,
        "filename": record.filename,
        "module": record.module,
        "lineno": record.lineno,
        "funcName": record.funcName,
        "created": created,
        "msecs": msecs,
        "relativeCreated": getattr(record, "relativeCreated", 0.0),
        "thread": record.thread,
        "threadName": record.threadName,
        "processName": proc_name,
        "process": record.process,
        "exc_text": exc_text,
        "stack_info": record.stack_info,
    }

    # 保留用户附加的 extra 属性
    standard_attrs = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process", "exc_info", "exc_text",
        "stack_info", "message"
    }
    for k, v in record.__dict__.items():
        if k not in standard_attrs and k not in data:
            try:
                data[k] = v
            except Exception:
                pass

    return data


def dict_to_record(data: Dict[str, Any]) -> logging.LogRecord:
    """
    从跨进程接收到的字典还原 logging.LogRecord 实例。
    具备默认容错能力，防止不完整字段导致 handle 时抛出 TypeError。
    """
    if not isinstance(data, dict):
        raise TypeError(f"Expected dict for LogRecord data, got {type(data).__name__}")

    safe_data = dict(data)
    levelno = safe_data.get("levelno")
    if not isinstance(levelno, int):
        levelno = logging.INFO
        safe_data["levelno"] = levelno

    if not safe_data.get("levelname"):
        safe_data["levelname"] = logging.getLevelName(levelno)
    if safe_data.get("msg") is None:
        safe_data["msg"] = ""
    if not safe_data.get("name"):
        safe_data["name"] = "unknown"
    if safe_data.get("created") is None:
        safe_data["created"] = time.time()
        safe_data["msecs"] = 0.0

    record = logging.makeLogRecord(safe_data)
    proc_name = safe_data.get("processName")
    if proc_name:
        record.processName = proc_name
    elif not getattr(record, "processName", None):
        record.processName = "unknown"

    return record


class IPCLogHandler(logging.Handler):
    """
    通过 IPC 管道向 Master 进程发送日志的集中式日志处理器。
    具备本地容错与回退机制：当管道未初始化、连接中断或发送失败时，自动回退到本地控制台输出，
    确保业务进程不会因为日志通道异常而阻塞挂起。
    内置线程本地防重入机制，防止在发送异常时触发递归日志风暴。
    """

    def __init__(
        self,
        channel: Optional[IPCChannel] = None,
        process_name: Optional[str] = None,
        fallback_handler: Optional[logging.Handler] = None,
        level: int = logging.NOTSET,
    ):
        super().__init__(level=level)
        self.channel = channel
        self.process_name = process_name
        self._local = threading.local()

        if fallback_handler is None:
            self._fallback_handler = logging.StreamHandler(sys.stdout)
            self._fallback_handler.setFormatter(
                logging.Formatter('%(asctime)s - [%(processName)s] - %(levelname)s - %(message)s')
            )
            self._fallback_handler.setLevel(level)
        else:
            self._fallback_handler = fallback_handler

    def set_channel(self, channel: Optional[IPCChannel]) -> None:
        """更新 IPC 通信通道"""
        self.channel = channel

    def emit(self, record: logging.LogRecord) -> None:
        # 重入保护：若当前线程已在 emit 内部，直接返回防止无限递归
        if getattr(self._local, "in_emit", False):
            return

        self._local.in_emit = True
        try:
            # 补齐记录进程标识
            if self.process_name and (not getattr(record, "processName", None) or record.processName == "MainProcess"):
                record.processName = self.process_name

            sent = False
            channel = self.channel

            # 仅当 channel 存在且未显式关闭时尝试发送
            if channel is not None and not getattr(channel, "_stopped", threading.Event()).is_set():
                try:
                    payload = record_to_dict(record, process_name=self.process_name)
                    sent = channel.notify("log", payload)
                except Exception:
                    sent = False

            # 回退机制：若未成功通过 IPC 发送，安全降级至本地 StreamHandler
            if not sent and self._fallback_handler is not None:
                try:
                    self._fallback_handler.handle(record)
                except Exception:
                    self.handleError(record)
        finally:
            self._local.in_emit = False

    def close(self) -> None:
        super().close()
        if self._fallback_handler:
            try:
                self._fallback_handler.close()
            except Exception:
                pass


def setup_child_logging(
    channel: Optional[IPCChannel] = None,
    process_name: Optional[str] = None,
    level: Optional[int] = None,
    clean_existing_handlers: bool = True,
) -> IPCLogHandler:
    """
    配置子进程日志系统：
    1. 移除可能已存在的本地文件日志 Handler（避免子进程私自写入 logs/ 目录）；
    2. 注册 IPCLogHandler 统一将日志发往 Master 进程；
    3. 支持传入 channel=None 进行安全回退初始化；
    4. 支持通过 LOG_LEVEL 环境变量或显式 level 指定日志级别。
    """
    if level is None:
        level_str = os.environ.get("LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_str, logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    if clean_existing_handlers:
        for h in list(root_logger.handlers):
            root_logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

    ipc_handler = IPCLogHandler(
        channel=channel,
        process_name=process_name,
        level=level
    )
    root_logger.addHandler(ipc_handler)
    return ipc_handler
