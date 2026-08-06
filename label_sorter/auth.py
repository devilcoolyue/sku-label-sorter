"""登录鉴权。

签名 token 而非服务端会话表：无额外依赖，服务重启后已登录的用户不掉线
（密钥落盘持久化）。token 放在 HttpOnly Cookie 里，JS 读不到，少一条
XSS 偷 token 的路径。
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

COOKIE = "sorter_session"
SESSION_HOURS = float(os.environ.get("SORTER_SESSION_HOURS", "12"))
USER = os.environ.get("SORTER_USER", "admin")

# 登录失败锁定：同一 IP 连续失败 FAIL_LIMIT 次，锁 LOCK_SECONDS
FAIL_LIMIT = 5
LOCK_SECONDS = 900

_fails = {}  # ip -> [失败次数, 最后一次失败时间]
_fails_lock = threading.Lock()


def _b64e(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def load_secret(work_dir):
    """签名密钥，落盘保存，权限 0600。没有就生成一把。"""
    path = work_dir / ".secret"
    if path.exists():
        return path.read_bytes()
    key = secrets.token_bytes(32)
    path.write_bytes(key)
    path.chmod(0o600)
    return key


def load_password(work_dir):
    """口令优先取环境变量；没配就随机生成一个并落盘，避免出现弱默认口令。

    返回 (口令, 是否是这次新生成的)—— 新生成时调用方要把它打到日志里，
    否则用户无从得知。
    """
    pw = os.environ.get("SORTER_PASSWORD", "").strip()
    if pw:
        return pw, False
    path = work_dir / ".password"
    if path.exists():
        return path.read_text("utf-8").strip(), False
    pw = secrets.token_urlsafe(12)
    path.write_text(pw, "utf-8")
    path.chmod(0o600)
    return pw, True


class Auth:
    def __init__(self, work_dir):
        self._key = load_secret(work_dir)
        self._password, generated = load_password(work_dir)
        # 只在这次新生成时暴露出来，供启动日志打印；否则为 None
        self.initial_password = self._password if generated else None

    def _sign(self, payload):
        return hmac.new(self._key, payload, hashlib.sha256).digest()

    def make_token(self, user):
        body = json.dumps({"u": user, "exp": time.time() + SESSION_HOURS * 3600},
                          separators=(",", ":")).encode()
        return f"{_b64e(body)}.{_b64e(self._sign(body))}"

    def verify_token(self, token):
        """返回用户名；无效/过期返回 None。"""
        if not token or "." not in token:
            return None
        raw, sig = token.rsplit(".", 1)
        try:
            body = _b64d(raw)
            if not hmac.compare_digest(_b64d(sig), self._sign(body)):
                return None
            data = json.loads(body)
        except (ValueError, json.JSONDecodeError):
            return None
        if float(data.get("exp", 0)) < time.time():
            return None
        return data.get("u")

    def locked_for(self, ip):
        """还需锁定多少秒，0 表示未锁定。"""
        with _fails_lock:
            n, last = _fails.get(ip, (0, 0.0))
            if n < FAIL_LIMIT:
                return 0
            left = LOCK_SECONDS - (time.time() - last)
            if left <= 0:
                _fails.pop(ip, None)
                return 0
            return int(left)

    def check(self, ip, user, password):
        """口令正确返回 True，并清掉该 IP 的失败计数。"""
        ok = (hmac.compare_digest(user or "", USER)
              and hmac.compare_digest(password or "", self._password))
        with _fails_lock:
            if ok:
                _fails.pop(ip, None)
            else:
                n, _ = _fails.get(ip, (0, 0.0))
                _fails[ip] = (n + 1, time.time())
        return ok
