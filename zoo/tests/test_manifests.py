import unittest

from tests.helpers import ZooEnv, manifest
from zoolib import manifests


class ValidateTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(manifests.validate(manifest("vless-reality"), "vless-reality"), [])

    def test_errors(self):
        cases = {
            "id": dict(id="Bad_ID"),
            "port": dict(port=70000),
            "layer": dict(layer="sctp"),
            "enabled": dict(enabled="yes"),
            "links": dict(links=[{"user": "owner"}]),
            "probe.kind": dict(probe={"kind": "openvpn"}),
        }
        for field, kw in cases.items():
            errs = manifests.validate(manifest("x", **kw), None)
            self.assertTrue(any(e.startswith(field) for e in errs), (field, errs))

    def test_id_must_match_file(self):
        errs = manifests.validate(manifest("a"), "b")
        self.assertTrue(errs and "не совпадает" in errs[0])

    def test_bool_is_not_port(self):
        self.assertTrue(manifests.validate(manifest("a", port=True), "a"))

    def test_layer_alias_and_services(self):
        m = manifests.from_dict(manifest("ss2022", layer="tcp,udp", service=["x-ui", "other.service"]))
        self.assertEqual(m.layer, "tcp+udp")
        self.assertEqual(m.protos, ["tcp", "udp"])
        self.assertEqual(m.services, ["x-ui", "other.service"])

    def test_disabled_links_hidden(self):
        m = manifests.from_dict(manifest("a", links=[
            {"user": "masha", "uri": "vless://m@1.2.3.4:443", "enabled": False},
            {"user": "owner", "uri": "vless://o@1.2.3.4:443", "enabled": True}],
            files=[{"user": "masha", "path": "/x/masha.conf", "enabled": False}]))
        self.assertEqual(m.links_for("masha"), [])
        self.assertEqual(m.files_for("masha"), [])
        self.assertEqual(m.links_for("masha", enabled_only=False), ["vless://m@1.2.3.4:443"])
        self.assertEqual(m.links_for("owner"), ["vless://o@1.2.3.4:443"])

    def test_users_backend(self):
        self.assertTrue(manifests.from_dict(manifest("a")).has_users)
        self.assertFalse(manifests.from_dict(manifest("a", users_backend="none")).has_users)
        data = manifest("a")
        del data["users_backend"]
        self.assertTrue(manifests.from_dict(data).has_users)


class LoadTest(unittest.TestCase):
    def test_load_all_splits_good_and_bad(self):
        with ZooEnv() as env:
            env.add_manifest("vless-reality")
            env.add_manifest("hysteria2", layer="udp", engine="hysteria", service="hysteria-server")
            (env.etc / "protocols.d" / "broken.json").write_text("{not json", encoding="utf-8")
            (env.etc / "protocols.d" / "wrong.json").write_text('{"id": "other"}', encoding="utf-8")
            (env.etc / "protocols.d" / ".tmp.json").write_text("{}", encoding="utf-8")
            good, bad = manifests.load_all()
            self.assertEqual([m.id for m in good], ["hysteria2", "vless-reality"])
            self.assertEqual(sorted(b.id for b in bad), ["broken", "wrong"])
            m = manifests.get("vless-reality")
            self.assertEqual(m.links_for("owner"), ["vless://owner@1.2.3.4:443#vless-reality"])
            self.assertIsNone(manifests.get("nope"))
            self.assertIsNone(manifests.get("../etc"))

    def test_missing_dir(self):
        with ZooEnv() as env:
            (env.etc / "protocols.d").rmdir()
            self.assertEqual(manifests.load_all(), ([], []))


class ProtoTitleTest(unittest.TestCase):
    def test_one_name_per_protocol_whatever_the_manifest_says(self):
        want = {"hysteria2": "Hysteria2", "hysteria2-obfs": "Hysteria2 + Salamander", "vless-xhttp": "VLESS XHTTP",
                "vless-reality": "VLESS Vision", "amneziawg": "AmneziaWG", "tuic": "TUIC", "ss2022": "Shadowsocks"}
        for pid, title in want.items():
            self.assertEqual(manifests.proto_title(pid), title)
            self.assertEqual(manifests.proto_title(pid, "HY2 + Salamander"), title, "short манифеста — только для незнакомых")
        self.assertEqual(manifests.proto_title("brand-new", "Новый"), "Новый")
        self.assertEqual(manifests.proto_title("brand-new"), "brand-new")


if __name__ == "__main__":
    unittest.main()
