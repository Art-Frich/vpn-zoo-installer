import os
import unittest
from pathlib import Path

from tests.helpers import ZooEnv, needs_bash
from zoolib import users

PROTOS = ("vless-reality", "hysteria2", "amneziawg")


@needs_bash
class UsersTest(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        for pid in PROTOS:
            self.env.add_protocol(pid)

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def registry(self):
        return {u["name"]: u for u in self.env.users_json()["users"]}

    # ---------- bootstrap ----------

    def test_bootstrap_adopts_owner(self):
        self.env.add_manifest("tuic")  # нет модуля — пропуск
        self.env.add_protocol("ss2022", users=())  # owner нет в протоколе
        rep = users.bootstrap()
        reg = self.registry()
        self.assertEqual(list(reg), ["owner"])
        self.assertEqual(sorted(reg["owner"]["protocols"]), sorted(PROTOS))
        self.assertIn("tuic", rep.skipped)
        self.assertFalse(rep.ok)  # ss2022 без owner — в отчёте
        self.assertEqual(self.env.users_json()["schema"], users.SCHEMA)
        # повторный вызов идемпотентен
        users.bootstrap()
        self.assertEqual(sorted(self.registry()["owner"]["protocols"]), sorted(PROTOS))

    def test_bootstrap_falls_back_to_manifest_links(self):
        self.env.fail("hysteria2:user_list")
        users.bootstrap()
        self.assertIn("hysteria2", self.registry()["owner"]["protocols"])

    # ---------- служебный пользователь пробника ----------

    def test_probe_user_lifecycle(self):
        self.assertEqual(users.probe_user(), "owner")
        rep = users.ensure_probe_user()
        self.assertTrue(rep.ok, rep.to_dict())
        reg = self.registry()
        self.assertTrue(reg["zoo-probe"]["system"])
        self.assertEqual(sorted(reg["zoo-probe"]["protocols"]), sorted(PROTOS))
        for pid in PROTOS:
            self.assertEqual(self.env.proto_users(pid).get("zoo-probe"), "true")
        self.assertIsNone(users.ensure_probe_user())  # уже есть
        self.assertEqual(users.probe_user(), "zoo-probe")
        self.assertEqual([u.name for u in users.list_users().visible()], ["owner"])
        self.assertIn("zoo-probe", users.hidden_names())
        with self.assertRaises(users.UserError):
            users.add_user("zoo-probe")
        with self.assertRaises(users.UserError):
            users.set_enabled("zoo-probe", False)
        with self.assertRaises(users.UserError) as cm:
            users.delete_user("zoo-probe")
        self.assertIn("--force", str(cm.exception))
        self.assertTrue(users.delete_user("zoo-probe", force=True).ok)
        self.assertEqual(users.probe_user(), "owner")

    def test_probe_user_delete_drops_its_bundle(self):
        users.ensure_probe_user()
        bundle = self.env.etc / "probe-export.json"
        bundle.write_text('{"type": "zoo-probe-export", "user": "zoo-probe"}', encoding="utf-8")
        rep = users.delete_user("zoo-probe", force=True)
        self.assertTrue(rep.ok)
        self.assertFalse(bundle.exists())
        self.assertIn("пакет пробника", rep.message)
        # пакет с кредами другого пользователя не трогаем
        users.add_user("masha")
        bundle.write_text('{"type": "zoo-probe-export", "user": "owner"}', encoding="utf-8")
        users.delete_user("masha")
        self.assertTrue(bundle.exists())

    def test_probe_user_partial(self):
        self.env.fail("hysteria2:user_add")
        rep = users.ensure_probe_user()
        self.assertFalse(rep.ok)
        self.assertEqual(sorted(self.registry()["zoo-probe"]["protocols"]), ["amneziawg", "vless-reality"])
        # протокол починили — sync доводит служебного пользователя, как и остальных
        self.env.fail()
        users.sync_users()
        self.assertIn("hysteria2", self.registry()["zoo-probe"]["protocols"])

    # ---------- add ----------

    def test_add_fans_out(self):
        rep = users.add_user("masha", note="сестра")
        self.assertTrue(rep.ok, rep.to_dict())
        for pid in PROTOS:
            self.assertEqual(self.env.proto_users(pid).get("masha"), "true")
        reg = self.registry()
        self.assertEqual(set(reg), {"owner", "masha"})  # owner создан автоматически
        self.assertEqual(reg["masha"]["note"], "сестра")
        self.assertEqual(sorted(reg["masha"]["protocols"]), sorted(PROTOS))
        self.assertTrue((self.env.etc / "clients" / "masha" / "amneziawg.conf").exists())

    def test_add_rolls_back_on_failure(self):
        users.bootstrap()
        self.env.fail("vless-reality:user_add")  # последний по порядку id
        rep = users.add_user("masha")
        self.assertFalse(rep.ok)
        self.assertTrue(rep.rolled_back)
        for pid in PROTOS:
            self.assertNotIn("masha", self.env.proto_users(pid))
        self.assertNotIn("masha", self.registry())
        self.assertFalse((self.env.etc / "clients" / "masha").exists())  # пустой каталог убран
        actions = [(s.proto_id, s.action, s.ok) for s in rep.steps]
        self.assertEqual(actions, [("amneziawg", "add", True), ("hysteria2", "add", True),
                                   ("vless-reality", "add", False),
                                   ("hysteria2", "rollback", True), ("amneziawg", "rollback", True)])
        self.assertIn("искусственная ошибка", rep.failed[0].error)

    def test_add_cleans_up_half_done_protocol(self):
        # модуль завёл пользователя и упал (так было с jq 1.6 в proto-vless-reality.sh)
        os.environ["FAKE_HALF"] = "hysteria2:user_add"
        rep = users.add_user("masha")
        self.assertFalse(rep.ok)
        for pid in PROTOS:
            self.assertNotIn("masha", self.env.proto_users(pid), pid)
        self.assertIn(("hysteria2", "rollback", True), [(s.proto_id, s.action, s.ok) for s in rep.steps])
        self.assertEqual(rep.message, "пользователь не создан: ошибка в hysteria2")

    def test_add_partial_keeps_successful(self):
        self.env.fail("hysteria2:user_add")
        rep = users.add_user("masha", partial=True)
        self.assertFalse(rep.ok)
        self.assertFalse(rep.rolled_back)
        self.assertEqual(sorted(self.registry()["masha"]["protocols"]), ["amneziawg", "vless-reality"])

    def test_add_adopts_existing_and_only(self):
        self.env.add_proto("vless-reality", users=("owner", "masha"))
        rep = users.add_user("masha", only=["vless-reality", "hysteria2"])
        self.assertTrue(rep.ok)
        self.assertEqual([(s.proto_id, s.action) for s in rep.steps],
                         [("hysteria2", "add"), ("vless-reality", "adopt")])
        self.assertNotIn("masha", self.env.proto_users("amneziawg"))

    def test_add_validation(self):
        for bad in ("", "Masha", "-x", "a b", "a" * 33, "имя", "../x"):
            with self.assertRaises(users.UserError, msg=bad):
                users.add_user(bad)
        users.add_user("masha")
        with self.assertRaises(users.UserError):
            users.add_user("masha")

    def test_add_without_protocols(self):
        for pid in PROTOS:
            (self.env.etc / "protocols.d" / f"{pid}.json").unlink()
        with self.assertRaises(users.UserError):
            users.add_user("masha")

    def test_skips_disabled_and_userless(self):
        self.env.add_protocol("tuic", enabled=False)
        self.env.add_protocol("warp", users_backend="none")
        ids, skipped = users.managed_protocols()
        self.assertEqual(sorted(ids), sorted(PROTOS))
        self.assertEqual(set(skipped), {"tuic", "warp"})

    # ---------- del ----------

    def test_delete(self):
        users.add_user("masha")
        rep = users.delete_user("masha")
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertNotIn("masha", self.registry())
        for pid in PROTOS:
            self.assertNotIn("masha", self.env.proto_users(pid))
        self.assertFalse((self.env.etc / "clients" / "masha").exists())

    def test_delete_owner_refused(self):
        users.bootstrap()
        with self.assertRaises(users.UserError):
            users.delete_user("owner")
        with self.assertRaises(users.UserError):
            users.delete_user("ghost")

    def test_delete_partial_failure_keeps_rest(self):
        users.add_user("masha")
        self.env.fail("hysteria2:user_del")
        rep = users.delete_user("masha")
        self.assertFalse(rep.ok)
        self.assertEqual(self.registry()["masha"]["protocols"], ["hysteria2"])
        self.env.fail()
        self.assertTrue(users.delete_user("masha").ok)
        self.assertNotIn("masha", self.registry())

    def test_delete_already_gone_is_ok(self):
        users.add_user("masha")
        self.env.add_proto("hysteria2", users=("owner",))  # masha пропала из протокола
        rep = users.delete_user("masha")
        self.assertTrue(rep.ok, rep.to_dict())

    def test_delete_force(self):
        users.add_user("masha")
        self.env.fail("hysteria2:user_del")
        rep = users.delete_user("masha", force=True)
        self.assertFalse(rep.ok)
        self.assertNotIn("masha", self.registry())

    # ---------- enable / disable ----------

    def test_disable_enable(self):
        users.add_user("masha")
        self.assertTrue(users.set_enabled("masha", False).ok)
        self.assertFalse(self.registry()["masha"]["enabled"])
        for pid in PROTOS:
            self.assertEqual(self.env.proto_users(pid)["masha"], "false")
        self.assertTrue(users.set_enabled("masha", True).ok)
        for pid in PROTOS:
            self.assertEqual(self.env.proto_users(pid)["masha"], "true")

    def test_disable_rolls_back(self):
        users.add_user("masha")
        self.env.fail("amneziawg:user_enable")
        rep = users.set_enabled("masha", False)
        self.assertFalse(rep.ok)
        self.assertTrue(rep.rolled_back)
        self.assertTrue(self.registry()["masha"]["enabled"])
        for pid in PROTOS:
            self.assertEqual(self.env.proto_users(pid)["masha"], "true")

    # ---------- sync / verify / links ----------

    def test_sync_adds_new_protocol(self):
        users.add_user("masha")
        users.set_enabled("masha", False)
        self.env.add_protocol("tuic", users=("owner",))
        (self.env.etc / "protocols.d" / "hysteria2.json").unlink()
        reports = {r.user: r for r in users.sync_users()}
        self.assertTrue(all(r.ok for r in reports.values()))
        reg = self.registry()
        self.assertIn("tuic", reg["masha"]["protocols"])
        self.assertNotIn("hysteria2", reg["masha"]["protocols"])
        self.assertEqual(self.env.proto_users("tuic")["masha"], "false")
        self.assertIn("tuic", reg["owner"]["protocols"])
        self.assertEqual([s.action for s in reports["owner"].steps if s.proto_id == "tuic"], ["adopt"])

    def test_sync_disabled_user_rolls_back_when_disable_fails(self):
        users.add_user("masha")
        users.set_enabled("masha", False)
        self.env.add_protocol("tuic", users=("owner",))
        self.env.fail("tuic:user_enable")
        rep = {r.user: r for r in users.sync_users()}["masha"]
        self.assertFalse(rep.ok)
        # отключённый пользователь не остаётся включённым в новом протоколе
        self.assertNotIn("masha", self.env.proto_users("tuic"))
        self.assertNotIn("tuic", self.registry()["masha"]["protocols"])

    def test_bad_names_never_reach_modules(self):
        from zoolib import protolib
        for bad in ("../x", "-rf", "a b", "x;id", "", "a" * 40):
            with self.assertRaises(protolib.ProtoError):
                protolib.user_add("vless-reality", bad)
        with self.assertRaises(protolib.ProtoError):
            protolib.user_list("../vless-reality")
        self.assertFalse(any(c.startswith("vless-reality user_add") for c in self.env.calls()))

    def test_verify(self):
        users.add_user("masha")
        self.env.add_proto("hysteria2", users=("owner", "stranger"))
        drift = users.verify()
        self.assertEqual(drift["hysteria2"], {"missing": ["masha"], "extra": ["stranger"], "error": ""})
        self.assertEqual(drift["vless-reality"], {"missing": [], "extra": [], "error": ""})

    def test_links_with_fallback(self):
        users.bootstrap()
        self.env.fail("hysteria2:links")
        links, errors = users.user_links("owner")
        by_proto = {}
        for x in links:
            by_proto.setdefault(x.proto_id, []).append(x.uri)
        self.assertEqual(set(by_proto), set(PROTOS))
        self.assertEqual(by_proto["hysteria2"], ["vless://owner@1.2.3.4:443#hysteria2"])  # из манифеста
        self.assertEqual(errors, {})
        # модуль не ответил и в манифесте пусто → ошибка
        self.env.add_manifest("hysteria2", links=[])
        _, errors = users.user_links("owner")
        self.assertIn("искусственная ошибка links", errors["hysteria2"])

    def test_files_come_from_module_not_dir_scan(self):
        users.add_user("masha")
        links, _ = users.user_links("masha")
        files = [x for x in links if x.kind == "file"]
        self.assertEqual([(x.proto_id, Path(x.uri).name) for x in files], [("amneziawg", "amneziawg.conf")])
        self.assertTrue((self.env.etc / "clients" / "masha" / "amneziawg.key").exists())  # в выдачу не попал
        links, _ = users.user_links("masha", ["vless-reality"])
        self.assertEqual([x.kind for x in links], ["uri"])

    def test_links_without_registry_use_manifests(self):
        self.env.add_manifest("hysteria2-obfs", links=[{"user": "owner", "uri": "hysteria2://obfs"}])
        links, errors = users.user_links("owner")
        self.assertEqual(errors, {})
        self.assertNotIn("hysteria2-obfs", {x.proto_id for x in links})  # ссылки отдаёт модуль hysteria2

    def test_shared_module_manifest(self):
        self.env.add_manifest("hysteria2-obfs", users_backend="hysteria-command")
        self.env.add_manifest("wireguard-extra", users_module="amneziawg")
        ids, skipped = users.managed_protocols()
        self.assertEqual(sorted(ids), sorted(PROTOS))
        self.assertEqual(skipped, {})


if __name__ == "__main__":
    unittest.main()
