import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.helpers import ZooEnv
from zoolib import config, xui

TOKEN = "t" * 48


class FakePanel(BaseHTTPRequestHandler):
    """Как 3x-ui: без заголовка — 404, неверный Bearer — 401, ответ {success,msg,obj}."""

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        auth = self.headers.get("Authorization")
        if not auth:
            return self._send(404)
        if auth != f"Bearer {TOKEN}":
            return self._send(401)
        if not self.path.startswith("/secret/panel/api/"):
            return self._send(404)
        p = self.path[len("/secret/panel/api/"):]
        if p == "server/status":
            return self._send(200, {"success": True, "msg": "", "obj": {
                "xray": {"state": "running", "version": "26.9.30"}, "panelVersion": "3.9.0"}})
        if p == "clients/list":
            return self._send(200, {"success": True, "msg": "", "obj": [{"email": "owner", "enable": True}]})
        if p.startswith("clients/links/"):
            email = p.rsplit("/", 1)[1]
            return self._send(200, {"success": True, "msg": "", "obj": [
                f"vless://{email}@{self.headers.get('Host')}:443"]})
        return self._send(200, {"success": False, "msg": "not found", "obj": None})


class XuiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakePanel)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def client(self, token=TOKEN, path="/secret/"):
        return xui.XuiClient(self.port, path, token, timeout=5)

    def test_status_and_clients(self):
        c = self.client()
        self.assertEqual(c.server_status()["xray"]["state"], "running")
        self.assertEqual(c.clients()[0]["email"], "owner")

    def test_links_use_host_header_and_quote_email(self):
        links = self.client().client_links("a b@c", "203.0.113.5")
        self.assertEqual(links, ["vless://a%20b%40c@203.0.113.5:443"])

    def test_errors(self):
        with self.assertRaises(xui.XuiError) as cm:
            self.client(token="wrong").server_status()
        self.assertEqual(cm.exception.http_code, 401)
        with self.assertRaises(xui.XuiError) as cm:
            self.client(path="other").server_status()
        self.assertEqual(cm.exception.http_code, 404)
        with self.assertRaises(xui.XuiError) as cm:
            self.client().get("inbounds/get/999")
        self.assertIn("not found", str(cm.exception))

    def test_unreachable(self):
        with self.assertRaises(xui.XuiError):
            xui.XuiClient(1, "", TOKEN, timeout=2).server_status()

    def test_from_config_and_token_file(self):
        with ZooEnv() as env:
            env.write_config({"PANEL_PORT": str(self.port), "PANEL_PATH": "secret", "XUI_API_TOKEN": "old"})
            cfg = config.load()
            self.assertEqual(xui.read_token(cfg=cfg), "old")  # файла заголовка нет
            (env.etc / "xui-auth.hdr").write_text(f"Authorization: Bearer {TOKEN}\n", encoding="utf-8")
            c = xui.XuiClient.from_config(cfg)
            self.assertEqual(c.server_status()["panelVersion"], "3.9.0")
            env.write_config({})
            with self.assertRaises(xui.XuiError):
                xui.XuiClient.from_config(config.load())


if __name__ == "__main__":
    unittest.main()
