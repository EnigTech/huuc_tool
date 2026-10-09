# -*- coding: utf-8 -*-
"""
huuc校园网断网自动重连工具 —— 图形界面版

加密方式（来源于抓取到的认证页 JS）：
    key = ip[0] ^ ip[1] ^ ... ^ ip[n]      # 本机 IP 每个字符的 ASCII 码逐字符异或
    加密 = 每个明文字符 XOR key 后转两位十六进制
    jsVersion = "4.2.1"
"""
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
    import winreg
except ImportError:
    winreg = None

# ==================== 开机自启 ====================
AUTOSTART_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE_NAME = "huuc校园网自动重连工具"


def _autostart_command():
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    pyw = sys.executable if sys.executable.lower().endswith("pythonw.exe") else \
        os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not pyw or not os.path.exists(pyw):
        pyw = sys.executable
    return '"%s" "%s"' % (pyw, os.path.abspath(__file__))


def is_autostart_enabled():
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
                    pass
        finally:
            winreg.CloseKey(key)
        return True, ""
    except Exception as e:
        return False, str(e)


# ==================== 配置持久化（注册表） ====================
SETTINGS_REG_KEY = r"Software\RenzhengAutoReconnect"
SETTINGS_VALUE_ALLOW_BG = "AllowBackground"
SETTINGS_VALUE_START_MINIMIZED = "StartMinimized"
SETTINGS_VALUE_ACCOUNT = "Account"
SETTINGS_VALUE_PASSWORD = "Password"
SETTINGS_VALUE_INTERVAL = "Interval"
SETTINGS_VALUE_CARRIER = "Carrier"

_REG_DWORD = winreg.REG_DWORD if winreg else 0
_REG_SZ = winreg.REG_SZ if winreg else 0


def _reg_write(name, value, value_type):
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
    """轻度混淆（非加密）：XOR + Base64。"""
    import base64
    raw = bytes(ord(c) ^ 0x5A for c in s)
    return base64.b64encode(raw).decode("ascii")


def _deobfuscate(s):
    import base64
    if not s:
        return ""
    try:
        raw = base64.b64decode(str(s).encode("ascii"))
        return "".join(chr(b ^ 0x5A) for b in raw)
    except Exception:
        return ""


def reg_load_settings():
    result = {
        "account": "",
        "password": "",
        "interval": DEFAULT_INTERVAL_SEC,
        "allow_bg": True,
        "start_minimized": False,
        "carrier": "unicom",
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
    v = _reg_read(SETTINGS_VALUE_CARRIER)
    if v in ("unicom", "cmcc"):
        result["carrier"] = v
    if result["start_minimized"]:
        result["allow_bg"] = True
    return result


def reg_save_credentials(account, password):
    ok1, err1 = _reg_write(SETTINGS_VALUE_ACCOUNT, _obfuscate(account), _REG_SZ)
    if not ok1:
        return False, err1
    return _reg_write(SETTINGS_VALUE_PASSWORD, _obfuscate(password), _REG_SZ)


def reg_save_interval(seconds):
    return _reg_write(SETTINGS_VALUE_INTERVAL, int(seconds), _REG_DWORD)


def reg_save_carrier(carrier):
    if carrier not in ("unicom", "cmcc"):
        carrier = "unicom"
    return _reg_write(SETTINGS_VALUE_CARRIER, carrier, _REG_SZ)


def set_allow_background(enabled):
    return _reg_write(SETTINGS_VALUE_ALLOW_BG, 1 if enabled else 0, _REG_DWORD)


def set_start_minimized(enabled):
    return _reg_write(SETTINGS_VALUE_START_MINIMIZED, 1 if enabled else 0, _REG_DWORD)


def reg_clear_settings(include_autostart=True):
    if winreg is None:
        return False, "当前系统不支持注册表"
    try:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, SETTINGS_REG_KEY, 0,
                                 winreg.KEY_ALL_ACCESS)
        except OSError:
            key = None
        if key is not None:
            try:
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
ACCOUNT = ""
ACCOUNT_SUFFIX = "@unicom"
PASSWORD = ""

CARRIER_SUFFIX = {
    "unicom": "@unicom",
    "cmcc":   "@cmcc",
}
CARRIER_LABEL = {
    "unicom": "联通",
    "cmcc":   "移动",
}

# 从抓取到的认证页 JS 中确认：jsVersion = "4.2.1"
JS_VERSION = "4.2.1"

LOGIN_URL = "https://netauth.huuc.edu.cn:802/eportal/portal/login"
PORTAL_URL = "https://netauth.huuc.edu.cn:802/eportal/portal/jsp/portal/tp-common-redirect.jsp"

PING_HOSTS = [
    "www.baidu.com",
    "www.qq.com",
    "www.taobao.com",
    "aliyun.com",
]
PING_TIMEOUT_SEC = 3
DEFAULT_INTERVAL_SEC = 5
RETRY_DELAY_SEC = 5
AFTER_LOGIN_WAIT_SEC = 8
HTTP_TIMEOUT_SEC = 15

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LOG_FILE = os.path.join(BASE_DIR, "auto_login.log")

CTX = ssl._create_unverified_context()
UI_QUEUE = queue.Queue()


# ==================== 核心逻辑 ====================
def load_credentials():
    global ACCOUNT, PASSWORD, ACCOUNT_SUFFIX
    s = reg_load_settings()
    ACCOUNT, PASSWORD = s["account"], s["password"]
    ACCOUNT_SUFFIX = CARRIER_SUFFIX.get(s["carrier"], "@unicom")
    return ACCOUNT, PASSWORD


def save_credentials(account, password):
    global ACCOUNT, PASSWORD
    ACCOUNT, PASSWORD = account.strip(), password
    return reg_save_credentials(ACCOUNT, PASSWORD)


def full_account(account=None):
    acc = account if account is not None else ACCOUNT
    if acc.endswith(ACCOUNT_SUFFIX) or not ACCOUNT_SUFFIX:
        return acc
    return acc + ACCOUNT_SUFFIX


def xor_hex(s, key):
    """把字符串每个字符 XOR key 后转两位十六进制。
    对应 JS 里的 util.enc_pwd(passIn, key)。"""
    return "".join(format(ord(c) ^ key, "02x") for c in s)


def calc_key_from_ip(ip):
    """对应 JS 里的 util.getkey(ip)：把 IP 每个字符的 ASCII 码逐字符异或。
    返回值必然是 0~255 之间的整数。"""
    key = 0
    for ch in ip:
        key ^= ord(ch)
    return key & 0xFF


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
        if is_win:
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT,
                                          timeout=PING_TIMEOUT_SEC + 5,
                                          creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT,
                                          timeout=PING_TIMEOUT_SEC + 5)
        text = out.decode("gbk", errors="ignore") if is_win else out.decode("utf-8", errors="ignore")
        low = text.lower()
        return "ttl=" in low or ("0%" in low and "loss" in low)
    except Exception:
        return False


def check_network():
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

    ok_kw = ["成功", "已登录", "认证成功", "登录成功", "AC999"]
    if any(k in msg for k in ok_kw):
        return True, msg
    fail_kw = ["不存在", "错误", "失败", "密码", "超时", "在线", "绑定", "已用"]
    if any(k in msg for k in fail_kw):
        return False, msg
    if ret_code is not None:
        return ret_code in (0, 2), msg
    return False, msg


def _try_login_with_key(acc, pwd, local_ip, key, js_ver):
    """用指定的 XOR 密钥尝试一次认证。"""
    params = {
        "callback":      xor_hex("dr1003", key),
        "login_method":  xor_hex("1", key),
        "user_account":  xor_hex(full_account(acc), key),
        "user_password": xor_hex(pwd, key),
        "wlan_user_ip":  xor_hex(local_ip, key) or "",
        "wlan_user_ipv6": "",
        "wlan_user_mac": "",
        "wlan_ac_ip": "",
        "wlan_ac_name": "",
        "jsVersion":     xor_hex(js_ver, key),
        "captcha": "",
        "terminal_type": xor_hex("1", key),
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
        return ok, msg
    except HTTPError as e:
        return False, "HTTP %s" % e.code
    except URLError as e:
        return False, str(e.reason)
    except Exception as e:
        return False, str(e)


def do_login(account=None, password=None, allow_interactive=True):
    """执行一次校园网认证。

    密钥由本机 IP 动态计算得到（见 calc_key_from_ip），无需任何探测。
    allow_interactive 参数保留以兼容旧调用，当前实现中不再使用。
    """
    acc = account if account is not None else ACCOUNT
    pwd = password if password is not None else PASSWORD

    local_ip = get_local_ip()
    if not local_ip:
        emit("error", "无法获取本机 IP，无法计算加密密钥。")
        return False

    fetch_portal_cookie()

    key = calc_key_from_ip(local_ip)
    _log_debug("本机 IP: %s，账号: %s，加密密钥: 0x%02X (jsVersion=%s)",
               local_ip, full_account(acc), key, JS_VERSION)

    ok, msg = _try_login_with_key(acc, pwd, local_ip, key, JS_VERSION)
    if ok:
        _log_debug("认证成功。")
        return True
    emit("error", "认证失败：%s" % msg)
    return False


def _log_debug(fmt, *args):
    try:
        UI_QUEUE.put(("log", "[DBG] " + (fmt % args if args else fmt)))
    except Exception:
        pass


# ==================== 日志 ====================
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
        return self._stop_evt.wait(seconds)

    def run(self):
        emit("info", "已启动监控，检测间隔 %d 秒。账号后缀 %s" % (self.interval, ACCOUNT_SUFFIX))
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


# ==================== Windows 系统托盘图标 + 应用图标 ====================
if sys.platform.startswith("win"):
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _shell32 = ctypes.windll.shell32

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

    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x00000010
    LR_DEFAULTSIZE = 0x00000040
    WM_SETICON = 0x0080
    ICON_SMALL = 0
    ICON_BIG = 1

    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

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

    _user32.LoadImageW.restype = wintypes.HANDLE
    _user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    _user32.GetParent.restype = wintypes.HWND
    _user32.GetParent.argtypes = [wintypes.HWND]
    _user32.SendMessageW.restype = ctypes.c_ssize_t
    _user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]


    def _load_app_hicon():
        if getattr(sys, "frozen", False):
            h = _user32.LoadIconW(_kernel32.GetModuleHandleW(None),
                                  ctypes.cast(1, ctypes.c_void_p))
            if h:
                return h
        ico_path = os.path.join(BASE_DIR, "app.ico")
        if os.path.exists(ico_path):
            h = _user32.LoadImageW(None, ico_path, IMAGE_ICON, 0, 0,
                                   LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                return h
        return _user32.LoadIconW(None, ctypes.cast(IDI_APPLICATION, ctypes.c_void_p))


    def set_window_icon(root):
        try:
            hicon = _load_app_hicon()
            if not hicon:
                return
            root.update_idletasks()
            hwnd = root.winfo_id()
            parent = _user32.GetParent(hwnd)
            target = parent if parent else hwnd
            _user32.SendMessageW(target, WM_SETICON, ICON_SMALL, hicon)
            _user32.SendMessageW(target, WM_SETICON, ICON_BIG, hicon)
        except Exception:
            pass


    class TrayIcon:
        def __init__(self, on_open, on_exit, tooltip="huuc校园网自动重连工具"):
            self._on_open = on_open
            self._on_exit = on_exit
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
            self._ready = threading.Event()

        def start(self):
            self._thread.start()

        def shutdown(self):
            if self._thread_id is not None:
                _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            if self._thread.is_alive():
                self._thread.join(timeout=2.0)

        def wait_ready(self, timeout=1.0):
            return self._ready.wait(timeout)

        def _run(self):
            self._thread_id = _kernel32.GetCurrentThreadId()
            self._hicon = _load_app_hicon()
            if not self._hicon:
                return

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
            self._ready.set()

            msg = wintypes.MSG()
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))

            if self._nid is not None:
                _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            if self._hwnd is not None:
                _user32.DestroyWindow(self._hwnd)

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
            try:
                fn()
            except Exception:
                pass

else:
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

        settings = reg_load_settings()

        self.acc_var = tk.StringVar(value=settings["account"])
        self.pwd_var = tk.StringVar(value=settings["password"])
        self.interval_var = tk.StringVar(value=str(settings["interval"]))
        self.carrier_var = tk.StringVar(value=settings["carrier"])
        self.status_var = tk.StringVar(value="未启动")
        self.hit_var = tk.StringVar(value="-")
        self.key_var = tk.StringVar(value="自动计算")
        self.autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        self.allow_bg_var = tk.BooleanVar(value=settings["allow_bg"])
        self.start_minimized_var = tk.BooleanVar(value=settings["start_minimized"])

        self._build_ui()
        self._sync_allow_bg_ui()
        self._apply_carrier_to_ui()
        self._refresh_key_status()
        self._apply_window_icon()
        self._setup_tray()

        self.root.after(120, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if self.start_minimized_var.get() and self.tray is not None:
            self.root.after(0, self._startup_minimize)

        self.root.after(0, self._auto_start_monitor)

    def _apply_window_icon(self):
        ico_path = os.path.join(BASE_DIR, "app.ico")
        if os.path.exists(ico_path):
            try:
                self.root.iconbitmap(ico_path)
            except Exception:
                pass
        set_window_icon(self.root)

    def _refresh_key_status(self):
        ip = get_local_ip()
        if not ip:
            self.key_var.set("（无法获取本机 IP）")
            return
        key = calc_key_from_ip(ip)
        self.key_var.set("0x%02X（由本机 IP %s 计算，jsVersion=%s）" % (key, ip, JS_VERSION))

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("vista" if "vista" in style.theme_names() else "default")

        pad = {"padx": 8, "pady": 6}
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

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
        self.suffix_label = ttk.Label(cfg, text="（账号后缀：%s）" % ACCOUNT_SUFFIX)
        self.suffix_label.grid(row=2, column=2, sticky="w")

        ttk.Label(cfg, text="运营商:").grid(row=3, column=0, sticky="w")
        carrier_frame = ttk.Frame(cfg)
        carrier_frame.grid(row=3, column=1, columnspan=2, sticky="w", padx=4)
        ttk.Radiobutton(carrier_frame, text="联通", value="unicom",
                        variable=self.carrier_var,
                        command=self._on_carrier_change).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(carrier_frame, text="移动", value="cmcc",
                        variable=self.carrier_var,
                        command=self._on_carrier_change).pack(side="left")

        ttk.Label(cfg, text="加密密钥:").grid(row=4, column=0, sticky="w")
        ttk.Label(cfg, textvariable=self.key_var).grid(row=4, column=1, columnspan=2,
                                                        sticky="w", padx=4)

        btns = ttk.Frame(cfg)
        btns.grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(btns, text="保存配置", command=self._save).pack(side="left", padx=4)
        ttk.Button(btns, text="测试连接", command=self._test).pack(side="left", padx=4)
        ttk.Button(btns, text="刷新密钥", command=self._refresh_key_status).pack(side="left", padx=4)
        ttk.Button(btns, text="清除注册表", command=self._clear_registry).pack(side="left", padx=4)

        chks = ttk.Frame(cfg)
        chks.grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Checkbutton(chks, text="开机自启", variable=self.autostart_var,
                        command=self._on_autostart_toggle).pack(side="left", padx=4)
        self.allow_bg_chk = ttk.Checkbutton(chks, text="允许后台运行",
                                            variable=self.allow_bg_var,
                                            command=self._on_allow_bg_toggle)
        self.allow_bg_chk.pack(side="left", padx=4)
        ttk.Checkbutton(chks, text="默认后台运行", variable=self.start_minimized_var,
                        command=self._on_start_minimized_toggle).pack(side="left", padx=4)

        cfg.columnconfigure(1, weight=1)

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

        logbox = ttk.LabelFrame(main, text="运行日志", padding=4)
        logbox.pack(fill="both", expand=True, **pad)

        self.log_text = tk.Text(logbox, height=10, state="disabled", wrap="word",
                                font=("Consolas", 9))
        sb = ttk.Scrollbar(logbox, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

        self.log_text.tag_configure("red", foreground="#c62828")
        self.log_text.tag_configure("orange", foreground="#ef6c00")

    def _toggle_pwd(self):
        self.pwd_entry.configure(show="" if self.show_pwd.get() else "*")

    def _apply_carrier_to_ui(self):
        global ACCOUNT_SUFFIX
        carrier = self.carrier_var.get()
        if carrier not in CARRIER_SUFFIX:
            carrier = "unicom"
            self.carrier_var.set(carrier)
        ACCOUNT_SUFFIX = CARRIER_SUFFIX[carrier]
        try:
            self.suffix_label.configure(text="（账号后缀：%s）" % ACCOUNT_SUFFIX)
        except Exception:
            pass

    def _on_carrier_change(self):
        carrier = self.carrier_var.get()
        if carrier not in CARRIER_SUFFIX:
            carrier = "unicom"
            self.carrier_var.set(carrier)
        self._apply_carrier_to_ui()
        ok, err = reg_save_carrier(carrier)
        if ok:
            self._log("已切换运营商为%s（账号后缀 %s），已保存到注册表。" %
                      (CARRIER_LABEL[carrier], ACCOUNT_SUFFIX), "")
        else:
            self._log("保存运营商选择失败：%s" % err, "red")

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
        ok3, err3 = reg_save_carrier(self.carrier_var.get())
        if not ok1 or not ok2 or not ok3:
            err = err1 or err2 or err3
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
        self._apply_carrier_to_ui()
        ip = get_local_ip()
        if ip:
            key = calc_key_from_ip(ip)
            self._log("正在测试登录（账号 %s，本机 IP %s，密钥 0x%02X）..."
                      % (full_account(acc), ip, key), "")
        else:
            self._log("正在测试登录（账号 %s）..." % full_account(acc), "")

        def worker():
            ok = do_login(acc, pwd)
            UI_QUEUE.put(("result", ok))

        threading.Thread(target=worker, daemon=True).start()

    def _start(self):
        self._start_monitor(manual=True)

    def _start_monitor(self, manual=True):
        if manual:
            interval = self._validate_interval()
            if interval is None:
                return
        else:
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

        self._apply_carrier_to_ui()

        if manual:
            save_credentials(acc, pwd)
            reg_save_interval(interval)
            reg_save_carrier(self.carrier_var.get())

        if self.monitor and self.monitor.is_alive():
            return
        self.monitor = MonitorThread(interval, acc, pwd)
        self.monitor.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._set_status("正在检测...", None)

    def _auto_start_monitor(self):
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
            self._log("已%s开机自启（已写入注册表）。" %
                      ("开启" if checkbox_state else "关闭"),
                      "" if checkbox_state else "orange")
        else:
            self._log("设置开机自启失败：%s" % err, "red")
            self.autostart_var.set(not checkbox_state)
            messagebox.showerror("提示", "设置开机自启失败：%s" % err)

    def _on_allow_bg_toggle(self):
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
        enabled = self.start_minimized_var.get()
        ok, err = set_start_minimized(enabled)
        if not ok:
            self._log("保存“默认后台运行”偏好失败：%s" % err, "red")
            return
        if enabled:
            if not self.allow_bg_var.get():
                self.allow_bg_var.set(True)
                set_allow_background(True)
                self._log("已联动开启“允许后台运行”。", "")
        self._sync_allow_bg_ui()
        self._log("已%s“默认后台运行”。" % ("开启" if enabled else "关闭"),
                  "" if enabled else "orange")

    def _sync_allow_bg_ui(self):
        if self.start_minimized_var.get():
            if not self.allow_bg_var.get():
                self.allow_bg_var.set(True)
                set_allow_background(True)
            self.allow_bg_chk.configure(state="disabled")
        else:
            self.allow_bg_chk.configure(state="normal")

    def _clear_registry(self):
        if not messagebox.askyesno(
            "确认清除",
            "将清除本程序在注册表中的全部配置：\n"
            "  · 账号 / 密码\n"
            "  · 检测间隔\n"
            "  · 运营商\n"
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
        settings = reg_load_settings()
        self.acc_var.set(settings["account"])
        self.pwd_var.set(settings["password"])
        self.interval_var.set(str(settings["interval"]))
        self.carrier_var.set(settings["carrier"])
        self.allow_bg_var.set(settings["allow_bg"])
        self.start_minimized_var.set(settings["start_minimized"])
        self.autostart_var.set(is_autostart_enabled())
        self._sync_allow_bg_ui()
        self._apply_carrier_to_ui()
        self._refresh_key_status()
        self._log("已清除注册表配置，并恢复默认值。", "")
        messagebox.showinfo("提示", "注册表配置已清除并恢复默认。")

    def _setup_tray(self):
        try:
            self.tray = TrayIcon(on_open=self._on_tray_open, on_exit=self._on_tray_exit)
            self.tray.start()
            if self.tray.wait_ready(timeout=1.0):
                self._log("已启用系统托盘。", "")
            else:
                self.tray = None
                self._log("系统托盘初始化失败，关闭窗口将直接退出。", "orange")
        except Exception as e:
            self.tray = None
            self._log("系统托盘初始化异常（%s）。" % e, "orange")

    def _startup_minimize(self):
        self.root.withdraw()
        self._log("已按“默认后台运行”设置启动并最小化到系统托盘。", "")

    def _on_tray_open(self):
        UI_QUEUE.put(("tray", "open"))

    def _on_tray_exit(self):
        UI_QUEUE.put(("tray", "exit"))

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _on_close(self):
        if self.allow_bg_var.get() and self.tray is not None:
            self.root.withdraw()
            self._log("已最小化到系统托盘。右键图标可“打开/退出”。", "")
        else:
            self._do_quit()

    def _do_quit(self):
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
                elif kind == "key":
                    self._refresh_key_status()
                elif kind == "result":
                    ok = item[1]
                    self._log("测试结果：%s" % ("成功" if ok else "失败"),
                              "" if ok else "red")
                    self._refresh_key_status()
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


# ==================== 单实例 ====================
SINGLE_INSTANCE_HOST = "127.0.0.1"
SINGLE_INSTANCE_PORT = 51799
SINGLE_INSTANCE_MAGIC = b"RENZHENG-RECONNECT-SINGLE-INSTANCE"


class SingleInstance:
    def __init__(self, host=SINGLE_INSTANCE_HOST, port=SINGLE_INSTANCE_PORT):
        self.host = host
        self.port = port
        self.server_sock = None

    def acquire(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((self.host, self.port))
            s.listen(5)
            self.server_sock = s
            return True
        except OSError:
            s.close()
            return not self._wake_existing()

    def _wake_existing(self):
        try:
            c = socket.create_connection((self.host, self.port), timeout=2)
        except OSError:
            return False
        try:
            c.sendall(b"PING")
            c.settimeout(2)
            resp = c.recv(64)
            if resp != SINGLE_INSTANCE_MAGIC + b"-OK":
                return False
            c.sendall(b"SHOW")
            return True
        except OSError:
            return False
        finally:
            try:
                c.close()
            except OSError:
                pass

    def start(self, on_activate):
        def _loop():
            while True:
                try:
                    conn, _addr = self.server_sock.accept()
                except OSError:
                    break
                try:
                    conn.settimeout(3)
                    hello = conn.recv(64)
                    if hello.strip() == b"PING":
                        conn.sendall(SINGLE_INSTANCE_MAGIC + b"-OK")
                        cmd = conn.recv(16)
                        if b"SHOW" in cmd:
                            on_activate()
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
    if not SINGLE.acquire():
        return
    load_credentials()
    root = tk.Tk()
    App(root)
    SINGLE.start(lambda: UI_QUEUE.put(("tray", "open")))
    root.mainloop()


if __name__ == "__main__":
    main()