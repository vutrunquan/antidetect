import socket
import select
import threading
import time
from urllib.parse import urlparse
import socks

_PROXY_SCHEME_CACHE = {}
_ACTIVE_BRIDGES = {}
_BRIDGE_LOCK = threading.Lock()

def probe_socks5(host: str, port: int, timeout: float = 1.0) -> bool:
    """Kiểm tra nhanh xem cổng proxy có hỗ trợ giao thức SOCKS5 không."""
    cache_key = f"{host}:{port}"
    if cache_key in _PROXY_SCHEME_CACHE:
        return _PROXY_SCHEME_CACHE[cache_key] == "socks5"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, int(port)))
        # Gửi SOCKS5 handshake: VER 5, 2 methods (0: no auth, 2: user/pass)
        s.sendall(b"\x05\x02\x00\x02")
        resp = s.recv(2)
        s.close()
        if resp and resp[0] == 5:
            _PROXY_SCHEME_CACHE[cache_key] = "socks5"
            return True
    except Exception:
        pass
    _PROXY_SCHEME_CACHE[cache_key] = "http"
    return False

def parse_proxy(proxy_str: str):
    """
    Phân tích chuỗi proxy linh hoạt:
    - ip:port:user:pass
    - ip:port
    - scheme://user:pass@host:port
    - scheme://host:port:user:pass
    - scheme://host:port
    Tự động nhận diện HTTP vs SOCKS5 nếu người dùng không ghi rõ scheme.
    """
    if not proxy_str or not proxy_str.strip():
        return None
    s = proxy_str.strip()
    
    scheme = None
    for prefix in ("socks5h://", "socks5://", "socks4://", "http://", "https://"):
        if s.lower().startswith(prefix):
            scheme = prefix.rstrip("://")
            s = s[len(prefix):]
            break

    host = ""
    port = ""
    user = None
    pwd = None

    if "@" in s:
        # user:pass@host:port
        creds, host_port = s.split("@", 1)
        if ":" in creds:
            user, pwd = creds.split(":", 1)
        else:
            user = creds
        if ":" in host_port:
            host, port = host_port.split(":", 1)
        else:
            host = host_port
    else:
        parts = s.split(":")
        if len(parts) == 4:
            # host:port:user:pass
            host, port, user, pwd = parts
        elif len(parts) == 2:
            # host:port
            host, port = parts
        elif len(parts) == 1:
            host = parts[0]
            port = "80"
        else:
            try:
                parsed = urlparse("http://" + s)
                host = parsed.hostname or ""
                port = str(parsed.port or 80)
                user = parsed.username
                pwd = parsed.password
            except Exception:
                return None

    if not host or not port:
        return None

    # Tự động nhận diện scheme nếu chưa có
    if not scheme:
        cache_key = f"{host}:{port}"
        if cache_key in _PROXY_SCHEME_CACHE:
            scheme = _PROXY_SCHEME_CACHE[cache_key]
        else:
            if probe_socks5(host, int(port)):
                scheme = "socks5"
            else:
                scheme = "http"

    res = {
        "server": f"{scheme}://{host}:{port}",
        "scheme": scheme,
        "host": host,
        "port": int(port),
    }
    if user:
        res["username"] = user
    if pwd:
        res["password"] = pwd
    return res

class Socks5HttpBridge:
    """
    Cầu nối cục bộ chuyển tiếp yêu cầu HTTP / HTTPS CONNECT từ trình duyệt
    tới proxy SOCKS5 từ xa (có hỗ trợ Username/Password).
    Giải quyết triệt để giới hạn: Playwright không hỗ trợ xác thực SOCKS5 trực tiếp.
    """
    def __init__(self, remote_host: str, remote_port: int, username: str = "", password: str = "", local_port: int = 0):
        self.remote_host = remote_host
        self.remote_port = int(remote_port)
        self.username = username or ""
        self.password = password or ""
        self.local_port = local_port
        self.server_sock = None
        self.actual_port = 0
        self.is_running = False
        self.thread = None

    def start(self) -> int:
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind(("127.0.0.1", self.local_port))
        self.actual_port = self.server_sock.getsockname()[1]
        self.server_sock.listen(100)
        self.is_running = True
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()
        return self.actual_port

    def _accept_loop(self):
        while self.is_running:
            try:
                client_sock, _ = self.server_sock.accept()
                threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True).start()
            except Exception:
                break

    def _forward(self, src: socket.socket, dst: socket.socket):
        try:
            while self.is_running:
                data = src.recv(32768)
                if not data:
                    break
                dst.sendall(data)
        except Exception:
            pass
        finally:
            try: src.close()
            except Exception: pass
            try: dst.close()
            except Exception: pass

    def _handle_client(self, client_sock: socket.socket):
        try:
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = client_sock.recv(4096)
                if not chunk:
                    break
                req += chunk
            if not req:
                client_sock.close()
                return

            first_line = req.split(b"\r\n")[0].decode("latin1", errors="ignore")
            parts = first_line.split()
            if len(parts) < 2:
                client_sock.close()
                return

            method, target = parts[0], parts[1]
            if method.upper() == "CONNECT":
                if ":" in target:
                    host, port_s = target.split(":", 1)
                    port = int(port_s)
                else:
                    host = target
                    port = 443
            else:
                from urllib.parse import urlparse
                u = urlparse(target)
                host = u.hostname or target
                port = u.port or (443 if u.scheme == "https" else 80)

            # Kết nối tới proxy SOCKS5 từ xa
            remote = socks.socksocket()
            remote.set_proxy(
                socks.SOCKS5,
                self.remote_host,
                self.remote_port,
                rdns=True,
                username=self.username if self.username else None,
                password=self.password if self.password else None,
            )
            remote.settimeout(20)
            remote.connect((host, port))
            remote.settimeout(None)

            if method.upper() == "CONNECT":
                client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                remote.sendall(req)

            t1 = threading.Thread(target=self._forward, args=(client_sock, remote), daemon=True)
            t2 = threading.Thread(target=self._forward, args=(remote, client_sock), daemon=True)
            t1.start()
            t2.start()
        except Exception:
            try:
                client_sock.close()
            except Exception:
                pass

    def stop(self):
        self.is_running = False
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass

def get_or_create_socks5_bridge(remote_host: str, remote_port: int, username: str = "", password: str = "") -> Socks5HttpBridge:
    key = (remote_host, int(remote_port), username or "", password or "")
    with _BRIDGE_LOCK:
        if key in _ACTIVE_BRIDGES and _ACTIVE_BRIDGES[key].is_running:
            return _ACTIVE_BRIDGES[key]
        bridge = Socks5HttpBridge(remote_host, remote_port, username, password)
        bridge.start()
        _ACTIVE_BRIDGES[key] = bridge
        return bridge

def setup_camoufox_proxy(proxy_str: str):
    """
    Chuẩn bị cấu hình proxy cho Camoufox:
    - Nếu là SOCKS5 có xác thực (user/pass), tự động dựng hoặc tái sử dụng cầu nối HTTP cục bộ.
    - Trả về tuple: (camoufox_proxy_dict, bridge_instance)
    """
    cfg = parse_proxy(proxy_str)
    if not cfg:
        return None, None

    scheme = cfg.get("scheme", "http").lower()
    has_auth = bool(cfg.get("username") and cfg.get("password"))

    # Playwright Firefox không hỗ trợ auth trên socks5 trực tiếp -> dùng bridge
    if scheme in ("socks5", "socks5h", "socks4") and has_auth:
        bridge = get_or_create_socks5_bridge(
            remote_host=cfg["host"],
            remote_port=cfg["port"],
            username=cfg.get("username", ""),
            password=cfg.get("password", ""),
        )
        return {"server": f"http://127.0.0.1:{bridge.actual_port}"}, bridge

    # Trả về format chuẩn Playwright
    playwright_proxy = {"server": cfg["server"]}
    if cfg.get("username"):
        playwright_proxy["username"] = cfg["username"]
    if cfg.get("password"):
        playwright_proxy["password"] = cfg["password"]
    return playwright_proxy, None
