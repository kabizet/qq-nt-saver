# -*- coding: utf-8 -*-
"""最小 WebSocket 客户端 + Chrome DevTools Protocol 打印 PDF。

不使用 --print-to-pdf（该路径在某些机器上会被 Edge 后台模式/单例吞掉，
表现为返回码 0、无任何输出），改为：
    启动浏览器 --remote-debugging-port=0 → 读 DevToolsActivePort
    → Page.navigate → Page.printToPDF → 自己写文件
"""

import base64
import json
import os
import socket
import struct
import subprocess
import time
import urllib.parse


class CDPError(RuntimeError):
    pass


class _WS(object):
    def __init__(self, url, timeout=180):
        u = urllib.parse.urlparse(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or 80
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key))
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("WebSocket 握手失败：连接被关闭")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise CDPError("WebSocket 握手失败：%r" % head[:120])
        self._buf = rest

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CDPError("WebSocket 连接中断")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, obj):
        data = json.dumps(obj).encode("utf-8")
        header = bytearray([0x81])
        n = len(data)
        mask = os.urandom(4)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        header += bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header))

    def recv(self):
        payload = b""
        while True:
            h = self._read(2)
            fin = h[0] & 0x80
            opcode = h[0] & 0x0F
            ln = h[1] & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            payload += self._read(ln)
            if opcode == 0x8:
                raise CDPError("WebSocket 被浏览器关闭")
            if opcode == 0x9:      # ping -> pong
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if fin:
                return payload

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class _CDP(object):
    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self.events = []

    def call(self, method, params=None, session=None, timeout=180):
        self._id += 1
        mid = self._id
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send(msg)
        deadline = time.time() + timeout
        while True:
            if time.time() > deadline:
                raise CDPError("等待 %s 响应超时" % method)
            resp = json.loads(self.ws.recv().decode("utf-8", "replace"))
            if resp.get("id") == mid:
                if "error" in resp:
                    raise CDPError("%s 失败: %s" % (method, resp["error"]))
                return resp.get("result", {})
            if "method" in resp:
                self.events.append(resp)

    def wait_event(self, name, timeout=60):
        deadline = time.time() + timeout
        for ev in list(self.events):
            if ev.get("method") == name:
                self.events.remove(ev)
                return ev
        while time.time() < deadline:
            resp = json.loads(self.ws.recv().decode("utf-8", "replace"))
            if resp.get("method") == name:
                return resp
            if "method" in resp:
                self.events.append(resp)
        return None


def _read_port_file(profile, proc, timeout=25):
    pf = os.path.join(profile, "DevToolsActivePort")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(pf):
            try:
                lines = open(pf, encoding="utf-8", errors="replace").read().splitlines()
                if lines and lines[0].strip().isdigit():
                    return int(lines[0].strip()), (lines[1].strip() if len(lines) > 1 else "")
            except Exception:
                pass
        if proc.poll() is not None:
            raise CDPError("浏览器进程已退出（返回码 %s），未生成调试端口" % proc.returncode)
        time.sleep(0.1)
    raise CDPError("等待浏览器调试端口超时（浏览器可能被策略限制或已在后台运行）")


def print_to_pdf(browser, html_path, out_pdf, work, timeout=240):
    """用 CDP 打印 HTML 到 PDF，返回 (ok, 详情字符串)。"""
    profile = os.path.join(work, "cdp_profile")
    os.makedirs(profile, exist_ok=True)
    url = "file:///" + html_path.replace("\\", "/")
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000
    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "--disable-features=StartupBoost,CalculateNativeWinOcclusion",
        "--remote-debugging-port=0",
        "--remote-allow-origins=*",
        "--user-data-dir=" + profile,
        "about:blank",
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    ws = None
    try:
        port, path = _read_port_file(profile, proc)
        # socket 超时必须覆盖整段打印：printToPDF 渲染大文档时几分钟不会有
        # 任何 WS 帧往返，默认 180s 会把正常的打印当成断连掐掉
        ws = _WS("ws://127.0.0.1:%d%s" % (port, path or "/devtools/browser"),
                 timeout=max(180, timeout))
        cdp = _CDP(ws)
        tgt = cdp.call("Target.createTarget", {"url": "about:blank"})
        tid = tgt["targetId"]
        att = cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True})
        sid = att["sessionId"]
        cdp.call("Page.enable", session=sid)
        cdp.call("Page.navigate", {"url": url}, session=sid)
        cdp.wait_event("Page.loadEventFired", timeout=90)
        time.sleep(0.6)                     # 等图片解码/布局稳定
        res = cdp.call("Page.printToPDF", {
            "printBackground": True,
            "preferCSSPageSize": True,
            "displayHeaderFooter": False,
        }, session=sid, timeout=timeout)
        data = res.get("data")
        if not data:
            return False, "printToPDF 未返回数据"
        raw = base64.b64decode(data)
        if not raw:
            return False, "printToPDF 返回空数据"
        parent = os.path.dirname(os.path.abspath(out_pdf))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with open(out_pdf, "wb") as f:
            f.write(raw)
        return True, "CDP 打印成功 %d 字节" % len(raw)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
