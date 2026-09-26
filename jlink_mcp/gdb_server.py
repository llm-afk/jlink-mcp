"""J-Link GDB Server 管理."""

import subprocess
import threading
import time
from typing import Optional, Dict, Any

from .jlink_manager import jlink_manager
from .exceptions import GDBServerError, JLinkErrorCode
from .models.device import TargetInterface, GDBServerStatus
from .utils import logger, find_jlink_dll


class GDBServerManager:
    """GDB Server 管理器.

    管理 J-Link GDB Server 的启动、停止和状态查询。
    支持通过子进程启动 GDB Server，提供远程调试能力。
    """

    _instance: Optional["GDBServerManager"] = None
    _initialized: bool = False

    def __new__(cls) -> "GDBServerManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if GDBServerManager._initialized:
            return

        self._process: Optional[subprocess.Popen] = None
        self._running: bool = False
        self._host: str = "127.0.0.1"
        self._port: int = 2331
        self._device: Optional[str] = None
        self._interface: TargetInterface = TargetInterface.SWD
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._reader: Optional[threading.Thread] = None
        self._output_tail = ""
        self._owns_reservation = False

        GDBServerManager._initialized = True
        logger.debug("GDBServerManager 初始化完成")

    @property
    def is_running(self) -> bool:
        """进程退出后及时释放设备保留。"""
        with self._lock:
            if self._process is not None and self._process.poll() is not None:
                self._cleanup()
            return self._process is not None and self._running

    def start(
        self,
        host: str = "127.0.0.1",
        port: int = 2331,
        device: Optional[str] = None,
        interface: Optional[TargetInterface] = None,
        speed: int = 4000,
        jlink_path: Optional[str] = None,
        transfer_connection: bool = False,
    ) -> None:
        """启动 GDB；已有 MCP 连接时需显式交接，停止后不自动重连目标。

        host 仅支持 127.0.0.1（本机）或 0.0.0.0（远程）。
        device/interface 未指定时继承当前 MCP 连接。
        未连接 MCP 时必须指定 device，interface 默认 SWD，探针由 GDB 选择。
        transfer_connection=True 表示允许停止 RTT 并关闭当前 MCP 连接。
        """
        with self._lock:
            if self.is_running:
                raise GDBServerError(JLinkErrorCode.GDB_SERVER_ALREADY_RUNNING,
                                     f"GDB Server 已在运行（端口 {self._port}）")
            if host not in ("127.0.0.1", "0.0.0.0"):
                raise GDBServerError(JLinkErrorCode.INVALID_PARAMETER,
                                     "host 仅支持 127.0.0.1 或 0.0.0.0")
            if (isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
                    or isinstance(speed, bool) or not isinstance(speed, int) or speed <= 0):
                raise GDBServerError(JLinkErrorCode.INVALID_PARAMETER, "port 或 speed 无效")
            if interface is not None and interface not in (TargetInterface.SWD, TargetInterface.JTAG):
                raise GDBServerError(JLinkErrorCode.INVALID_PARAMETER, "interface 仅支持 SWD/JTAG")
            connected = jlink_manager.is_connected
            selected_device = device if device is not None else (jlink_manager._device_name if connected else None)
            if not selected_device or not selected_device.strip():
                raise GDBServerError(JLinkErrorCode.INVALID_PARAMETER,
                                     "未连接 MCP 时必须指定 device；已连接时可继承当前芯片名称")
            jlink_exe = self._find_jlink_gdbserver_exe(jlink_path)
            if not jlink_exe:
                raise GDBServerError(JLinkErrorCode.GDB_SERVER_START_FAILED,
                                     "未找到 JLinkGDBServer 可执行文件，请检查 jlink_path")

            try:
                connection = jlink_manager.reserve_for_gdb(transfer_connection)
                self._owns_reservation = True
                self._host, self._port = host, port
                self._device = selected_device.strip()
                self._interface = interface or connection["interface"]
                cmd = [
                    jlink_exe, "-device", self._device,
                    "-if", self._interface.value.lower(), "-speed", str(speed),
                    "-port", str(port), "-LocalhostOnly", "1" if host == "127.0.0.1" else "0",
                    "-strict", "-nogui", "-nosilent", "-timeout", "10000",
                ]
                if connection["serial_number"]:
                    cmd.extend(["-select", "USB=" + str(connection["serial_number"])])
                self._ready.clear()
                self._output_tail = ""
                logger.info(f"启动 GDB Server: {' '.join(cmd)}")
                self._process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self._reader = threading.Thread(
                    target=self._drain_output, args=(self._process.stdout,), daemon=True,
                    name="jlink-gdb-output",
                )
                self._reader.start()
                self._wait_until_ready(timeout=10.0)
                self._running = True
            except Exception as exc:
                self._cleanup()
                if isinstance(exc, GDBServerError):
                    raise
                raise GDBServerError(JLinkErrorCode.GDB_SERVER_START_FAILED, str(exc), exc) from exc

    def _drain_output(self, stream) -> None:
        """持续排空合并后的输出流，仅保留有限诊断文本。"""
        import codecs
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                chunk = stream.read1(4096)
                if not chunk:
                    break
                self._output_tail = (self._output_tail + decoder.decode(chunk))[-16384:]
                # SEGGER 的该提示表示可接受 GDB 客户端；仅 Listening on TCP/IP
                # 可能出现在目标连接完成前，不能当作就绪信号。
                if "waiting for gdb connection" in self._output_tail.lower():
                    self._ready.set()
        except (OSError, ValueError) as exc:
            logger.debug(f"GDB 输出流已关闭: {exc}")

    def _wait_until_ready(self, timeout: float) -> None:
        """仅在 GDB 报告可接受调试连接时成功，进程存活本身不足以判定。"""
        deadline = time.monotonic() + timeout
        while True:
            if self._process.poll() is not None:
                if self._reader:
                    self._reader.join(timeout=0.2)
                raise GDBServerError(JLinkErrorCode.GDB_SERVER_START_FAILED,
                                     f"GDB Server 启动失败: {self._output_tail[-4000:]}")
            if self._ready.is_set():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GDBServerError(JLinkErrorCode.OPERATION_TIMEOUT,
                                     f"等待 GDB Server 就绪超时: {self._output_tail[-4000:]}")
            self._ready.wait(min(0.05, remaining))

    def stop(self) -> None:
        """停止 GDB 并释放探针；随后需用户显式重新连接 MCP。"""
        with self._lock:
            self._cleanup()

    def _cleanup(self) -> None:
        process = self._process
        if process is not None:
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            if self._reader:
                self._reader.join(timeout=1)
            if process.stdout:
                process.stdout.close()
            self._process = None
            self._reader = None
        self._running = False
        self._ready.clear()
        self._device = None
        if self._owns_reservation:
            jlink_manager.release_gdb_reservation()
            self._owns_reservation = False

    def get_status(self) -> GDBServerStatus:
        with self._lock:
            running = self.is_running
            return GDBServerStatus(
                running=running, host=self._host if running else None,
                port=self._port if running else None, device_name=self._device if running else None,
                interface=self._interface if running else None,
            )

    def _find_jlink_gdbserver_exe(self, jlink_path: Optional[str] = None) -> Optional[str]:
        """查找 JLinkGDBServer 可执行文件（优先最新版本，同版本优先命令行版 CL）.

        与 utils.find_jlink_dll 保持一致：跨盘符收集候选，按修改时间降序
        （最新版本优先），保证 GDB Server 与 pylink 实际加载的 JLinkARM.dll
        版本一致，避免旧版（如 v6.64）不支持新芯片（如 GD32C103）。

        若显式指定 jlink_path（目录或 exe 文件），则直接使用该版本；
        否则优先复用 find_jlink_dll() 定位到的最新 DLL 所在目录，直接取其中的
        CL/GUI 版；若失败则回退到多来源扫描并按 mtime 排序。

        MCP 以 stdio 无窗口方式运行，GUI 版 JLinkGDBServer.exe 不向 stdout/stderr
        输出任何日志；命令行版 JLinkGDBServerCL.exe 输出完整日志、可无窗口运行，
        故同版本下优先选择 CL 版。

        Args:
            jlink_path: 指定 JLink 安装目录或 GDB Server 可执行文件路径（可选）

        Returns:
            可执行文件路径，如果未找到则返回 None
        """
        import os
        import subprocess

        def _pick_from(base_dir: str) -> Optional[str]:
            """在指定目录优先取 CL 版，其次 GUI 版."""
            cl = os.path.join(base_dir, "JLinkGDBServerCL.exe")
            if os.path.isfile(cl):
                return cl
            gui = os.path.join(base_dir, "JLinkGDBServer.exe")
            if os.path.isfile(gui):
                return gui
            return None

        def _version_mtime(exe_path: str) -> float:
            """返回 exe 所属 JLink 版本的修改时间.

            版本由同目录下的 JLink_x64.dll / JLinkARM.dll 的 mtime 判定，
            而不是 exe 自身的 mtime —— CL 与 GUI 两个 exe 的 mtime 可能相差
            几秒，若直接按 exe mtime 排序会导致"GUI 比 CL 新 8 秒而排前"，
            从而误选 GUI 版。同版本下 CL/GUI 的 DLL mtime 完全一致，可稳定
            区分版本。
            """
            d = os.path.dirname(exe_path)
            for dll_name in ("JLink_x64.dll", "JLinkARM.dll"):
                dll = os.path.join(d, dll_name)
                if os.path.isfile(dll):
                    return os.path.getmtime(dll)
            return os.path.getmtime(exe_path)

        # 0. 显式指定 jlink_path（目录或 exe 文件）时优先使用
        if jlink_path:
            if os.path.isfile(jlink_path):
                return jlink_path
            if os.path.isdir(jlink_path):
                exe = _pick_from(jlink_path)
                if exe:
                    return exe

        # 1. 复用 find_jlink_dll 的"按 mtime 选最新"逻辑，保证 DLL 与 GDB Server 版本一致
        try:
            dll_path = find_jlink_dll()
            if dll_path:
                exe = _pick_from(os.path.dirname(dll_path))
                if exe:
                    return exe
        except Exception:
            pass

        # 2. 回退：多来源扫描，收集所有候选后按 mtime 降序（最新优先），同版本 CL 优先
        candidates = []

        # 2.1 从 JLINK_LIB_PATH 环境变量推导
        lib_path_env = os.environ.get("JLINK_LIB_PATH")
        if lib_path_env:
            base_dir = os.path.dirname(lib_path_env) if os.path.isfile(lib_path_env) else lib_path_env
            candidates.append(os.path.join(base_dir, "JLinkGDBServerCL.exe"))
            candidates.append(os.path.join(base_dir, "JLinkGDBServer.exe"))

        # 2.2 从系统 PATH 查找
        for name in ("JLinkGDBServerCL.exe", "JLinkGDBServer.exe"):
            try:
                result = subprocess.run(
                    ["where", name],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    path = result.stdout.strip().split('\n')[0].strip()
                    if path:
                        candidates.append(path)
            except Exception:
                pass

        # 2.3 常见安装路径（覆盖 C/D/E 盘及带版本号后缀的 JLink_Vxxx 目录）
        base_dirs = [
            r"C:\Program Files\SEGGER",
            r"C:\Program Files (x86)\SEGGER",
            r"D:\Program Files\SEGGER",
            r"D:\Program Files (x86)\SEGGER",
            r"E:\Program Files\SEGGER",
            r"E:\Program Files (x86)\SEGGER",
        ]
        for base in base_dirs:
            if not os.path.isdir(base):
                continue
            try:
                for entry in os.listdir(base):
                    if entry.lower().startswith("jlink") and os.path.isdir(os.path.join(base, entry)):
                        candidates.append(os.path.join(base, entry, "JLinkGDBServerCL.exe"))
                        candidates.append(os.path.join(base, entry, "JLinkGDBServer.exe"))
            except Exception:
                pass
            candidates.append(os.path.join(base, "JLinkGDBServerCL.exe"))
            candidates.append(os.path.join(base, "JLinkGDBServer.exe"))

        # 去重、过滤存在的、按版本 mtime 降序（最新版本优先），同版本 CL 优先
        existing = []
        seen = set()
        for exe_path in candidates:
            if exe_path and exe_path not in seen and os.path.isfile(exe_path):
                seen.add(exe_path)
                existing.append(exe_path)

        if existing:
            existing.sort(
                key=lambda p: (-_version_mtime(p), 0 if p.lower().endswith("cl.exe") else 1)
            )
            return existing[0]

        return None


# 全局单例实例
gdb_server_manager = GDBServerManager()


def start_gdb_server(
    host: str = "127.0.0.1",
    port: int = 2331,
    device: Optional[str] = None,
    interface: Optional[str] = None,
    speed: int = 4000,
    jlink_path: Optional[str] = None,
    transfer_connection: bool = False,
) -> Dict[str, Any]:
    """启动 GDB Server.

    Args:
        host: 监听范围：127.0.0.1（默认）或 0.0.0.0
        port: 监听端口（默认 2331）
        device: 设备名称（None 则使用当前连接的设备）
        interface: 接口类型（SWD/JTAG，None 继承当前连接）
        speed: 接口速度（kHz，默认 4000）
        transfer_connection: 是否允许将已连接的 MCP 探针交给 GDB（默认 False）
        jlink_path: 指定 JLink 安装目录或 GDB Server 可执行文件路径
            （可选，如 D:\\Program Files\\SEGGER\\JLink_V942）；None 则自动
            选择最新版本

    Returns:
        启动结果，包含:
        - success: 是否成功
        - host: 监听地址
        - port: 监听端口
        - message: 状态信息
    """
    try:
        interface_enum = TargetInterface(interface.upper()) if interface is not None else None
        gdb_server_manager.start(host, port, device, interface_enum, speed, jlink_path, transfer_connection)

        return {
            "success": True,
            "host": host,
            "port": port,
            "message": f"GDB Server 已启动，监听 {host}:{port}"
        }
    except GDBServerError as e:
        logger.error(f"启动 GDB Server 失败: {e}")
        return {
            "success": False,
            "host": host,
            "port": port,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"启动 GDB Server 失败: {e}")
        return {
            "success": False,
            "host": host,
            "port": port,
            "error": {
                "code": JLinkErrorCode.GDB_SERVER_START_FAILED.value[0],
                "description": str(e),
                "suggestion": "请检查 JLink 软件是否正确安装并添加到 PATH"
            }
        }


def stop_gdb_server() -> Dict[str, Any]:
    """停止 GDB Server.

    Returns:
        停止结果，包含:
        - success: 是否成功
        - message: 状态信息
    """
    try:
        gdb_server_manager.stop()

        return {
            "success": True,
            "message": "GDB Server 已停止"
        }
    except Exception as e:
        logger.error(f"停止 GDB Server 失败: {e}")
        return {
            "success": False,
            "error": {
                "code": JLinkErrorCode.UNKNOWN_ERROR.value[0],
                "description": str(e),
                "suggestion": "请检查 GDB Server 状态"
            }
        }


def get_gdb_server_status() -> Dict[str, Any]:
    """获取 GDB Server 状态.

    Returns:
        GDB Server 状态，包含:
        - success: 是否成功
        - running: 是否正在运行
        - host: 监听地址
        - port: 监听端口
        - device_name: 设备名称
        - interface: 接口类型
    """
    try:
        status = gdb_server_manager.get_status()

        return {
            "success": True,
            "status": status.model_dump(),
            "message": "GDB Server 已启动" if status.running else "GDB Server 未运行"
        }
    except Exception as e:
        logger.error(f"获取 GDB Server 状态失败: {e}")
        return {
            "success": False,
            "error": {
                "code": JLinkErrorCode.UNKNOWN_ERROR.value[0],
                "description": str(e),
                "suggestion": "请检查 GDB Server 状态"
            }
        }
