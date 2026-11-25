"""Collect the three APIs used by magpie-usage-display, on this computer only."""
from concurrent.futures import ThreadPoolExecutor
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener


class MagpieError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MagpieError("Magpie API 不应重定向")


class MagpieClient:
    def __init__(self, base_url="http://127.0.0.1:3425", timeout=10):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise ValueError("Magpie 地址必须是本机 HTTP 根地址，不含凭据")
        try:
            parsed.port
        except ValueError as error:
            raise ValueError("Magpie 端口无效") from error
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get(self, path):
        request = Request(self.base_url + path, headers={"Accept": "application/json"})
        try:
            # No proxies: loopback data must remain on this computer.
            from urllib.request import ProxyHandler
            opener = build_opener(ProxyHandler({}), NoRedirect())
            with opener.open(request, timeout=self.timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise MagpieError("Magpie 响应超出 2 MiB 上限")
                value = json.loads(raw)
        except (HTTPError, URLError, OSError, ValueError) as error:
            raise MagpieError("无法读取本机 Magpie API") from error
        if not isinstance(value, dict):
            raise MagpieError("Magpie API 响应必须为 JSON 对象")
        return value

    def collect(self):
        if self.get("/").get("name") != "magpie":
            raise MagpieError("本机端点不是 Magpie")
        paths = ("/v1/magpie/quotas", "/v1/magpie/usage", "/v1/magpie/usage/requests?limit=1")
        with ThreadPoolExecutor(max_workers=3) as workers:
            futures = [workers.submit(self.get, path) for path in paths]
            return tuple(future.result() for future in futures)
