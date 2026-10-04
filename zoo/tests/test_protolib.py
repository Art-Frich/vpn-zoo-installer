import unittest
from unittest import mock

from tests.helpers import ZooEnv, needs_bash
from zoolib import protolib


@needs_bash
class ProtolibTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        self.env.add_protocol("vless-reality", users=("owner",))

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_discovery(self):
        self.env.add_proto("hysteria2")
        self.assertEqual(protolib.list_libs(), ["hysteria2", "vless-reality"])
        self.assertTrue(protolib.available("vless-reality"))
        self.assertFalse(protolib.available("tuic"))

    def test_user_cycle(self):
        protolib.user_add("vless-reality", "masha")
        self.assertEqual(protolib.user_list("vless-reality"), ["owner", "masha"])
        protolib.user_enable("vless-reality", "masha", False)
        self.assertEqual(self.env.proto_users("vless-reality")["masha"], "false")
        protolib.user_del("vless-reality", "masha")
        self.assertEqual(protolib.user_list("vless-reality"), ["owner"])

    def test_logs_go_to_stderr(self):
        res = protolib.call("vless-reality", "user_list")
        self.assertEqual(res.stdout.split(), ["owner"])
        self.assertIn("[i] vless-reality: список", res.stderr)

    def test_links_parsing(self):
        links = protolib.links("vless-reality", "owner")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].label, "vless-reality-основная")
        self.assertTrue(links[0].uri.startswith("vless://owner@10.0.0.1:443"))
        self.assertEqual(links[0].proto_id, "vless-reality")

    def test_probe_and_traffic(self):
        p = protolib.probe("vless-reality", "owner")
        self.assertEqual(p["kind"], "xray")
        self.assertEqual(p["outbound"]["user"], "owner")
        rows = protolib.traffic("vless-reality")
        self.assertEqual([(r.user, r.up, r.down) for r in rows], [("owner", 10, 20)])

    def test_manifest_refresh(self):
        protolib.manifest_refresh("vless-reality")
        self.assertTrue((self.env.fake / "vless-reality.refreshed").exists())

    def test_failure_carries_stderr(self):
        self.env.fail("vless-reality:user_add")
        with self.assertRaises(protolib.ProtoError) as cm:
            protolib.user_add("vless-reality", "masha")
        e = cm.exception
        self.assertEqual(e.kind, "failed")
        self.assertEqual(e.rc, 1)
        self.assertIn("искусственная ошибка user_add", e.short())

    def test_missing_lib_and_function(self):
        with self.assertRaises(protolib.ProtoError) as cm:
            protolib.user_add("tuic", "x")
        self.assertEqual(cm.exception.kind, "missing_lib")
        with self.assertRaises(protolib.ProtoError) as cm:
            protolib.call("vless-reality", "no_such_fn")
        self.assertEqual(cm.exception.kind, "missing_fn")

    def test_dashed_function_names(self):
        self.env.add_manifest("vless-xhttp")
        self.env.add_proto("vless-xhttp", dashed=True, users=("owner",))
        self.assertEqual(protolib.user_list("vless-xhttp"), ["owner"])

    def test_timeout(self):
        with self.assertRaises(protolib.ProtoError) as cm:
            protolib.call("vless-reality", "sleepy", timeout=1)
        self.assertEqual(cm.exception.kind, "timeout")

    def test_check_false_returns_rc(self):
        self.env.fail("vless-reality:user_list")
        res = protolib.call("vless-reality", "user_list", check=False)
        self.assertEqual(res.rc, 1)


def _fake_call(stdout):
    return lambda pid, fn, *a, **kw: protolib.ProtoResult(pid, fn, list(a), 0, stdout, "", 0.0)


class ParseTest(unittest.TestCase):
    def test_traffic_formats(self):
        # как у настоящих модулей: shared/scope в extra, user=null у итога inbound
        out = ('{"user":"owner","up":5,"down":7,"shared":true}\n'
               '{"user":null,"up":1,"down":2,"scope":"inbound"}\nмусор\n{"up":1}\n')
        with mock.patch.object(protolib, "call", _fake_call(out)):
            rows = protolib.traffic("x")
        self.assertEqual([(r.user, r.up, r.down, r.extra) for r in rows],
                         [("owner", 5, 7, {"shared": True}), ("", 1, 2, {"scope": "inbound"})])

    def test_links_formats(self):
        out = "vpn://AAAA\n/etc/vpn-setup/clients/x/amneziawg.conf\nметка\tss://k@h:1#x\nпросто текст\n"
        with mock.patch.object(protolib, "call", _fake_call(out)):
            links = protolib.links("amneziawg", "x")
        self.assertEqual([(x.kind, x.label, x.uri) for x in links], [
            ("uri", "", "vpn://AAAA"), ("file", "", "/etc/vpn-setup/clients/x/amneziawg.conf"),
            ("uri", "метка", "ss://k@h:1#x")])
        with mock.patch.object(protolib, "call", _fake_call('["a://1", {"uri": "b://2", "label": "L"}]')):
            self.assertEqual([x.uri for x in protolib.links("x", "x")], ["a://1", "b://2"])

    def test_user_list_formats(self):
        with mock.patch.object(protolib, "call", _fake_call("owner\ttrue\nmasha\tfalse\n")):
            self.assertEqual(protolib.user_list("x"), ["owner", "masha"])
        with mock.patch.object(protolib, "call", _fake_call('[{"email": "a"}, "b"]')):
            self.assertEqual(protolib.user_list("x"), ["a", "b"])

    def test_try_json_embedded(self):
        self.assertEqual(protolib._try_json('шум {"a": {"b": 1}} хвост', embedded=True), {"a": {"b": 1}})
        self.assertIsNone(protolib._try_json("не json"))
        self.assertEqual(protolib._try_json('["a", "b"]'), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
