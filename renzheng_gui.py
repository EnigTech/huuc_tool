# -*- coding: utf-8 -*-
"""
huuc校园网断网自动重连工具 —— 图形界面版
"""
import base64
import http.cookiejar
import json
import logging
import os
import platform
import queue
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
import urllib.request
import urllib.parse
from urllib.error import URLError, HTTPError
from datetime import datetime

try:
    import winreg                     # Windows 注册表（配置持久化）
except ImportError:                    # 非 Windows 环境
    winreg = None

# ==================== 开机自启（注册表，无需手动放置文件） ====================
AUTOSTART_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE_NAME = "huuc校园网自动重连工具"


def _autostart_command():
    """返回写入注册表 Run 项的启动命令。"""
    if getattr(sys, "frozen", False):
        # 打包为 exe：开机自启 exe 本身（无黑框）
        return '"%s"' % sys.executable
    # 源码运行：用 pythonw 静默启动本脚本，避免弹出命令行黑框
    pyw = sys.executable if sys.executable.lower().endswith("pythonw.exe") else \
        os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not pyw or not os.path.exists(pyw):
        pyw = sys.executable
    return '"%s" "%s"' % (pyw, os.path.abspath(__file__))


def is_autostart_enabled():
    """读取注册表，判断开机自启是否已开启。"""
    if winreg is None:
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_KEY)
        try:
            winreg.QueryValueEx(key, AUTOSTART_VALUE_NAME)
            return True
        finally:
            winreg.CloseKey(key)
    except OSError:
        return False


def set_autostart(enabled):
    """写入（enabled=True）或删除（enabled=False）开机自启注册表项。
    返回 (成功?, 错误信息)。"""
    if winreg is None:
        return False, "当前系统不支持注册表自启"
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_KEY)
        try:
            if enabled:
                winreg.SetValueEx(key, AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ,
                                  _autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_VALUE_NAME)
                except OSError:
                    pass        # 该项本就不存在，视为成功
        finally:
            winreg.CloseKey(key)
        return True, ""
    except Exception as e:
        return False, str(e)


# ==================== 配置持久化（注册表；已取消 key.txt） ====================
SETTINGS_REG_KEY = r"Software\RenzhengAutoReconnect"
SETTINGS_VALUE_ALLOW_BG = "AllowBackground"       # REG_DWORD：1=允许后台运行，0=不允许
SETTINGS_VALUE_START_MINIMIZED = "StartMinimized" # REG_DWORD：1=启动即最小化到托盘，0=正常显示
SETTINGS_VALUE_ACCOUNT = "Account"                # REG_SZ（轻度混淆后的字符串）
SETTINGS_VALUE_PASSWORD = "Password"              # REG_SZ（轻度混淆后的字符串）
SETTINGS_VALUE_INTERVAL = "Interval"              # REG_DWORD：检测间隔秒数

_REG_DWORD = winreg.REG_DWORD if winreg else 0
_REG_SZ = winreg.REG_SZ if winreg else 0


def _reg_write(name, value, value_type):
    """写入 HKCU\\...\\RenzhengAutoReconnect 下的一个值。返回 (成功?, 错误信息)。"""
    if winreg is None:
        return False, "当前系统不支持注册表"
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, SETTINGS_REG_KEY)
        try:
            winreg.SetValueEx(key, name, 0, value_type, value)
        finally:
            winreg.CloseKey(key)
        return True, ""
    except Exception as e:
        return False, str(e)


def _reg_read(name):
    """读取 HKCU\\...\\RenzhengAutoReconnect 下的一个值；不存在或异常返回 None。"""
    if winreg is None:
        return None
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, SETTINGS_REG_KEY)
        try:
            v, _ = winreg.QueryValueEx(key, name)
            return v
        finally:
            winreg.CloseKey(key)
    except OSError:
        return None


def _obfuscate(s):
    """轻度混淆（非加密）：XOR + Base64，避免注册表里直接肉眼可见明文。"""
    raw = bytes(ord(c) ^ 0x5A for c in s)
    return base64.b64encode(raw).decode("ascii")


def _deobfuscate(s):
    """还原 _obfuscate 的结果；失败返回空串。"""
    if not s:
        return ""
    try:
        raw = base64.b64decode(str(s).encode("ascii"))
        return "".join(chr(b ^ 0x5A) for b in raw)
    except Exception:
        return ""


def reg_load_settings():
    """从注册表一次性读取全部设置。返回 dict：
       {"account": str, "password": str, "interval": int,
        "allow_bg": bool, "start_minimized": bool}
       缺失项用默认值补齐。若 start_minimized=True，则 allow_bg 强制为 True。"""
    result = {
        "account": "",
        "password": "",
        "interval": DEFAULT_INTERVAL_SEC,
        "allow_bg": True,
        "start_minimized": False,
    }
    v = _reg_read(SETTINGS_VALUE_ACCOUNT)
    if isinstance(v, str):
        result["account"] = _deobfuscate(v)
    v = _reg_read(SETTINGS_VALUE_PASSWORD)
    if isinstance(v, str):
        result["password"] = _deobfuscate(v)
    v = _reg_read(SETTINGS_VALUE_INTERVAL)
    try:
        iv = int(v)
        if iv >= 3:
            result["interval"] = iv
    except (TypeError, ValueError):
        pass
    v = _reg_read(SETTINGS_VALUE_ALLOW_BG)
    try:
        result["allow_bg"] = bool(int(v))
    except (TypeError, ValueError):
        pass
    v = _reg_read(SETTINGS_VALUE_START_MINIMIZED)
    try:
        result["start_minimized"] = bool(int(v))
    except (TypeError, ValueError):
        pass
    # 一致性约束：勾选"默认后台运行"时，"允许后台运行"必须为 True
    if result["start_minimized"]:
        result["allow_bg"] = True
    return result


def reg_save_credentials(account, password):
    """把账号、密码写入注册表。返回 (成功?, 错误信息)。"""
    ok1, err1 = _reg_write(SETTINGS_VALUE_ACCOUNT, _obfuscate(account), _REG_SZ)
    if not ok1:
        return False, err1
    return _reg_write(SETTINGS_VALUE_PASSWORD, _obfuscate(password), _REG_SZ)


def reg_save_interval(seconds):
    """把检测间隔写入注册表。返回 (成功?, 错误信息)。"""
    return _reg_write(SETTINGS_VALUE_INTERVAL, int(seconds), _REG_DWORD)


def set_allow_background(enabled):
    """把"允许后台运行"偏好写入注册表。返回 (成功?, 错误信息)。"""
    return _reg_write(SETTINGS_VALUE_ALLOW_BG, 1 if enabled else 0, _REG_DWORD)


def get_allow_background():
    """读取"允许后台运行"偏好；键不存在时默认 True（勾选）。"""
    v = _reg_read(SETTINGS_VALUE_ALLOW_BG)
    try:
        return bool(int(v))
    except (TypeError, ValueError):
        return True


def set_start_minimized(enabled):
    """把"默认后台运行"偏好写入注册表。返回 (成功?, 错误信息)。"""
    return _reg_write(SETTINGS_VALUE_START_MINIMIZED, 1 if enabled else 0, _REG_DWORD)


def get_start_minimized():
    """读取"默认后台运行"偏好；键不存在时默认 False（不勾选）。"""
    v = _reg_read(SETTINGS_VALUE_START_MINIMIZED)
    try:
        return bool(int(v))
    except (TypeError, ValueError):
        return False


def reg_clear_settings(include_autostart=True):
    """清除本程序写入注册表的所有配置项（HKCU\\...\\RenzhengAutoReconnect 下的所有值，
    并可选清除 Run 键下的开机自启项）。
    返回 (成功?, 错误信息)。键本身保留，仅删除里面的值。"""
    if winreg is None:
        return False, "当前系统不支持注册表"
    try:
        # 1) 清空 SETTINGS_REG_KEY 下的全部值
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, SETTINGS_REG_KEY, 0,
                                 winreg.KEY_ALL_ACCESS)
        except OSError:
            key = None       # 键不存在，视为已清空
        if key is not None:
            try:
                # 反复取索引 0 的值并删除，直到没有值可删
                while True:
                    try:
                        name, _, _ = winreg.EnumValue(key, 0)
                    except OSError:
                        break
                    try:
                        winreg.DeleteValue(key, name)
                    except OSError:
                        break
            finally:
                winreg.CloseKey(key)

        # 2) 可选：清除开机自启
        if include_autostart:
            try:
                rk = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_KEY, 0,
                                    winreg.KEY_ALL_ACCESS)
                try:
                    try:
                        winreg.DeleteValue(rk, AUTOSTART_VALUE_NAME)
                    except OSError:
                        pass
                finally:
                    winreg.CloseKey(rk)
            except OSError:
                pass

        return True, ""
    except Exception as e:
        return False, str(e)


# ==================== 配置区 ====================
ACCOUNT = ""                       # 学号/账号（启动时从注册表加载）
ACCOUNT_SUFFIX = "@unicom"         # 认证账号后缀；若账号本身已含后缀则不会重复拼接
PASSWORD = ""                      # 密码（启动时从注册表加载）

LOGIN_URL = "https://netauth.huuc.edu.cn:802/eportal/portal/login"
PORTAL_URL = "https://netauth.huuc.edu.cn:802/eportal/portal/jsp/portal/tp-common-redirect.jsp"

# 多目标探测：任意一个通即判定"校园网没掉"
PING_HOSTS = [
    "www.baidu.com",
    "www.qq.com",
    "www.taobao.com",
    "aliyun.com",
]
PING_TIMEOUT_SEC = 3
DEFAULT_INTERVAL_SEC = 5           # ping 周期（界面可改）
RETRY_DELAY_SEC = 5                # 登录失败重试
AFTER_LOGIN_WAIT_SEC = 8           # 认证后等待网络恢复的秒数
HTTP_TIMEOUT_SEC = 15

# 打包为单个 exe 后 sys.frozen 为真；此时以 exe 所在目录为基准，
# 保证 auto_login.log 始终生成在 exe 旁，而不是打包临时解压目录里。
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LOG_FILE = os.path.join(BASE_DIR, "auto_login.log")

CTX = ssl._create_unverified_context()

# 界面可见（交互）与后台线程之间靠 queue 传递消息，避免跨线程直接改 tk 控件。
UI_QUEUE = queue.Queue()
# ==============================================


# ==================== 核心逻辑（与原版保持一致） ====================
def load_credentials():
    """启动时从注册表加载账号密码到全局变量。返回 (account, password)。"""
    global ACCOUNT, PASSWORD
    s = reg_load_settings()
    ACCOUNT, PASSWORD = s["account"], s["password"]
    return ACCOUNT, PASSWORD


def save_credentials(account, password):
    """把账号密码写入注册表，并同步全局变量。返回 (成功?, 错误信息)。"""
    global ACCOUNT, PASSWORD
    ACCOUNT, PASSWORD = account.strip(), password
    return reg_save_credentials(ACCOUNT, PASSWORD)


def full_account(account=None):
    acc = account if account is not None else ACCOUNT
    if acc.endswith(ACCOUNT_SUFFIX) or not ACCOUNT_SUFFIX:
        return acc
    return acc + ACCOUNT_SUFFIX


def xor_hex(s):
    """encrypt=1: 每个字符 XOR 0x1f 后转两位十六进制"""
    return "".join(format(ord(c) ^ 0x1f, "02x") for c in s)


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


def do_ping(host):
    sysname = platform.system().lower()
    is_win = sysname.startswith("win")
    count_arg = "-n" if is_win else "-c"
    timeout_arg = "-w" if is_win else "-W"
    cmd = ["ping", count_arg, "1", timeout_arg, str(PING_TIMEOUT_SEC), host]
    try:
        # Windows 下必须给子进程加 CREATE_NO_WINDOW，否则无控制台的 GUI 每次 ping 都会弹黑框
        if is_win:
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=PING_TIMEOUT_SEC + 5,
                                          creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=PING_TIMEOUT_SEC + 5)
        text = out.decode("gbk", errors="ignore") if is_win else out.decode("utf-8", errors="ignore")
        low = text.lower()
        return "ttl=" in low or ("0%" in low and "loss" in low)
    except Exception:
        return False


def check_network():
    """依次 ping 多个目标，任意一个通即认为在线。返回 (在线?, 命中的主机或None)"""
    for host in PING_HOSTS:
        if do_ping(host):
            return True, host
    return False, None


COOKIE_JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=CTX),
    urllib.request.HTTPCookieProcessor(COOKIE_JAR),
)
OPENER.addheaders = [("User-Agent", "Mozilla/5.0")]


def fetch_portal_cookie():
    if not PORTAL_URL:
        return
    try:
        OPENER.open(PORTAL_URL, timeout=HTTP_TIMEOUT_SEC).read()
    except Exception as e:
        _log_debug("预取 cookie 失败（不影响登录）: %s", e)


def parse_result(body):
    """从 jsonp 包裹的返回体里提取 (是否成功, msg)。"""
    m = re.search(r"\{.*\}", body)
    raw = m.group(0) if m else body
    msg = ""
    ret_code = None
    try:
        obj = json.loads(raw)
        msg = str(obj.get("msg", ""))
        rc = obj.get("ret_code", obj.get("result", None))
        if rc is not None:
            ret_code = int(rc)
    except Exception:
        mm = re.search(r'"msg"\s*:\s*"([^"]*)"', body)
        if mm:
            msg = mm.group(1)

    ok_kw = ["成功", "已登录", "认证成功", "登录成功", "AC999"]   # AC999 = 认证成功/已在线类提示
    if any(k in msg for k in ok_kw):
        return True, msg
    fail_kw = ["不存在", "错误", "失败", "密码", "超时", "在线", "绑定", "已用"]
    if any(k in msg for k in fail_kw):
        return False, msg
    if ret_code is not None:
        return ret_code in (0, 2), msg      # 0/2 都当成功兜底
    return False, msg


def do_login(account=None, password=None):
    """执行一次校园网认证。成功返回 True，失败返回 False。"""
    fetch_portal_cookie()
    acc = account if account is not None else ACCOUNT
    pwd = password if password is not None else PASSWORD
    params = {
        "callback": "7b6d2e2f2f2c",
        "login_method": "2e",
        "user_account": xor_hex(full_account(acc)),
        "user_password": xor_hex(pwd),
        "wlan_user_ip": xor_hex(get_local_ip()) or "",
        "wlan_user_ipv6": "",
        "wlan_user_mac": "",
        "wlan_ac_ip": "",
        "wlan_ac_name": "",
        "jsVersion": "2b312d312e",
        "captcha": "",
        "terminal_type": "2e",
        "lang": "zh",
        "encrypt": "1",
        "v": str(int(time.time() * 1000) % 100000),
    }
    url = LOGIN_URL + "?" + urllib.parse.urlencode(params)
    try:
        with OPENER.open(url, timeout=HTTP_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", errors="ignore")[:600]
        ok, msg = parse_result(body)
        _log_debug("认证返回: %s", body.strip())
        _log_debug("解析结果: 成功=%s, msg=%s", ok, msg)
        return ok
    except HTTPError as e:
        _log_debug("认证返回 HTTP %s", e.code)
        return False
    except URLError as e:
        _log_debug("认证请求失败: %s", e.reason)
        return False
    except Exception as e:
        _log_debug("认证异常: %s", e)
        return False


def _log_debug(fmt, *args):
    """更新到界面日志区；这里统一走接口，避免与 GUI 接口耦合。"""
    try:
        UI_QUEUE.put(("log", "[DBG] " + (fmt % args if args else fmt)))
    except Exception:
        pass


# ==================== 日志落地（文件 + 界面队列） ====================
def init_file_logger():
    logger = logging.getLogger("renzheng_gui")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


LOGGER = init_file_logger()


def emit(level, msg):
    """统一日志入口：写文件 + 推送到界面。level: info/warn/error。"""
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    if level == "error":
        LOGGER.error(msg)
        UI_QUEUE.put(("log", line, "red"))
    elif level == "warn":
        LOGGER.warning(msg)
        UI_QUEUE.put(("log", line, "orange"))
    else:
        LOGGER.info(msg)
        UI_QUEUE.put(("log", line, ""))


# ==================== 后台监控线程 ====================
class MonitorThread(threading.Thread):
    """独立线程执行"探测 -> 断网则认证 -> 恢复"的循环，通过 UI_QUEUE 回报状态。"""

    def __init__(self, interval, account, password):
        super().__init__(daemon=True)
        self.interval = max(3, int(interval))
        self.account = account
        self.password = password
        self._stop_evt = threading.Event()
        self.online = False

    def stop(self):
        self._stop_evt.set()

    def _wait(self, seconds):
        """等待 seconds 秒；期间被 stop 则立即返回 True。"""
        return self._stop_evt.wait(seconds)

    def run(self):
        emit("info", "已启动监控，检测间隔 %d 秒。账号后缀 %s" % (self.interval, ACCOUNT_SUFFIX))
        # 初始探测
        ok, hit = check_network()
        self.online = ok
        UI_QUEUE.put(("status", ok, hit))
        emit("info", "初始状态：%s（命中 %s）" % ("在线" if ok else "离线", hit or "-"))

        while not self._stop_evt.is_set():
            ok, hit = check_network()

            if ok:
                if not self.online:
                    self.online = True
                    emit("info", "网络已恢复（命中 %s）" % hit)
                UI_QUEUE.put(("status", True, hit))
                if self._wait(self.interval):
                    break
                continue

            # 断网分支
            self.online = False
            UI_QUEUE.put(("status", False, None))
            emit("warn", "全部探测目标不通，判定断网，开始认证...")

            if do_login(self.account, self.password):
                emit("info", "认证请求已发送，等待网络恢复...")
                if self._wait(AFTER_LOGIN_WAIT_SEC):
                    break
                ok2, hit2 = check_network()
                if ok2:
                    self.online = True
                    emit("info", "登录后网络已恢复（命中 %s）" % hit2)
                    UI_QUEUE.put(("status", True, hit2))
                else:
                    emit("warn", "已认证但网络未恢复，%d 秒后重试" % RETRY_DELAY_SEC)
                    if self._wait(RETRY_DELAY_SEC):
                        break
            else:
                emit("error", "认证失败，%d 秒后重试" % RETRY_DELAY_SEC)
                if self._wait(RETRY_DELAY_SEC):
                    break

        emit("info", "监控已停止。")


# ==================== Windows 系统托盘图标 + 应用图标（零依赖，ctypes 调用 Win32 API） ====================
if sys.platform.startswith("win"):
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _shell32 = ctypes.windll.shell32

    # ---- 结构体 ----
    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", GUID),
            ("hBalloonIcon", wintypes.HICON),
        ]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HCURSOR),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    # ---- 常量 ----
    NIM_ADD = 0
    NIM_DELETE = 2
    NIF_MESSAGE = 1
    NIF_ICON = 2
    NIF_TIP = 4
    WM_APP = 0x8000
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_QUIT = 0x0012
    IDI_APPLICATION = 32512
    MF_STRING = 0
    MF_SEPARATOR = 0x0800
    TPM_RETURNCMD = 0x0100
    TPM_NONOTIFY = 0x0080

    # ---- 图标相关常量 ----
    IMAGE_ICON = 1               # LoadImageW 的 type 参数
    LR_LOADFROMFILE = 0x00000010 # LoadImageW：从文件加载
    LR_DEFAULTSIZE = 0x00000040  # LoadImageW：按系统默认尺寸（16 或 32）加载
    WM_SETICON = 0x0080          # SendMessage：给窗口设置图标
    ICON_SMALL = 0               # 小图标（标题栏）
    ICON_BIG = 1                 # 大图标（任务栏 / Alt+Tab）

    # ---- WNDPROC 回调类型 ----
    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    # ---- 关键 API 类型声明（防 64 位指针截断） ----
    _user32.DefWindowProcW.restype = ctypes.c_ssize_t
    _user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    _user32.LoadIconW.restype = wintypes.HICON
    _user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    _shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    _user32.GetCursorPos.restype = wintypes.BOOL
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _user32.TrackPopupMenu.restype = wintypes.BOOL
    _user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.CreatePopupMenu.restype = wintypes.HMENU
    _user32.AppendMenuW.restype = wintypes.BOOL
    _user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_void_p, wintypes.LPCWSTR]
    _user32.DestroyMenu.restype = wintypes.BOOL
    _user32.DestroyMenu.argtypes = [wintypes.HMENU]
    _user32.DestroyWindow.restype = wintypes.BOOL
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.PostThreadMessageW.restype = wintypes.BOOL
    _user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.RegisterClassW.restype = ctypes.c_ushort
    _user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    _user32.TranslateMessage.restype = wintypes.BOOL
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.DispatchMessageW.restype = ctypes.c_ssize_t
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]

    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    _kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

    # ---- LoadImageW：从磁盘文件加载图标 ----
    _user32.LoadImageW.restype = wintypes.HANDLE
    _user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    # ---- GetParent / SendMessageW：给 tkinter 顶层窗口挂图标 ----
    _user32.GetParent.restype = wintypes.HWND
    _user32.GetParent.argtypes = [wintypes.HWND]
    _user32.SendMessageW.restype = ctypes.c_ssize_t
    _user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]

    # ---- 应用图标加载：exe 资源 → 磁盘 app.ico → 系统默认 ----
    def _load_app_hicon():
        """返回应用 HICON。优先级：
           1) 已打包为 exe：从 exe 资源段读取（PyInstaller 把 icon= 指定的图标作为资源 ID 1 嵌入）；
           2) 未打包或资源读取失败：从 BASE_DIR/app.ico 文件读取；
           3) 都失败：回退到 Windows 系统默认应用图标。"""
        # 1) 从 exe 资源读取（exe 图标资源 ID 通常为 1）
        if getattr(sys, "frozen", False):
            h = _user32.LoadIconW(_kernel32.GetModuleHandleW(None),
                                  ctypes.cast(1, ctypes.c_void_p))
            if h:
                return h

        # 2) 从磁盘 ico 文件读取（源码运行时 / exe 旁手动放置 app.ico 时也走这里）
        ico_path = os.path.join(BASE_DIR, "app.ico")
        if os.path.exists(ico_path):
            h = _user32.LoadImageW(None, ico_path, IMAGE_ICON, 0, 0,
                                   LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                return h

        # 3) 兜底：系统默认应用图标
        return _user32.LoadIconW(None, ctypes.cast(IDI_APPLICATION, ctypes.c_void_p))

    def set_window_icon(root):
        """给 tkinter 顶层窗口设置自定义图标（标题栏 + 任务栏）。
        通过 SendMessage WM_SETICON 实现，从 exe 资源或磁盘 ico 加载。"""
        try:
            hicon = _load_app_hicon()
            if not hicon:
                return
            # 确保窗口 HWND 已建立
            root.update_idletasks()
            hwnd = root.winfo_id()
            # tkinter 的 winfo_id 有时返回子窗口句柄，取父窗口即真正的顶层
            parent = _user32.GetParent(hwnd)
            target = parent if parent else hwnd
            _user32.SendMessageW(target, WM_SETICON, ICON_SMALL, hicon)
            _user32.SendMessageW(target, WM_SETICON, ICON_BIG, hicon)
        except Exception:
            pass


    class TrayIcon:
        """Windows 系统托盘图标。左键单击=打开窗口，右键弹出菜单（打开/退出）。
        独立 daemon 线程运行消息循环，不阻塞 tkinter 主循环。"""

        def __init__(self, on_open, on_exit, tooltip="huuc校园网自动重连工具"):
            self._on_open = on_open          # callable, 托盘线程调用
            self._on_exit = on_exit          # callable, 托盘线程调用
            self._tooltip = tooltip
            self._thread_id = None
            self._hwnd = None
            self._hicon = None
            self._nid = None
            self._wndproc_cb = None
            self._class_name = "RenzhengTray_%d" % id(self)
            self._WM_TRAY = WM_APP + 1
            self._ID_OPEN = 1001
            self._ID_EXIT = 1002
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._ready = threading.Event()   # 托盘就绪信号

        def start(self):
            self._thread.start()

        def shutdown(self):
            """向托盘线程发送 WM_QUIT 并等待结束，随后图标自动清除。"""
            if self._thread_id is not None:
                _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            if self._thread.is_alive():
                self._thread.join(timeout=2.0)

        def wait_ready(self, timeout=1.0):
            """阻塞当前线程等待托盘图标创建完成，返回是否就绪。"""
            return self._ready.wait(timeout)

        # ---- 内部 ----
        def _run(self):
            self._thread_id = _kernel32.GetCurrentThreadId()

            # 加载应用图标（exe 资源 → 磁盘 ico → 系统默认，三级回退）
            self._hicon = _load_app_hicon()
            if not self._hicon:
                return

            # 注册隐藏窗口类
            wc = WNDCLASSW()
            wc.style = 0
            wc.cbClsExtra = 0
            wc.cbWndExtra = 0
            wc.hInstance = _kernel32.GetModuleHandleW(None)
            wc.hIcon = self._hicon
            wc.hCursor = None
            wc.hbrBackground = None
            wc.lpszMenuName = None
            wc.lpszClassName = self._class_name

            self._wndproc_cb = WNDPROC(self._wndproc)
            wc.lpfnWndProc = ctypes.cast(self._wndproc_cb, ctypes.c_void_p)

            if _user32.RegisterClassW(ctypes.byref(wc)) == 0:
                return

            self._hwnd = _user32.CreateWindowExW(
                0, self._class_name, None, 0,
                0, 0, 0, 0, None, None, wc.hInstance, None,
            )
            if not self._hwnd:
                return

            # 添加托盘图标
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self._hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = self._WM_TRAY
            nid.hIcon = self._hicon
            nid.szTip = self._tooltip
            _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
            self._nid = nid
            self._ready.set()  # 通知主线程：托盘已就绪

            # 消息循环
            msg = wintypes.MSG()
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))

            # 收到 WM_QUIT 后清理
            if self._nid is not None:
                _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            if self._hwnd is not None:
                _user32.DestroyWindow(self._hwnd)
            # 系统共享图标无需 DestroyIcon

        def _wndproc(self, hwnd, msg, wparam, lparam):
            if msg == self._WM_TRAY:
                if lparam == WM_LBUTTONUP or lparam == WM_LBUTTONDBLCLK:
                    self._safe_call(self._on_open)
                elif lparam == WM_RBUTTONUP:
                    self._show_menu(hwnd)
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        def _show_menu(self, hwnd):
            menu = _user32.CreatePopupMenu()
            _user32.AppendMenuW(menu, MF_STRING, self._ID_OPEN, "打开")
            _user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            _user32.AppendMenuW(menu, MF_STRING, self._ID_EXIT, "退出")
            pt = wintypes.POINT()
            _user32.GetCursorPos(ctypes.byref(pt))
            _user32.SetForegroundWindow(hwnd)
            cmd = _user32.TrackPopupMenu(
                menu, TPM_RETURNCMD | TPM_NONOTIFY, pt.x, pt.y, 0, hwnd, None
            )
            _user32.DestroyMenu(menu)
            if cmd == self._ID_OPEN:
                self._safe_call(self._on_open)
            elif cmd == self._ID_EXIT:
                self._safe_call(self._on_exit)

        @staticmethod
        def _safe_call(fn):
            """安全调用回调（托盘线程中），吞掉异常避免托盘线程崩溃。"""
            try:
                fn()
            except Exception:
                pass

else:
    # 非 Windows：降级为无托盘模式 + 空实现窗口图标设置
    class TrayIcon:
        def __init__(self, *args, **kwargs):
            pass
        def start(self):
            pass
        def shutdown(self):
            pass
        def wait_ready(self, timeout=1.0):
            return False

    def set_window_icon(root):
        pass


# ==================== 图形界面 ====================
class App:
    def __init__(self, root):
        self.root = root
        self.root.title("huuc校园网自动重连工具")
        self.root.geometry("760x560")
        self.root.minsize(640, 480)
        self.monitor = None
        self.tray = None
        self._quitting = False

        # 启动时一次性从注册表读取全部配置
        settings = reg_load_settings()

        self.acc_var = tk.StringVar(value=settings["account"])
        self.pwd_var = tk.StringVar(value=settings["password"])
        self.interval_var = tk.StringVar(value=str(settings["interval"]))
        self.status_var = tk.StringVar(value="未启动")
        self.hit_var = tk.StringVar(value="-")
        # 开机自启勾选框：初始值同步当前注册表状态，保证重启程序后勾选状态正确
        self.autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        # 允许后台运行：状态从注册表读取（未写过则默认 True）
        self.allow_bg_var = tk.BooleanVar(value=settings["allow_bg"])
        # 默认后台运行：启动时自动最小化到托盘，状态从注册表读取（未写过则默认 False）
        self.start_minimized_var = tk.BooleanVar(value=settings["start_minimized"])

        self._build_ui()
        # 应用"默认后台运行"对"允许后台运行"的联动约束（强制勾选 + 置灰）
        self._sync_allow_bg_ui()
        # 设置主窗口图标（标题栏 + 任务栏），从 exe 资源或磁盘 app.ico 读取
        self._apply_window_icon()
        self._setup_tray()

        # 轮询 UI_QUEUE，把后台消息刷到界面（主线程执行，线程安全）
        self.root.after(120, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 若勾选了"默认后台运行"且有可用托盘，则启动后（含开机自启）直接最小化到托盘，
        # 不显示主窗口。用 after 延迟到主循环开始前完成隐藏，避免窗口一闪而过。
        if self.start_minimized_var.get() and self.tray is not None:
            self.root.after(0, self._startup_minimize)

        # 启动时若检测到注册表里已存在有效账密，则自动开始监控（无论窗口是否显示）。
        # 放在 after(0) 里，等主循环真正起来后再启动线程，保证 UI 日志能正常刷新。
        self.root.after(0, self._auto_start_monitor)

    def _apply_window_icon(self):
        """给主窗口设置自定义图标：优先 Win32 WM_SETICON（可从 exe 资源读取），
        并额外尝试 tkinter 的 iconbitmap（对源码运行 + 同目录 app.ico 最直接）。"""
        # 1) tkinter 原生方式（只在源码运行时有效——打包后 app.ico 不在 exe 旁）
        ico_path = os.path.join(BASE_DIR, "app.ico")
        if os.path.exists(ico_path):
            try:
                self.root.iconbitmap(ico_path)
            except Exception:
                pass
        # 2) Win32 方式（打包后从 exe 资源段读取，效果最可靠）
        set_window_icon(self.root)

    # ---------- 界面构建 ----------
    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("vista" if "vista" in style.theme_names() else "default")

        pad = {"padx": 8, "pady": 6}
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        # --- 配置区 ---
        cfg = ttk.LabelFrame(main, text="登录配置（保存在 Windows 注册表）", padding=8)
        cfg.pack(fill="x", **pad)

        ttk.Label(cfg, text="账号:").grid(row=0, column=0, sticky="w")
        ttk.Entry(cfg, textvariable=self.acc_var, width=28).grid(row=0, column=1, sticky="we", padx=4)

        ttk.Label(cfg, text="密码:").grid(row=1, column=0, sticky="w")
        ent = ttk.Entry(cfg, textvariable=self.pwd_var, width=28, show="*")
        ent.grid(row=1, column=1, sticky="we", padx=4)
        self.pwd_entry = ent
        self.show_pwd = tk.BooleanVar(value=False)
        ttk.Checkbutton(cfg, text="显示密码", variable=self.show_pwd,
                        command=self._toggle_pwd).grid(row=1, column=2, sticky="w")

        ttk.Label(cfg, text="检测间隔(秒):").grid(row=2, column=0, sticky="w")
        ttk.Entry(cfg, textvariable=self.interval_var, width=8).grid(row=2, column=1, sticky="w", padx=4)
        ttk.Label(cfg, text="（账号后缀：%s）" % ACCOUNT_SUFFIX).grid(row=2, column=2, sticky="w")

        # 第一行按钮：保存 / 测试 / 清除注册表
        btns = ttk.Frame(cfg)
        btns.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(btns, text="保存配置", command=self._save).pack(side="left", padx=4)
        ttk.Button(btns, text="测试连接", command=self._test).pack(side="left", padx=4)
        ttk.Button(btns, text="清除注册表", command=self._clear_registry).pack(side="left", padx=4)

        # 第二行：三个勾选框
        chks = ttk.Frame(cfg)
        chks.grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Checkbutton(chks, text="开机自启", variable=self.autostart_var,
                        command=self._on_autostart_toggle).pack(side="left", padx=4)
        # 保留"允许后台运行"复选框的引用，用于按"默认后台运行"状态启用/禁用
        self.allow_bg_chk = ttk.Checkbutton(chks, text="允许后台运行",
                                            variable=self.allow_bg_var,
                                            command=self._on_allow_bg_toggle)
        self.allow_bg_chk.pack(side="left", padx=4)
        ttk.Checkbutton(chks, text="默认后台运行", variable=self.start_minimized_var,
                        command=self._on_start_minimized_toggle).pack(side="left", padx=4)

        cfg.columnconfigure(1, weight=1)

        # --- 状态区 ---
        status = ttk.LabelFrame(main, text="运行状态", padding=8)
        status.pack(fill="x", **pad)

        self.indicator = tk.Canvas(status, width=16, height=16, highlightthickness=0)
        self.dot = self.indicator.create_oval(2, 2, 14, 14, fill="#9e9e9e", outline="")
        self.indicator.pack(side="left", padx=(0, 6))

        ttk.Label(status, textvariable=self.status_var,
                  font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        ttk.Label(status, textvariable=self.hit_var).pack(side="left", padx=(10, 0))

        big = ttk.Frame(status)
        big.pack(side="right")
        self.start_btn = ttk.Button(big, text="开始监控", command=self._start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(big, text="停止监控", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        # --- 日志区 ---
        logbox = ttk.LabelFrame(main, text="运行日志", padding=4)
        logbox.pack(fill="both", expand=True, **pad)

        self.log_text = tk.Text(logbox, height=10, state="disabled", wrap="word",
                                font=("Consolas", 9))
        sb = ttk.Scrollbar(logbox, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

        # 标签着色
        self.log_text.tag_configure("red", foreground="#c62828")
        self.log_text.tag_configure("orange", foreground="#ef6c00")

    # ---------- 交互 ----------
    def _toggle_pwd(self):
        self.pwd_entry.configure(show="" if self.show_pwd.get() else "*")

    def _save(self):
        acc = self.acc_var.get().strip()
        pwd = self.pwd_var.get()
        if not acc:
            messagebox.showwarning("提示", "请输入账号。")
            return
        interval = self._validate_interval()
        if interval is None:
            return
        ok1, err1 = save_credentials(acc, pwd)
        ok2, err2 = reg_save_interval(interval)
        if not ok1 or not ok2:
            err = err1 or err2
            self._log("保存到注册表失败：%s" % err, "red")
            messagebox.showerror("提示", "保存到注册表失败：%s" % err)
            return
        self._log("配置已保存到注册表。", "")
        messagebox.showinfo("提示", "配置已保存到注册表。")

    def _validate_interval(self):
        try:
            return max(3, int(self.interval_var.get()))
        except Exception:
            messagebox.showwarning("提示", "检测间隔需为整数秒（至少 3）。")
            return None

    def _test(self):
        acc = self.acc_var.get().strip()
        pwd = self.pwd_var.get()
        if not acc or not pwd:
            messagebox.showwarning("提示", "请先填写账号和密码。")
            return
        self._log("正在测试登录（账号 %s）..." % full_account(acc), "")

        def worker():
            ok = do_login(acc, pwd)
            UI_QUEUE.put(("result", ok))

        threading.Thread(target=worker, daemon=True).start()

    def _start(self):
        """用户点击"开始监控"按钮。"""
        self._start_monitor(manual=True)

    def _start_monitor(self, manual=True):
        """启动监控线程。

        manual=True  ：用户点击"开始监控"触发——校验间隔、弹窗提示，并把账密写回注册表；
        manual=False ：程序启动时自动触发——仅在注册表已存在有效账密时由调用方进入，
                       不弹窗、不重写注册表，保持静默。
        """
        if manual:
            interval = self._validate_interval()
            if interval is None:
                return
        else:
            # 自动启动模式：间隔读自注册表，理论上必然合法，非法时退回默认值
            try:
                interval = max(3, int(self.interval_var.get()))
            except Exception:
                interval = DEFAULT_INTERVAL_SEC

        acc = self.acc_var.get().strip()
        pwd = self.pwd_var.get()
        if not acc or not pwd:
            if manual:
                messagebox.showwarning("提示", "请先填写账号和密码并保存。")
            return

        if manual:
            # 点"开始监控"时顺带把当前账密与间隔写入注册表（等价于点一次"保存配置"）
            save_credentials(acc, pwd)
            reg_save_interval(interval)

        if self.monitor and self.monitor.is_alive():
            return
        self.monitor = MonitorThread(interval, acc, pwd)
        self.monitor.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._set_status("正在检测...", None)

    def _auto_start_monitor(self):
        """程序启动时：若注册表中已保存有效账号密码，则自动开始监控，无需手动点击。"""
        if self.monitor and self.monitor.is_alive():
            return
        acc = self.acc_var.get().strip()
        pwd = self.pwd_var.get()
        if not acc or not pwd:
            self._log("未检测到已保存的账号密码，未自动启动监控。"
                      "填写并保存后，点“开始监控”即可。", "orange")
            return
        self._log("检测到已保存的账号密码，自动开始监控。", "")
        self._start_monitor(manual=False)

    def _stop(self):
        if self.monitor:
            self.monitor.stop()
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self._set_status("已停止", None)

    def _on_autostart_toggle(self):
        checkbox_state = self.autostart_var.get()
        ok, err = set_autostart(checkbox_state)
        if ok:
            self._log("已%s开机自启（已写入注册表，无需手动放置文件）。" %
                      ("开启" if checkbox_state else "关闭"),
                      "" if checkbox_state else "orange")
        else:
            self._log("设置开机自启失败：%s" % err, "red")
            self.autostart_var.set(not checkbox_state)     # 回滚勾选状态
            messagebox.showerror("提示", "设置开机自启失败：%s" % err)

    def _on_allow_bg_toggle(self):
        """勾选状态变化时立即写入注册表持久化。"""
        enabled = self.allow_bg_var.get()
        ok, err = set_allow_background(enabled)
        if ok:
            self._log("已%s后台运行：点关闭按钮将%s。" %
                      ("允许" if enabled else "禁止",
                       "最小化到系统托盘继续监控" if enabled else "直接退出程序"),
                      "" if enabled else "orange")
        else:
            self._log("保存后台运行偏好失败：%s" % err, "red")

    def _on_start_minimized_toggle(self):
        """勾选状态变化时立即写入注册表持久化。
        勾选时强制"允许后台运行"为勾选并置灰；取消勾选时恢复可编辑。"""
        enabled = self.start_minimized_var.get()
        ok, err = set_start_minimized(enabled)
        if not ok:
            self._log("保存“默认后台运行”偏好失败：%s" % err, "red")
            return
        if enabled:
            # 强制允许后台运行（"启动即到托盘"必须以"允许后台运行"为前提）
            if not self.allow_bg_var.get():
                self.allow_bg_var.set(True)
                set_allow_background(True)
                self._log("已联动开启“允许后台运行”（勾选“默认后台运行”时强制允许）。", "")
        self._sync_allow_bg_ui()
        self._log("已%s“默认后台运行”：下次启动程序（含开机自启）将%s。" %
                  ("开启" if enabled else "关闭",
                   "自动最小化到系统托盘" if enabled else "正常显示主窗口"),
                  "" if enabled else "orange")

    def _sync_allow_bg_ui(self):
        """按"默认后台运行"状态同步"允许后台运行"的勾选与可用性：
           - 勾选"默认后台运行" -> 强制"允许后台运行"为勾选，并置灰不可改；
           - 取消勾选 -> 恢复"允许后台运行"可编辑。"""
        if self.start_minimized_var.get():
            if not self.allow_bg_var.get():
                self.allow_bg_var.set(True)
                set_allow_background(True)
            self.allow_bg_chk.configure(state="disabled")
        else:
            self.allow_bg_chk.configure(state="normal")

    def _clear_registry(self):
        """清空本程序写入注册表的全部配置，并重新从注册表加载到界面。"""
        if not messagebox.askyesno(
            "确认清除",
            "将清除本程序在注册表中的全部配置：\n"
            "  · 账号 / 密码\n"
            "  · 检测间隔\n"
            "  · 允许后台运行 / 默认后台运行\n"
            "  · 开机自启\n\n"
            "清除后界面会恢复为默认值，确定继续？"
        ):
            return
        ok, err = reg_clear_settings(include_autostart=True)
        if not ok:
            self._log("清除注册表失败：%s" % err, "red")
            messagebox.showerror("提示", "清除注册表失败：%s" % err)
            return
        # 清除后立即重新读取注册表，并把读到（默认）的值刷到界面
        settings = reg_load_settings()
        self.acc_var.set(settings["account"])             # 空
        self.pwd_var.set(settings["password"])            # 空
        self.interval_var.set(str(settings["interval"]))  # 5
        self.allow_bg_var.set(settings["allow_bg"])       # True
        self.start_minimized_var.set(settings["start_minimized"])  # False
        self.autostart_var.set(is_autostart_enabled())    # False
        self._sync_allow_bg_ui()                          # 恢复"允许后台运行"可编辑
        self._log("已清除注册表配置，并恢复默认值（账号/密码清空、间隔=5s、各勾选恢复默认）。", "")
        messagebox.showinfo("提示", "注册表配置已清除并恢复默认。")

    # ---------- 系统托盘（后台运行） ----------
    def _setup_tray(self):
        """初始化系统托盘图标。成功后关闭窗口将最小化到托盘而非退出。"""
        try:
            self.tray = TrayIcon(on_open=self._on_tray_open, on_exit=self._on_tray_exit)
            self.tray.start()
            if self.tray.wait_ready(timeout=1.0):
                self._log("已启用系统托盘：勾选“允许后台运行”时关闭窗口将最小化到托盘，右键图标可打开/退出。", "")
            else:
                self.tray = None
                self._log("系统托盘初始化失败，关闭窗口将直接退出。", "orange")
        except Exception as e:
            self.tray = None
            self._log("系统托盘初始化异常（%s），关闭窗口将直接退出。" % e, "orange")

    def _startup_minimize(self):
        """启动时按“默认后台运行”设置隐藏主窗口到托盘。"""
        self.root.withdraw()
        self._log("已按“默认后台运行”设置启动并最小化到系统托盘（任务栏右下角“显示隐藏的图标”）。", "")

    def _on_tray_open(self):
        """托盘回调（托盘线程）：请求打开主窗口，经队列转交主线程。"""
        UI_QUEUE.put(("tray", "open"))

    def _on_tray_exit(self):
        """托盘回调（托盘线程）：请求退出，经队列转交主线程。"""
        UI_QUEUE.put(("tray", "exit"))

    def _show_window(self):
        """恢复并前置主窗口（主线程调用）。"""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _on_close(self):
        """点击右上角 X：勾选了“允许后台运行”且有托盘则隐藏到托盘，否则直接退出。"""
        if self.allow_bg_var.get() and self.tray is not None:
            self.root.withdraw()
            self._log("已最小化到系统托盘（任务栏右下角“显示隐藏的图标”）。右键图标可“打开/退出”。", "")
        else:
            self._do_quit()

    def _do_quit(self):
        """真正退出：停监控、删托盘、销毁窗口（主线程调用）。"""
        if self._quitting:
            return
        self._quitting = True
        if self.monitor and self.monitor.is_alive():
            self.monitor.stop()
        if self.tray is not None:
            try:
                self.tray.shutdown()
            except Exception:
                pass
        self.root.destroy()

    # ---------- 日志 / 状态输出（主线程调用） ----------
    def _log(self, msg, color=""):
        self.log_text.configure(state="normal")
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", "[%s] %s\n" % (ts, msg), color)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_status(self, text, hit):
        self.status_var.set(text)
        self.hit_var.set("命中 %s" % hit if hit else "")
        color = "#9e9e9e"
        if "在线" in text:
            color = "#2e7d32"
        elif "离线" in text or "检测" in text:
            color = "#c62828" if "离线" in text else "#ef6c00"
        self.indicator.itemconfigure(self.dot, fill=color)

    # ---------- 与后台线程通信 ----------
    def _poll_queue(self):
        try:
            while True:
                item = UI_QUEUE.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._log(item[1], item[2] if len(item) > 2 else "")
                elif kind == "status":
                    online, hit = item[1], item[2]
                    self._set_status("在线" if online else "离线", hit)
                elif kind == "result":
                    ok = item[1]
                    self._log("测试结果：%s" % ("成功" if ok else "失败"),
                              "" if ok else "red")
                    messagebox.showinfo("测试连接", "登录%s" % ("成功" if ok else "失败"))
                elif kind == "tray":
                    cmd = item[1]
                    if cmd == "open":
                        self._show_window()
                    elif cmd == "exit":
                        self._do_quit()
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)


# ==================== 防止多开（单实例运行） ====================
# 通过本机回环端口实现单实例锁：
#   - 第一个启动的实例成功 bind 并监听固定端口，成为唯一实例；
#   - 若端口已被占用，则说明"已有程序在运行"，本进程向该端口发送唤醒指令，
#     通知已运行实例把自己的窗口显示并置于前台（即"再次打开=唤起已运行的程序"），
#     随后本进程直接退出。
# 该方案仅使用标准库 socket，跨平台（Windows/macOS/Linux 通用），零第三方依赖。
#
# 握手时序（务必保证客户端/服务端读写顺序一致）：
#   客户端 connect → send "PING" → recv "-OK" → send "SHOW" → close
#   服务端 accept  → recv "PING" → send "-OK" → recv "SHOW" → 唤起窗口 → close
SINGLE_INSTANCE_HOST = "127.0.0.1"
SINGLE_INSTANCE_PORT = 51799          # 固定端口，兼作单实例锁与唤醒通道
SINGLE_INSTANCE_MAGIC = b"RENZHENG-RECONNECT-SINGLE-INSTANCE"   # 握手标识，区分本程序与占用同端口的第三方程序


class SingleInstance:
    """单实例锁 + 唤起通道（仅标准库、跨平台）。

    acquire():  尝试占坑。
                - 返回 True  -> 本进程是唯一实例，应继续正常启动；
                - 返回 False -> 已有实例在运行且已被成功唤起，本进程应立即退出。
    start(cb):  作为已运行的主实例，在后台线程监听唤醒请求；收到后回调 cb
                （回调须自行转交主线程处理，勿直接操作 tk 控件）。
    """

    def __init__(self, host=SINGLE_INSTANCE_HOST, port=SINGLE_INSTANCE_PORT):
        self.host = host
        self.port = port
        self.server_sock = None

    def acquire(self):
        # 故意不设 SO_REUSEADDR：单实例锁恰恰依赖"端口占用即失败"
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((self.host, self.port))
            s.listen(5)
            self.server_sock = s
            return True
        except OSError:
            s.close()
            # 端口被占：尝试唤醒既有实例；唤醒成功则本进程应退出
            return not self._wake_existing()

    def _wake_existing(self):
        """连接占用端口的进程并握手：确认是本程序则发送唤醒并返回 True；
        若端口属无关第三方程序（握手不符）或连不上，则返回 False，放行本进程正常运行。

        客户端时序：connect → send "PING" → recv "-OK" → send "SHOW" → close"""
        try:
            c = socket.create_connection((self.host, self.port), timeout=2)
        except OSError:
            return False                       # 连不上：多为端口残留/崩溃后未释放，放行本进程
        try:
            c.sendall(b"PING")                 # 第一步：打招呼
            c.settimeout(2)
            resp = c.recv(64)                  # 第二步：等握手回应
            if resp != SINGLE_INSTANCE_MAGIC + b"-OK":
                return False                   # 端口被第三方程序占用，放行本进程
            c.sendall(b"SHOW")                 # 第三步：通知既有实例唤起窗口
            return True
        except OSError:
            return False
        finally:
            try:
                c.close()
            except OSError:
                pass

    def start(self, on_activate):
        """主实例后台监听线程：接受唤醒连接，握手后回调 on_activate。

        服务端时序：accept → recv "PING" → send "-OK" → recv "SHOW" → 唤起窗口 → close"""
        def _loop():
            while True:
                try:
                    conn, _addr = self.server_sock.accept()
                except OSError:
                    break                      # server_sock 已关闭，退出监听
                try:
                    conn.settimeout(3)         # 防止异常连接把监听线程挂死
                    hello = conn.recv(64)      # 第一步：先读客户端的 PING
                    if hello.strip() == b"PING":
                        conn.sendall(SINGLE_INSTANCE_MAGIC + b"-OK")   # 第二步：回应握手
                        cmd = conn.recv(16)    # 第三步：等 SHOW 指令
                        if b"SHOW" in cmd:
                            on_activate()      # 第四步：唤起既有窗口
                except OSError:
                    pass
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass
        threading.Thread(target=_loop, daemon=True).start()


SINGLE = SingleInstance()


def main():
    # 防止多开：若已有实例在运行，则唤起其窗口并直接退出本进程（不再启动第二个）
    if not SINGLE.acquire():
        return
    load_credentials()          # 从注册表加载账号密码到全局变量
    root = tk.Tk()
    App(root)
    # 第二个实例再次启动时，经 UI_QUEUE 转交主线程唤起窗口（线程安全，复用托盘"打开"逻辑：
    # 窗口最小化到托盘时会被恢复并前置；窗口本就显示时则被提到最前）
    SINGLE.start(lambda: UI_QUEUE.put(("tray", "open")))
    root.mainloop()


if __name__ == "__main__":
    main()