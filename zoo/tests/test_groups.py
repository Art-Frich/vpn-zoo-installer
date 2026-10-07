import inspect
import itertools
import json
import os
import re
import shutil
import subprocess
import unittest
from unittest import mock

from tests.helpers import BASH, ZooEnv, needs_bash
from tests.test_cli import run_cli
from zoolib import allowlist, clients, groups, paths, protolib, users

PROTOS = ("vless-reality", "hysteria2", "amneziawg")
JQ = shutil.which("jq")


@needs_bash
class GroupsBase(unittest.TestCase):
    def setUp(self):
        self.env = ZooEnv().__enter__()
        for pid in PROTOS:
            self.env.add_protocol(pid)

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def registry(self):
        return {u["name"]: u for u in self.env.users_json()["users"]}

    def groups_json(self):
        return json.loads(paths.groups_file().read_text(encoding="utf-8"))

    def allow_json(self):
        return json.loads(paths.allowlist_file().read_text(encoding="utf-8"))

    def refresh_calls(self):
        return [c for c in self.env.calls() if c == "amneziawg manifest_refresh"]


class MigrationTest(GroupsBase):
    def test_new_protocol_only_for_owner_does_not_make_users_custom(self):
        # протокол включили после установки: фаза завела только owner, затем 09 (zoo setup) мигрирует
        users.bootstrap()
        users.add_user("masha")
        users.add_user("petya", only=["amneziawg"])
        self.env.add_protocol("tuic", users=("owner",))
        code, _, err = run_cli("setup")
        self.assertEqual(code, 0, err)
        reg = self.registry()
        self.assertIn("tuic", reg["owner"]["protocols"])
        self.assertNotIn("custom", reg["masha"], "без tuic она только потому, что он новый")
        self.assertTrue(reg["petya"]["custom"], "а сознательно урезанный набор остаётся своим")
        users.sync_users()
        reg = self.registry()
        self.assertIn("tuic", reg["masha"]["protocols"], "Основная довела новый протокол")
        self.assertNotIn("tuic", reg["petya"]["protocols"])

    def test_sync_does_not_extend_custom_users_without_flag(self):
        users.bootstrap()
        groups.ensure()
        users.add_user("petya", only=["amneziawg"])
        self.assertTrue(self.registry()["petya"]["custom"])
        self.env.add_protocol("tuic", users=("owner",))
        rep = {r.user: r for r in users.sync_users()}
        self.assertEqual(self.registry()["petya"]["protocols"], ["amneziawg"])
        self.assertIn("tuic", rep["petya"].skipped)
        self.assertIn("tuic", self.registry()["owner"]["protocols"])
        self.assertEqual(rep["petya"].message, "без изменений")
        users.sync_users(include_custom=True)
        self.assertIn("tuic", self.registry()["petya"]["protocols"])
        self.assertTrue(self.registry()["petya"]["custom"], "набор остаётся «своим»")

    def test_cli_sync_flag(self):
        users.bootstrap()
        groups.ensure()
        users.add_user("petya", only=["amneziawg"])
        self.env.add_protocol("tuic", users=("owner",))
        code, out, err = run_cli("user", "sync")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.registry()["petya"]["protocols"], ["amneziawg"])
        code, out, err = run_cli("user", "sync", "--include-custom")
        self.assertEqual(code, 0, err)
        self.assertIn("tuic", self.registry()["petya"]["protocols"])
    def test_existing_users_go_to_main_with_current_settings(self):
        users.bootstrap()
        users.add_user("masha")
        users.add_user("petya", only=["amneziawg"])
        self.assertFalse(paths.groups_file().exists())
        gs = groups.ensure()
        self.assertEqual([(g.id, g.name, g.protocols) for g in gs.groups], [("main", "Основная", ["*"])])
        reg = self.registry()
        self.assertEqual({n: reg[n]["group"] for n in ("owner", "masha", "petya")}, {n: "main" for n in ("owner", "masha", "petya")})
        self.assertNotIn("custom", reg["masha"])
        self.assertTrue(reg["petya"]["custom"], "набор протоколов не как у группы — свой")
        self.assertEqual(sorted(reg["petya"]["protocols"]), ["amneziawg"])

    def edit_registry(self, **per_user):
        data = json.loads(paths.users_file().read_text(encoding="utf-8"))
        for u in data["users"]:
            u.update(per_user.get(u["name"], {}))
        paths.users_file().write_text(json.dumps(data), encoding="utf-8")

    def test_custom_flags_recomputed_once_for_servers_migrated_by_old_code(self):
        users.bootstrap()
        users.add_user("masha")
        users.add_user("petya", only=["amneziawg"])
        groups.ensure()
        self.env.add_protocol("tuic", users=("owner",))
        # прежний код: у masha ошибочно custom (нового tuic у неё нет), метки в groups.json нет
        self.edit_registry(masha={"custom": True})
        data = self.groups_json()
        data.pop("custom_recomputed", None)
        paths.groups_file().write_text(json.dumps(data), encoding="utf-8")
        self.assertTrue(self.registry()["masha"]["custom"])
        groups.ensure()
        reg = self.registry()
        self.assertNotIn("custom", reg["masha"], "набор как у группы без нового tuic — не свой")
        self.assertTrue(reg["petya"]["custom"], "сознательно урезанный набор остаётся своим")
        self.assertEqual(self.groups_json().get("custom_recomputed"), 1)
        # один раз: позже ручной custom не сбрасывается пересчётом
        self.edit_registry(masha={"custom": True})
        groups.ensure()
        self.assertTrue(self.registry()["masha"]["custom"])

    def test_recompute_flips_wrong_non_custom_and_skips_other_groups(self):
        users.bootstrap()
        users.add_user("masha", only=["vless-reality"])
        users.add_user("vasya", only=["vless-reality"])
        users.add_user("petya")   # держит все протоколы: они не «новые только у владельца»
        groups.ensure()
        g = groups.create("Семья", ["vless-reality"])
        groups.move_many(["vasya"], g.id)
        self.edit_registry(masha={"custom": False}, vasya={"custom": True})
        data = self.groups_json()
        data.pop("custom_recomputed", None)
        paths.groups_file().write_text(json.dumps(data), encoding="utf-8")
        groups.ensure()
        reg = self.registry()
        self.assertTrue(reg["masha"]["custom"], "урезанный набор в Основной — свой")
        self.assertTrue(reg["vasya"]["custom"], "участников других групп пересчёт не трогает")

    def test_fresh_install_marks_recompute_done(self):
        users.bootstrap()
        groups.ensure()
        self.assertEqual(self.groups_json().get("custom_recomputed"), 1)

    def test_sync_always_gives_owner_all_protocols(self):
        users.bootstrap()
        groups.ensure()
        g = groups.create("Узкая", ["amneziawg"])
        groups.move_many(["owner"], g.id)
        self.env.add_protocol("tuic", users=())
        users.sync_users()
        self.assertIn("tuic", self.registry()["owner"]["protocols"], "владелец вне группы-«всех» всё равно получает всё")
        self.env.add_protocol("tuic2", users=())
        self.edit_registry(owner={"custom": True})
        users.sync_users()
        self.assertIn("tuic2", self.registry()["owner"]["protocols"], "custom у владельца sync не пропускает")

    def test_idempotent_and_leaves_system_user_alone(self):
        users.bootstrap()
        users.ensure_probe_user()
        groups.ensure()
        first = (paths.groups_file().read_text(encoding="utf-8"), paths.users_file().read_text(encoding="utf-8"))
        groups.ensure()
        groups.ensure()
        self.assertEqual(first, (paths.groups_file().read_text(encoding="utf-8"),
                                 paths.users_file().read_text(encoding="utf-8")))
        self.assertNotIn("group", self.registry()["zoo-probe"])
        if os.name == "posix":
            self.assertEqual(paths.groups_file().stat().st_mode & 0o777, 0o600)

    def test_without_registry_nothing_is_written(self):
        groups.ensure()
        self.assertFalse(paths.groups_file().exists())

    def test_new_users_after_migration_land_in_main(self):
        users.bootstrap()
        groups.ensure()
        users.add_user("masha")
        self.assertEqual(self.registry()["masha"]["group"], "main")

    def test_deleted_main_is_not_resurrected(self):
        users.bootstrap()
        groups.ensure()
        g = groups.create("Семья", ["amneziawg"])
        groups.move_many(["owner"], g.id)
        groups.remove("main")
        groups.ensure()
        self.assertEqual([x["id"] for x in self.groups_json()["groups"]], [g.id])
        users.add_user("masha")
        self.assertNotIn("group", self.registry()["masha"])

    def test_setup_command_migrates(self):
        users.bootstrap()
        code, out, err = run_cli("setup")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.registry()["owner"]["group"], "main")


class ModelTest(GroupsBase):
    def setUp(self):
        super().setUp()
        users.bootstrap()
        groups.ensure()

    def test_create_validates(self):
        for bad_name in ("", "   ", "x" * 41):
            with self.assertRaises(groups.GroupError, msg=bad_name):
                groups.create(bad_name, ["amneziawg"])
        with self.assertRaises(groups.GroupError):
            groups.create("основная", ["amneziawg"])  # имя занято, регистр не важен
        with self.assertRaises(groups.GroupError):
            groups.create("Семья", [])
        with self.assertRaises(groups.GroupError):
            groups.create("Семья", ["nope"])
        with self.assertRaises(groups.GroupError):
            groups.create("Семья", ["amneziawg"], {"android": "ghost"})
        with self.assertRaises(groups.GroupError):  # AmneziaWG-клиент не умеет VLESS
            groups.create("Семья", ["vless-reality"], {"android": "amneziawg"})
        with self.assertRaises(groups.GroupError):
            groups.create("Семья", ["amneziawg"], allow={"android": ["com.a.b"], "windows": []})
        with self.assertRaises(groups.GroupError):
            groups.create("Семья", ["amneziawg"], allow={"android": ["bad id"], "windows": ["a.exe"]})
        self.assertEqual([g["id"] for g in self.groups_json()["groups"]], ["main"], "ошибки ничего не записали")
        with self.assertRaises(groups.GroupError):  # один из набора не умеет ни одного выбранного протокола
            groups.create("Семья", ["amneziawg"], {"android": ["amneziawg", "happ"]})
        with self.assertRaises(groups.GroupError):
            groups.create("Семья", ["amneziawg"], {"android": ["amneziawg", "ghost"]})
        with self.assertRaises(groups.GroupError):
            groups.create("Семья", ["amneziawg"], {"android": ["amneziawg"] * 2 + ["wgtunnel", "amneziavpn", "v2rayng",
                                                                                     "happ", "incy", "singbox"]})
        g = groups.create("  Семья \n", ["hysteria2", "amneziawg"], {"android": ["happ", "amneziawg", "happ"], "ios": ""})
        self.assertEqual((g.id, g.name, g.protocols, g.clients), ("g1", "Семья", ["hysteria2", "amneziawg"],
                                                                 {"android": ["happ", "amneziawg"]}))
        g2 = groups.create("Старый вид", ["hysteria2"], {"android": "happ", "windows": []})
        self.assertEqual(g2.clients, {"android": ["happ"]}, "строка — набор из одного клиента")
        self.assertEqual(groups.Groups.load().next_name(), "Группа 4")
        self.assertEqual(groups.Groups.load().get("семья").id, "g1")

    def test_user_gets_group_protocols(self):
        g = groups.create("Телефон", ["hysteria2", "amneziawg"])
        rep = users.add_user("masha", group=g.id)
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual(sorted(self.registry()["masha"]["protocols"]), ["amneziawg", "hysteria2"])
        self.assertNotIn("masha", self.env.proto_users("vless-reality"))
        self.assertEqual(self.registry()["masha"]["group"], "g1")
        self.assertNotIn("custom", self.registry()["masha"])
        # по названию; свой --proto делает набор «своим»
        rep = users.add_user("petya", group="телефон", only=["vless-reality"])
        self.assertEqual(self.registry()["petya"]["protocols"], ["vless-reality"])
        self.assertTrue(self.registry()["petya"]["custom"])
        with self.assertRaises(users.UserError):
            users.add_user("kolya", group="нет такой")
        self.assertNotIn("kolya", self.registry())

    def test_update_applies_to_members_once(self):
        g = groups.create("Телефон", ["hysteria2", "amneziawg"])
        users.add_user("masha", group=g.id)
        users.add_user("petya", group=g.id, only=["vless-reality"])  # свой набор
        before = len(self.refresh_calls())
        rep = groups.update(g.id, protocols=["vless-reality", "amneziawg"])
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual(sorted(self.registry()["masha"]["protocols"]), ["amneziawg", "vless-reality"])
        self.assertNotIn("masha", self.env.proto_users("hysteria2"))
        self.assertEqual(self.registry()["petya"]["protocols"], ["vless-reality"], "свой набор не тронут")
        self.assertEqual(rep.skipped, ["petya"])
        self.assertEqual(rep.needs_qr, ["masha"])
        self.assertEqual(len(self.refresh_calls()), before, "протоколы без приложений: файлы AWG не пересобирались")
        # смена названия и клиентов — без пересборки и без новых QR
        rep = groups.update(g.id, name="Мобильные", clients={"android": ["happ", "amneziawg"]})
        self.assertEqual((rep.needs_qr, rep.skipped), ([], []))
        self.assertEqual(self.groups_json()["groups"][1]["name"], "Мобильные")
        self.assertEqual(self.groups_json()["groups"][1]["clients"], {"android": ["happ", "amneziawg"]})
        with self.assertRaises(groups.GroupError):
            groups.update(g.id, name="Основная")
        with self.assertRaises(groups.GroupError):
            groups.update(g.id, protocols=["nope"])
        self.assertEqual(self.groups_json()["groups"][1]["protocols"], ["amneziawg", "vless-reality"])

    def test_group_allowlist_and_user_override(self):
        g = groups.create("Семья", ["amneziawg", "vless-reality"])
        users.add_user("masha", group=g.id)
        users.add_user("petya")
        own = {"android": ["com.whatsapp"], "windows": ["Discord.exe"]}
        before = len(self.refresh_calls())
        rep = groups.update(g.id, allow=own)
        self.assertEqual(rep.needs_qr, ["masha"], "список группы изменил только её участника")
        self.assertEqual(len(self.refresh_calls()), before + 1, "AmneziaWG пересобран один раз")
        self.assertEqual(rep.allow["amneziawg"], "пересобран")
        al = allowlist.Allowlist.load()
        self.assertEqual(al.effective("android", "masha"), ["com.whatsapp"])
        self.assertEqual(al.effective("windows", "masha"), ["Discord.exe"])
        self.assertEqual(al.effective("android", "petya"), al.android, "вне группы — общий список")
        data = self.allow_json()
        self.assertEqual(data["groups"], {"g1": own})
        self.assertEqual(data["members"], {"masha": "g1"})
        rules = json.loads(allowlist.user_file("masha").read_text(encoding="utf-8"))
        self.assertEqual(rules[3]["process"], ["Discord.exe"])
        # свой список пользователя сильнее списка группы; совпал со списком группы — снова «как у группы»
        allowlist.set_lists({"android": ["com.example.app"], "windows": ["Discord.exe"]}, user="masha")
        self.assertEqual(allowlist.Allowlist.load().effective("android", "masha"), ["com.example.app"])
        allowlist.set_lists(own, user="masha")
        al = allowlist.Allowlist.load()
        self.assertFalse(al.own("masha"))
        self.assertEqual(al.effective("android", "masha"), ["com.whatsapp"])
        # группа обратно на общий список
        rep = groups.update(g.id, allow=None)
        self.assertEqual(rep.needs_qr, ["masha"])
        self.assertNotIn("groups", self.allow_json())
        self.assertEqual(allowlist.Allowlist.load().effective("android", "masha"), al.android)

    def test_new_user_born_with_group_allowlist(self):
        g = groups.create("Семья", ["amneziawg"], allow={"android": ["com.whatsapp"], "windows": ["Discord.exe"]})
        self.assertEqual(self.allow_json()["groups"]["g1"]["android"], ["com.whatsapp"])
        users.add_user("masha", group=g.id)
        self.assertEqual(self.allow_json()["members"], {"masha": "g1"})
        rules = json.loads(allowlist.user_file("masha").read_text(encoding="utf-8"))
        self.assertEqual(rules[3]["process"], ["Discord.exe"])
        # неудача создания убирает пользователя и из зеркала
        self.env.fail("amneziawg:user_add")
        rep = users.add_user("petya", group=g.id)
        self.assertFalse(rep.ok)
        self.assertEqual(self.allow_json()["members"], {"masha": "g1"})
        # удаление пользователя забывает его в зеркале
        self.env.fail()
        users.delete_user("masha")
        self.assertEqual(self.allow_json()["members"], {})

    def test_move_and_reset(self):
        a = groups.create("Семья", ["amneziawg"], allow={"android": ["com.whatsapp"], "windows": ["Discord.exe"]})
        b = groups.create("Друзья", ["hysteria2", "vless-reality"])
        users.add_user("masha", group=a.id)
        rep = groups.move_many(["masha"], b.id)
        self.assertEqual((rep.moved, rep.needs_qr), (["masha"], ["masha"]))
        self.assertEqual(sorted(self.registry()["masha"]["protocols"]), ["hysteria2", "vless-reality"])
        self.assertEqual(self.registry()["masha"]["group"], b.id)
        self.assertNotIn("masha", self.env.proto_users("amneziawg"))
        self.assertEqual(self.allow_json()["members"], {}, "новая группа на общем списке")
        # «свой» набор возвращается к группе тем же переводом
        users.apply_protocols({"masha": ["hysteria2"]})
        reg = users.list_users()
        reg.require("masha").custom = True
        reg.save()
        rep = groups.move_many(["masha"], b.id)
        self.assertEqual(sorted(self.registry()["masha"]["protocols"]), ["hysteria2", "vless-reality"])
        self.assertNotIn("custom", self.registry()["masha"])
        for bad in (["ghost"], ["zoo-probe"]):
            with self.assertRaises(groups.GroupError):
                groups.move_many(bad, b.id)
        with self.assertRaises(groups.GroupError):
            groups.move_many(["masha"], "нет")

    def test_remove_only_empty(self):
        g = groups.create("Семья", ["amneziawg"])
        users.add_user("masha", group=g.id)
        with self.assertRaises(groups.GroupError) as cm:
            groups.remove(g.id)
        self.assertIn("masha", str(cm.exception))
        groups.move_many(["masha"], "main")
        groups.remove("Семья")
        self.assertEqual([x["id"] for x in self.groups_json()["groups"]], ["main"])

    def test_sync_follows_group_protocols(self):
        g = groups.create("Телефон", ["hysteria2"])
        users.add_user("masha", group=g.id)
        users.add_user("petya")  # Основная: все протоколы
        self.env.add_protocol("tuic", users=("owner", "masha", "petya"))
        reports = {r.user: r for r in users.sync_users()}
        self.assertEqual(self.registry()["masha"]["protocols"], ["hysteria2"], "tuic в группу не входил")
        self.assertIn("tuic", self.registry()["petya"]["protocols"])
        self.assertIn("tuic", self.registry()["owner"]["protocols"])
        self.assertEqual(reports["masha"].message, "без изменений")

    def test_partial_failure_is_reported(self):
        g = groups.create("Телефон", ["hysteria2"])
        users.add_user("masha", group=g.id)
        self.env.fail("amneziawg:user_add")
        rep = groups.update(g.id, protocols=["hysteria2", "amneziawg"])
        self.assertFalse(rep.ok)
        self.assertTrue(any("amneziawg" in e for e in rep.errors), rep.errors)
        self.assertEqual(self.registry()["masha"]["protocols"], ["hysteria2"])

    def test_add_members_and_connect(self):
        with self.assertRaises(groups.GroupError):
            groups.connect("Семья", ["amneziawg"], {}, None, [], [])
        with self.assertRaises(groups.GroupError):
            groups.connect("Семья", ["amneziawg"], {}, None, [("Bad Name", "")], [])
        with self.assertRaises(groups.GroupError):
            groups.connect("Семья", ["amneziawg"], {}, None, [("owner", "")], [])
        with self.assertRaises(groups.GroupError):
            groups.connect("Семья", ["amneziawg"], {}, None, [("a", ""), ("a", "")], [])
        with self.assertRaises(groups.GroupError):
            groups.connect("Семья", ["amneziawg"], {}, None, [], ["ghost"])
        self.assertEqual(len(self.groups_json()["groups"]), 1, "проверка до первого изменения")
        users.add_user("petya")
        rep = groups.connect("Семья", ["hysteria2", "amneziawg"], {"android": "happ"},
                             {"android": ["com.whatsapp"], "windows": ["Discord.exe"]},
                             groups.parse_new_users("masha; сестра Маша\nkolya"), ["petya"])
        self.assertTrue(rep.ok, rep.errors)
        self.assertEqual((rep.created, rep.moved), (["masha", "kolya"], ["petya"]))
        self.assertEqual(sorted(rep.needs_qr), ["kolya", "masha", "petya"])
        reg = self.registry()
        self.assertEqual(reg["masha"]["note"], "сестра Маша")
        for n in ("masha", "kolya", "petya"):
            self.assertEqual((reg[n]["group"], sorted(reg[n]["protocols"])), ("g1", ["amneziawg", "hysteria2"]))
        self.assertEqual(self.allow_json()["members"], {"masha": "g1", "kolya": "g1", "petya": "g1"})
        self.assertEqual(self.registry()["owner"]["group"], "main")

    def test_connect_crash_without_members_removes_empty_group(self):
        with mock.patch.object(groups, "add_members", side_effect=RuntimeError("boom")):
            rep = groups.connect("Семья", ["amneziawg"], {}, None, [("masha", "")], [])
        self.assertTrue(rep.crashed and rep.removed)
        self.assertFalse(rep.ok)
        self.assertIn("boom", " ".join(rep.errors))
        self.assertEqual([g["id"] for g in self.groups_json()["groups"]], ["main"], "пустая группа убрана")
        rep = groups.connect("Семья", ["amneziawg"], {}, None, [("masha", "")], [])  # повтор с тем же названием
        self.assertTrue(rep.ok, rep.errors)
        self.assertEqual(rep.created, ["masha"])

    def test_connect_crash_midway_reports_what_was_applied(self):
        with mock.patch.object(groups, "move_many", side_effect=RuntimeError("упал перенос")):
            rep = groups.connect("Семья", ["amneziawg"], {}, None, [("masha", "")], ["owner"])
        self.assertTrue(rep.crashed)
        self.assertFalse(rep.removed, "masha уже создана: группа остаётся")
        self.assertEqual((rep.created, rep.moved), (["masha"], []))
        self.assertEqual(rep.needs_qr, ["masha"])
        self.assertIn("упал перенос", " ".join(rep.errors))
        self.assertEqual(self.registry()["masha"]["group"], "g1")
        self.assertEqual([g["id"] for g in self.groups_json()["groups"]], ["main", "g1"])

    def test_connect_failure_keeps_group_and_reports(self):
        self.env.fail("amneziawg:user_add")
        rep = groups.connect("Семья", ["amneziawg"], {}, None, [("masha", "")], [])
        self.assertFalse(rep.ok)
        self.assertEqual(rep.created, [])
        self.assertTrue(rep.errors)
        self.assertEqual([g["id"] for g in self.groups_json()["groups"]], ["main", "g1"])
        self.assertNotIn("masha", self.registry())

    def test_parse_new_users(self):
        self.assertEqual(groups.parse_new_users("  Masha ; сестра \n\n kolya\n"), [("masha", "сестра"), ("kolya", "")])
        self.assertEqual(groups.parse_new_users("a\na\nowner"), [("a", ""), ("a-2", ""), ("owner-2", "")])
        with self.assertRaises(groups.GroupError):
            groups.parse_new_users("\n".join(f"u{i}" for i in range(groups.NEW_USERS_MAX + 1)))
        with self.assertRaises(groups.GroupError):
            groups.parse_new_users("***")

    def test_client_options(self):
        cat = clients.load()
        opts = groups.client_options(cat, "android", ["vless-reality", "hysteria2"])
        self.assertEqual(opts[0]["client"]["id"], "happ")
        self.assertEqual(opts[0]["covers"], ["vless-reality", "hysteria2"])
        self.assertTrue(opts[0]["recommended"] and not any(o["recommended"] for o in opts[1:]))
        names = [o["client"]["id"] for o in groups.client_options(cat, "android", ["vless-reality"])]
        self.assertNotIn("hiddify", names, "sing-box-клиенты REALITY не проходят")
        self.assertNotIn("karing", names)
        only_awg = [o["client"]["id"] for o in groups.client_options(cat, "android", ["amneziawg"])]
        self.assertEqual(only_awg[0], "amneziawg")
        self.assertIn("wgtunnel", only_awg)
        self.assertEqual(groups.client_options(cat, "macos", ["vless-reality"]), [])
        defaults = groups.default_clients(cat, ["vless-reality"])
        self.assertEqual(defaults["android"], ["happ"])
        self.assertNotIn("macos", defaults)

    def test_ios_default_covers_everything_with_two_apps(self):
        cat = clients.load()
        proto3 = ["vless-reality", "hysteria2", "amneziawg"]
        opts = groups.client_options(cat, "ios", proto3)
        ids = [o["client"]["id"] for o in opts]
        self.assertEqual(ids[0], "incy", "в client_options российский магазин идёт первым")
        self.assertTrue(next(o for o in opts if o["client"]["id"] == "happ")["no_ru_store"])
        ios = groups.default_clients(cat, proto3)["ios"]
        self.assertEqual(ios, ["happ", "amneziavpn"], "покрытие выше магазина РФ, не больше двух приложений")
        self.assertEqual(groups.coverage(cat, "ios", proto3, ios)[1], [], "набор покрывает все три протокола")
        both = groups.default_clients(cat, ["vless-reality", "hysteria2"])["ios"]
        self.assertEqual(both, ["happ"], "Happ покрывает оба протокола одним приложением")
        self.assertEqual(groups.default_clients(cat, ["vless-reality"])["ios"], ["incy"], "один протокол — приложение из РФ-магазина")
        # на Android Happ остаётся первым: он есть в Google Play и на GitHub
        self.assertEqual(groups.default_clients(cat, ["vless-reality", "hysteria2"])["android"], ["happ"])

    def test_suggest_covers_all_default_protocols(self):
        cat = clients.load()
        # Happ/v2rayNG умеют VLESS и Hysteria2, но не AmneziaWG: нужен ещё один клиент
        proto3 = ["hysteria2", "vless-xhttp", "amneziawg"]
        android = groups.suggest_clients(cat, "android", proto3)
        self.assertEqual(android, ["happ", "amneziawg"], "первым — клиент основного протокола, минимум приложений")
        self.assertEqual(groups.coverage(cat, "android", proto3, android), (proto3, []))
        self.assertEqual(len(android), 2, "лишнего третьего приложения нет")
        ios = groups.suggest_clients(cat, "ios", proto3)
        self.assertNotIn("happ", ios)
        self.assertIn("incy", ios)
        self.assertEqual(groups.coverage(cat, "ios", proto3, ios)[1], [])
        win = groups.suggest_clients(cat, "windows", proto3)
        self.assertEqual(win, ["v2rayn", "amneziavpn"])
        # один протокол — одно приложение; протокол без клиента на платформе — набор без него
        self.assertEqual(groups.suggest_clients(cat, "android", ["amneziawg"]), ["amneziawg"])
        self.assertEqual(groups.suggest_clients(cat, "macos", ["vless-reality", "amneziawg"]), ["amneziavpn"])
        self.assertEqual(groups.coverage(cat, "macos", ["vless-reality", "amneziawg"], ["amneziavpn"]),
                         (["amneziawg"], ["vless-reality"]))
        self.assertEqual(groups.suggest_clients(cat, "android", []), [])
        self.assertEqual(groups.coverage(cat, "android", proto3, ["happ"])[1], ["amneziawg"])
        self.assertEqual(groups.coverage(cat, "android", proto3, [])[1], proto3)

    def test_hysteria2_group_gets_one_app_on_android_and_desktop(self):
        cat = clients.load()
        got = groups.suggest_set(cat, ["android", "windows"], ["hysteria2"])
        self.assertEqual(got, {"android": ["hiddify"], "windows": ["hiddify"]})
        head, line = groups.apps_summary(cat, got)
        self.assertEqual(head, "Одно приложение на всех устройствах: Hiddify")
        self.assertEqual(line, "")
        # все пять устройств: Hiddify везде; на iPhone sing-box (SFI) не из магазина, поэтому и там Hiddify (App Store США)
        every = groups.default_clients(cat, ["hysteria2"])
        self.assertEqual(every, {p: ["hiddify"] for p in ("android", "ios", "windows", "macos", "linux")})
        head, line = groups.apps_summary(cat, every)
        self.assertEqual(head, "Одно приложение на всех устройствах: Hiddify")
        self.assertEqual(line, "")
        # Salamander и TUIC тот же Hiddify тоже покрывает
        for protos in (["hysteria2", "hysteria2-obfs"], ["hysteria2", "tuic", "ss2022"]):
            self.assertEqual(groups.suggest_set(cat, ["android", "windows"], protos),
                             {"android": ["hiddify"], "windows": ["hiddify"]}, protos)
        # старый подбор по одной платформе общих приложений не ищет
        self.assertEqual(groups.suggest_clients(cat, "android", ["hysteria2"]), ["happ"])
        self.assertEqual(groups.suggest_clients(cat, "android", ["hysteria2"], prefer={"hiddify"}), ["hiddify"])
        opts = groups.client_options(cat, "android", ["hysteria2"], prefer={"hiddify"})
        self.assertLess([o["client"]["id"] for o in opts].index("hiddify"), [o["client"]["id"] for o in opts].index("singbox"))

    def test_vless_is_never_given_to_sing_box_clients(self):
        cat = clients.load()
        for protos in (["vless-reality"], ["vless-xhttp"], ["vless-reality", "vless-xhttp"]):
            for plat, ids in groups.default_clients(cat, protos).items():
                engines = {cat.client(i)["engine"] for i in ids}
                self.assertNotIn("sing-box", engines, f"{plat} {protos}: sing-box не проходит REALITY у Xray 26.9.30")

    def test_group_with_amneziawg_keeps_an_awg_app_on_every_device(self):
        cat = clients.load()
        protos = ["hysteria2", "amneziawg"]
        got = groups.default_clients(cat, protos)
        self.assertEqual(sorted(got), ["android", "ios", "linux", "macos", "windows"])
        awg_capable = {c["id"] for c in cat.clients if c["protocols"].get("amneziawg", {}).get("s") in ("ok", "warn")}
        for plat, ids in got.items():
            self.assertTrue(awg_capable & set(ids), f"{plat}: нет приложения для AmneziaWG: {ids}")
            self.assertEqual(groups.coverage(cat, plat, protos, ids)[1], [], plat)
        # на Android — AmneziaWG (приложения из .conf, нет бага AmneziaVPN после Doze), а не AmneziaVPN ради «одного»
        self.assertEqual(got["android"], ["hiddify", "amneziawg"])
        self.assertEqual(got["windows"], ["hiddify", "amneziavpn"])
        self.assertEqual(got["ios"], ["singbox", "amneziavpn"])
        # только AmneziaWG
        only = groups.default_clients(cat, ["amneziawg"])
        self.assertEqual(only["android"], ["amneziawg"])
        self.assertEqual({only[p][0] for p in ("ios", "windows", "macos", "linux")}, {"amneziavpn"})

    def test_legacy_sets_when_nothing_can_be_shared_better(self):
        cat = clients.load()
        self.assertEqual(groups.default_clients(cat, ["vless-reality"]),
                         {"android": ["happ"], "ios": ["incy"], "windows": ["v2rayn"], "linux": ["v2rayn"]})
        self.assertEqual(groups.default_clients(cat, ["vless-reality", "hysteria2"])["android"], ["happ"])
        self.assertEqual(groups.default_clients(cat, ["vless-reality", "hysteria2"])["windows"], ["v2rayn"])
        self.assertEqual(groups.suggest_set(cat, ["android"], []), {})
        self.assertEqual(groups.suggest_set(cat, [], ["hysteria2"]), {})
        head, line = groups.apps_summary(cat, {"android": ["happ"], "ios": ["incy"], "windows": ["v2rayn"]})
        self.assertEqual((head, line), ("", ""), "ничего общего — сказать нечего")
        self.assertEqual(groups.apps_summary(cat, {"android": ["happ"]}), ("", ""))

    def test_shared_set_is_never_worse_per_device_than_per_platform_set(self):
        """Режим «ставит ИТ»: приложений не больше, чем у подбора по платформе, охват тот же; если подбору по платформе
        нужно больше MAX_APPS приложений, набор — лучшие два и честное «без X»."""
        cat = clients.load()
        for r in (1, 2, 3):
            for protos in itertools.combinations(groups.PRIORITY, r):
                got = groups.default_clients(cat, list(protos), "admin")
                for plat in cat.platforms:
                    old = groups.suggest_clients(cat, plat, list(protos))
                    new = got.get(plat, [])
                    self.assertEqual(bool(new), bool(old), (plat, protos))
                    self.assertLessEqual(len(new), max(len(old), 0) if len(old) <= groups.MAX_APPS else groups.MAX_APPS,
                                         (plat, protos, new, old))
                    if len(old) <= groups.MAX_APPS:
                        self.assertEqual(groups.coverage(cat, plat, list(protos), new)[1],
                                         groups.coverage(cat, plat, list(protos), old)[1], (plat, protos, new, old))

    def test_legacy_string_client_loads_as_one_item_set_and_saves_as_list(self):
        users.bootstrap()
        data = self.groups_json()
        data["groups"].append({"id": "old", "name": "Старая", "protocols": ["hysteria2"],
                               "clients": {"android": "happ", "ios": "", "windows": ["v2rayn", "v2rayn", ""]},
                               "allowlist": None})
        paths.groups_file().write_text(json.dumps(data), encoding="utf-8")
        gs = groups.Groups.load()
        self.assertEqual(gs.get("old").clients, {"android": ["happ"], "windows": ["v2rayn"]})
        # идемпотентно: сохранённое читается так же, повторное сохранение ничего не меняет
        gs.save()
        saved = self.groups_json()["groups"][-1]["clients"]
        self.assertEqual(saved, {"android": ["happ"], "windows": ["v2rayn"]})
        groups.Groups.load().save()
        self.assertEqual(self.groups_json()["groups"][-1]["clients"], saved)
        self.assertEqual(groups.Groups.load().get("old").to_dict()["clients"], saved)
        # сохранение без правок клиентов (update без clients) не ломает старую запись
        groups.update("old", name="Старая 2")
        self.assertEqual(self.groups_json()["groups"][-1]["clients"], saved)

    def test_messages_clean_roundtrip_and_survive_update(self):
        users.bootstrap()
        self.assertEqual(groups.clean_message("  a \r\nb\t\x01c \n\n"), "a\nb  c")
        g = groups.create("Семья", ["vless-reality"], {"android": ["happ"]})
        self.assertNotIn("messages", self.groups_json()["groups"][-1], "пустых текстов в файле нет")
        groups.set_message(g.id, "android", "{name}, привет\n\n")
        self.assertEqual(groups.Groups.load().get(g.id).messages, {"android": "{name}, привет"})
        self.assertEqual(groups.Groups.load().get(g.id).to_dict()["messages"],
                         {"android": {"text": "{name}, привет", "sig": None}})
        groups.update(g.id, name="Семья 2", protocols=["vless-reality", "hysteria2"], clients={"android": ["happ"]})
        self.assertEqual(groups.Groups.load().get(g.id).messages, {"android": "{name}, привет"}, "update текст не трогает")
        groups.set_message(g.id, "android", None)
        self.assertEqual(groups.Groups.load().get(g.id).messages, {})
        with self.assertRaises(groups.GroupError):
            groups.set_message(g.id, "plan9", "x")
        with self.assertRaises(groups.GroupError):
            groups.set_message(g.id, "ios", "я" * (groups.MESSAGE_MAX + 1))
        with self.assertRaises(groups.GroupError):
            groups.set_message("нет такой", "ios", "x")
        # мусор в файле не роняет чтение
        data = self.groups_json()
        data["groups"][-1]["messages"] = {"android": 5, "ios": "  ok  ", "windows": "   "}
        paths.groups_file().write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(groups.Groups.load().get(g.id).messages, {"ios": "ok"})

    def test_message_signature_roundtrip_and_legacy_strings(self):
        users.bootstrap()
        g = groups.create("Семья", ["vless-reality"], {"android": ["happ"]})
        groups.set_message(g.id, "android", "привет", "abc123")
        self.assertEqual(self.groups_json()["groups"][-1]["messages"], {"android": {"text": "привет", "sig": "abc123"}})
        self.assertEqual(groups.Groups.load().get(g.id).msg_sigs, {"android": "abc123"})
        groups.update(g.id, name="Семья 2")
        self.assertEqual(groups.Groups.load().get(g.id).msg_sigs, {"android": "abc123"}, "update подпись не трогает")
        groups.set_message(g.id, "android", "привет 2")
        self.assertEqual(groups.Groups.load().get(g.id).msg_sigs, {}, "без подписи — старая не остаётся")
        groups.set_message(g.id, "android", "привет 3", "zzz")
        groups.set_message(g.id, "android", None)
        self.assertEqual(groups.Groups.load().get(g.id).msg_sigs, {})
        data = self.groups_json()
        data["groups"][-1]["messages"] = {"android": "старый формат", "ios": {"text": "новый", "sig": "q"},
                                          "windows": {"text": "  ", "sig": "x"}, "linux": {"sig": "x"}}
        paths.groups_file().write_text(json.dumps(data), encoding="utf-8")
        got = groups.Groups.load().get(g.id)
        self.assertEqual(got.messages, {"android": "старый формат", "ios": "новый"})
        self.assertEqual(got.msg_sigs, {"ios": "q"}, "у строки подписи нет")
        groups.Groups.load().save()
        self.assertEqual(self.groups_json()["groups"][-1]["messages"],
                         {"android": {"text": "старый формат", "sig": None}, "ios": {"text": "новый", "sig": "q"}})

    def test_default_client_follows_first_handoff_protocol(self):
        cat = clients.load()
        # поровну по охвату — рекомендованный каталога для первого по раздаче протокола группы
        opts = groups.client_options(cat, "android", ["hysteria2", "amneziawg"])
        self.assertEqual(opts[0]["client"]["id"], "amneziawg", "у Android первым в раздаче идёт AmneziaWG")
        self.assertTrue(opts[0]["recommended"])
        # только TUIC на десктопе: Hiddify проверен ядром на стенде, но каталог его там не рекомендует
        for plat in ("windows", "macos", "linux"):
            opts = groups.client_options(cat, plat, ["tuic"])
            self.assertIn("hiddify", [o["client"]["id"] for o in opts])
            self.assertFalse(any(o["recommended"] for o in opts))
        android = groups.client_options(cat, "android", ["tuic"])
        self.assertEqual(android[0]["client"]["id"], "hiddify", "SFA не берёт ссылки, Hiddify берёт")
        self.assertTrue(android[0]["recommended"])

    def test_corrupt_file_is_an_error_not_a_crash(self):
        paths.groups_file().write_text("{", encoding="utf-8")
        with self.assertRaises(groups.GroupError):
            groups.Groups.load()
        paths.groups_file().write_text('{"groups": 5}', encoding="utf-8")
        with self.assertRaises(groups.GroupError):
            groups.ensure()


class CliTest(GroupsBase):
    def setUp(self):
        super().setUp()
        users.bootstrap()

    def test_group_commands(self):
        code, out, err = run_cli("group", "list")
        self.assertEqual(code, 0, err)
        self.assertIn("Основная", out)
        self.assertIn("owner", out)
        code, out, err = run_cli("group", "add", "Семья", "--proto", "hysteria2", "--proto", "amneziawg",
                                 "--client", "android=happ,amneziawg", "--client", "windows=v2rayn",
                                 "--allow", "whatsapp", "--allow", "Discord.exe")
        self.assertEqual(code, 0, err)
        g = self.groups_json()["groups"][1]
        self.assertEqual((g["id"], g["protocols"], g["clients"]),
                         ("g1", ["hysteria2", "amneziawg"], {"android": ["happ", "amneziawg"], "windows": ["v2rayn"]}))
        code, out, _ = run_cli("group", "list")
        self.assertIn("android=happ+amneziawg", out)
        self.assertEqual(g["allowlist"]["android"], ["com.whatsapp"])
        self.assertEqual(g["allowlist"]["windows"], ["Discord.exe"])
        code, out, err = run_cli("user", "add", "masha", "--group", "Семья")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.registry()["masha"]["group"], "g1")
        code, out, _ = run_cli("user", "list")
        self.assertIn("Семья", out)
        code, out, _ = run_cli("user", "show", "masha")
        self.assertIn("группа: Семья (g1)", out)
        code, out, err = run_cli("group", "set", "g1", "--proto", "amneziawg", "--name", "Родные")
        self.assertEqual(code, 0, err)
        self.assertIn("masha", out + err)
        self.assertIn("новые QR", out + err)
        self.assertEqual(self.registry()["masha"]["protocols"], ["amneziawg"])
        # --client платформы заменяет её набор целиком, остальные платформы остаются
        code, out, err = run_cli("group", "set", "g1", "--client", "android=amneziawg", "--no-client", "windows")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.groups_json()["groups"][1]["clients"], {"android": ["amneziawg"]})
        code, out, err = run_cli("group", "set", "g1", "--allow-common", "--no-client", "android")
        self.assertEqual(code, 0, err)
        g = self.groups_json()["groups"][1]
        self.assertEqual((g["name"], g["allowlist"], g["clients"]), ("Родные", None, {}))
        code, out, err = run_cli("group", "rm", "g1")
        self.assertEqual(code, 1)
        self.assertIn("masha", err)
        code, out, err = run_cli("group", "move", "masha", "main")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(self.registry()["masha"]["protocols"]), sorted(PROTOS))
        code, out, err = run_cli("group", "rm", "Родные")
        self.assertEqual(code, 0, err)
        code, out, _ = run_cli("group", "list", "--json")
        data = json.loads(out)
        self.assertEqual([g["id"] for g in data["groups"]], ["main"])
        self.assertEqual(sorted(data["groups"][0]["members"]), ["masha", "owner"])

    def test_errors_are_messages(self):
        for argv in (("group", "add", "Основная"), ("group", "set", "нет", "--name", "x"),
                     ("group", "add", "X", "--client", "android"), ("group", "add", "X", "--proto", "nope"),
                     ("user", "add", "masha", "--group", "нет")):
            code, _, err = run_cli(*argv)
            self.assertEqual(code, 1, argv)
            self.assertTrue(err.strip(), argv)
        code, _, _ = run_cli("group")
        self.assertEqual(code, 2)


@needs_bash
@unittest.skipUnless(JQ, "нет jq (на сервере и в стенде он есть)")
class ZooAllowlistGroupsTest(GroupsBase):
    """lib.sh читает зеркало групп так же, как Python: свой → группы → общий."""

    def bash(self, user):
        script = '. "$ZOO_SCRIPTS_DIR/lib.sh"; zoo_allowlist android "%s"' % user
        cp = subprocess.run([BASH, "-c", script], capture_output=True, text=True, encoding="utf-8",
                            env={**os.environ, "SCRIPTS_DIR": self.env.scripts.as_posix()})
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout.strip()

    def test_precedence(self):
        users.bootstrap()
        groups.ensure()
        g = groups.create("Семья", ["amneziawg"], allow={"android": ["com.whatsapp", "com.x.y"],
                                                        "windows": ["Discord.exe"]})
        users.add_user("masha", group=g.id)
        users.add_user("petya")
        self.assertEqual(self.bash("masha"), "com.whatsapp, com.x.y")
        self.assertEqual(self.bash("petya"), "com.brave.browser, org.telegram.messenger")
        allowlist.set_lists({"android": ["org.own.app"], "windows": ["Discord.exe"]}, user="masha")
        self.assertEqual(self.bash("masha"), "org.own.app")
        for platform in ("android", "windows"):
            self.assertEqual(allowlist.Allowlist.load().effective(platform, "petya"),
                             self.bash("petya").split(", ") if platform == "android" else
                             allowlist.Allowlist.load().windows)
        # мусор в зеркале: не объект / не строка — общий список, как в Python
        for broken in ({"members": ["masha"], "groups": 5}, {"members": {"masha": 5}, "groups": {"g1": 1}}):
            paths.allowlist_file().write_text(json.dumps({"android": ["com.brave.browser"], **broken}),
                                              encoding="utf-8")
            self.assertEqual(self.bash("masha"), "com.brave.browser")
            self.assertEqual(allowlist.Allowlist.load().effective("android", "masha"), ["com.brave.browser"])


class DeleteGroupTest(GroupsBase):
    def setUp(self):
        super().setUp()
        users.bootstrap()
        self.g = groups.create("Семья", ["amneziawg"], allow={"android": ["com.whatsapp"], "windows": ["Discord.exe"]})
        for n in ("masha", "kolya"):
            users.add_user(n, group=self.g.id)

    def ids(self):
        return [g["id"] for g in self.groups_json()["groups"]]

    def test_move_members_to_main_with_one_apply(self):
        self.assertEqual(self.registry()["masha"]["protocols"], ["amneziawg"])
        with mock.patch.object(users, "_apply_protocols", wraps=users._apply_protocols) as ap:
            rep = groups.delete_group("Семья")
        self.assertEqual(ap.call_count, 1, "настройки «Основной» применяются один раз на всех")
        self.assertTrue(rep.ok and rep.removed, rep.to_dict())
        self.assertEqual(sorted(rep.moved), ["kolya", "masha"])
        self.assertEqual(self.ids(), ["main"])
        reg = self.registry()
        for n in ("masha", "kolya"):
            self.assertEqual((reg[n]["group"], sorted(reg[n]["protocols"])), ("main", sorted(PROTOS)))
        al = self.allow_json()
        self.assertFalse(al.get("groups") or al.get("members"), "зеркало списка приложений чистое")
        self.assertEqual(inspect.signature(groups.delete_group).parameters["members"].default, "move")

    def test_delete_keeps_custom_protocol_sets(self):
        reg = users.Registry.load()
        reg.get("masha").custom = True
        reg.save()
        rep = groups.delete_group(self.g.id)
        self.assertTrue(rep.ok and rep.removed, rep.to_dict())
        self.assertEqual(rep.skipped, ["masha"], "свой набор группа не тронула")
        reg = self.registry()
        self.assertEqual((reg["masha"]["group"], reg["masha"]["protocols"], reg["masha"]["custom"]),
                         ("main", ["amneziawg"], True), "группа сменилась, набор и признак — нет")
        self.assertEqual((reg["kolya"]["group"], sorted(reg["kolya"]["protocols"])), ("main", sorted(PROTOS)))
        self.assertNotIn("custom", reg["kolya"])

    def test_expected_members_must_match(self):
        for bad in ([], ["masha"], ["masha", "kolya", "petya"], ["masha", "owner"]):
            with self.assertRaises(groups.MembersChanged, msg=bad) as cm:
                groups.delete_group(self.g.id, "delete", bad)
            self.assertIn("изменился", str(cm.exception))
            self.assertIn("Ничего не удалено", str(cm.exception))
        self.assertEqual(self.ids(), ["main", self.g.id])
        reg = self.registry()
        self.assertEqual((reg["masha"]["group"], reg["kolya"]["group"]), (self.g.id, self.g.id))
        self.assertIn("masha", self.env.proto_users("amneziawg"))
        rep = groups.delete_group(self.g.id, "delete", ["kolya", "masha"])
        self.assertTrue(rep.removed, rep.to_dict())
        self.assertEqual(sorted(rep.deleted), ["kolya", "masha"])

    def test_expected_empty_group_and_new_member(self):
        e = groups.create("Пустая", ["amneziawg"])
        self.assertTrue(groups.delete_group(e.id, "move", []).removed)
        e = groups.create("Пустая2", ["amneziawg"])
        users.add_user("petya", group=e.id)   # добавили, пока страница подтверждения была открыта
        with self.assertRaises(groups.MembersChanged):
            groups.delete_group(e.id, "delete", [])
        self.assertIn("petya", self.registry(), "новичка не удалили вслепую")

    def test_delete_members_keeps_owner(self):
        groups.move_many(["owner"], self.g.id)
        rep = groups.delete_group(self.g.id, members="delete")
        self.assertTrue(rep.ok and rep.removed, rep.to_dict())
        self.assertEqual(sorted(rep.deleted), ["kolya", "masha"])
        self.assertEqual(rep.moved, ["owner"])
        reg = self.registry()
        self.assertNotIn("masha", reg)
        self.assertNotIn("kolya", reg)
        self.assertEqual(reg["owner"]["group"], "main")
        self.assertEqual(sorted(reg["owner"]["protocols"]), sorted(PROTOS))
        for pid in PROTOS:
            self.assertEqual(set(self.env.proto_users(pid)), {"owner"})
        self.assertEqual(self.ids(), ["main"])

    def test_delete_empty_group_any_mode(self):
        for mode in ("move", "delete"):
            g = groups.create("Пустая " + mode, ["amneziawg"])
            rep = groups.delete_group(g.id, mode)
            self.assertTrue(rep.removed)
            self.assertEqual((rep.moved, rep.deleted), ([], []))
        self.assertEqual(self.ids(), ["main", self.g.id])

    def test_main_and_bad_input_are_refused(self):
        for args in (("main",), ("Основная", "delete"), ("нет",), (self.g.id, "everything"), (self.g.id, "")):
            with self.assertRaises(groups.GroupError, msg=args):
                groups.delete_group(*args)
        self.assertEqual(self.ids(), ["main", self.g.id])
        self.assertEqual(self.registry()["masha"]["group"], self.g.id)

    def test_failed_delete_keeps_group_and_reports(self):
        self.env.fail("amneziawg:user_del")
        rep = groups.delete_group(self.g.id, "delete")
        self.assertFalse(rep.ok)
        self.assertFalse(rep.removed)
        self.assertEqual(rep.deleted, [])
        self.assertEqual(len(rep.errors), 2)
        self.assertIn(self.g.id, self.ids())
        self.assertIn("не удалена", rep.message)
        self.assertEqual(self.registry()["masha"]["group"], self.g.id)

    def test_lock_is_taken_once(self):
        real = users._lock
        with mock.patch.object(users, "_lock", side_effect=real) as lock:
            groups.delete_group(self.g.id, "delete")
        self.assertEqual(lock.call_count, 1)


class MergeGroupsTest(GroupsBase):
    def setUp(self):
        super().setUp()
        users.bootstrap()
        self.a = groups.create("Семья", ["amneziawg"], allow={"android": ["com.whatsapp"], "windows": ["Discord.exe"]})
        self.b = groups.create("Друзья", ["hysteria2", "vless-reality"], {"android": ["happ"]})
        groups.set_message(self.b.id, "android", "Привет, {name}!")
        for n in ("masha", "kolya"):
            users.add_user(n, group=self.a.id)
        users.add_user("petya", group=self.b.id)

    def group(self, gid):
        return next(g for g in self.groups_json()["groups"] if g["id"] == gid)

    def test_members_move_and_target_keeps_settings(self):
        before = self.group(self.b.id)
        with mock.patch.object(users, "_apply_protocols", wraps=users._apply_protocols) as ap:
            rep = groups.merge_groups(self.a.id, "друзья")
        self.assertEqual(ap.call_count, 1)
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual(rep.group.id, self.b.id)
        self.assertEqual(sorted(rep.moved), ["kolya", "masha"])
        self.assertIn("«Семья» объединена с «Друзья»", rep.message)
        self.assertEqual([g["id"] for g in self.groups_json()["groups"]], ["main", self.b.id])
        self.assertEqual(self.group(self.b.id), before, "протоколы, клиенты, приложения и тексты цели не тронуты")
        reg = self.registry()
        for n in ("masha", "kolya"):
            self.assertEqual((reg[n]["group"], sorted(reg[n]["protocols"])), (self.b.id, ["hysteria2", "vless-reality"]))
            self.assertNotIn("custom", reg[n])
        self.assertEqual(reg["petya"]["group"], self.b.id)
        al = self.allow_json()
        self.assertFalse(al.get("groups") or al.get("members"), "список исчезнувшей группы из зеркала убран")

    def test_custom_members_keep_their_set(self):
        reg = users.Registry.load()
        reg.get("masha").custom = True
        reg.save()
        rep = groups.merge_groups(self.a.id, self.b.id)
        self.assertEqual(rep.skipped, ["masha"])
        reg = self.registry()
        self.assertEqual((reg["masha"]["group"], reg["masha"]["protocols"], reg["masha"]["custom"]),
                         (self.b.id, ["amneziawg"], True), "группа новая, набор протоколов — свой, как был")
        self.assertEqual(sorted(reg["kolya"]["protocols"]), ["hysteria2", "vless-reality"])
        self.assertNotIn("custom", reg["kolya"])

    def test_empty_source_is_just_removed(self):
        e = groups.create("Пустая", ["amneziawg"])
        rep = groups.merge_groups(e.id, self.a.id)
        self.assertEqual(rep.moved, [])
        self.assertNotIn(e.id, [g["id"] for g in self.groups_json()["groups"]])

    def test_refusals(self):
        for args in ((self.a.id, self.a.id), ("main", self.a.id), (self.a.id, "нет"), ("нет", self.a.id)):
            with self.assertRaises(groups.GroupError, msg=args):
                groups.merge_groups(*args)
        self.assertEqual(len(self.groups_json()["groups"]), 3)
        self.assertEqual(self.registry()["masha"]["group"], self.a.id)

    def test_into_main(self):
        groups.merge_groups(self.a.id, "main")
        reg = self.registry()
        self.assertEqual((reg["masha"]["group"], sorted(reg["masha"]["protocols"])), ("main", sorted(PROTOS)))

    def test_lock_is_taken_once(self):
        real = users._lock
        with mock.patch.object(users, "_lock", side_effect=real) as lock:
            groups.merge_groups(self.a.id, self.b.id)
        self.assertEqual(lock.call_count, 1)


class GroupDeleteMergeCliTest(GroupsBase):
    def setUp(self):
        super().setUp()
        users.bootstrap()
        run_cli("group", "add", "Семья", "--proto", "amneziawg")
        run_cli("group", "add", "Друзья", "--proto", "hysteria2")
        run_cli("user", "add", "masha", "--group", "g1")
        run_cli("user", "add", "kolya", "--group", "g1")

    def ids(self):
        return [g["id"] for g in self.groups_json()["groups"]]

    def test_rm_move_members(self):
        code, out, err = run_cli("group", "rm", "g1")
        self.assertEqual(code, 1, "с участниками без флага — по-прежнему отказ")
        self.assertIn("masha", err)
        code, out, err = run_cli("group", "rm", "g1", "--move-members")
        self.assertEqual(code, 0, err)
        self.assertIn("masha: в группе «Основная»", out + err)
        self.assertEqual(self.ids(), ["main", "g2"])
        self.assertEqual(self.registry()["masha"]["group"], "main")

    def test_rm_delete_members(self):
        code, out, err = run_cli("group", "rm", "Семья", "--delete-members")
        self.assertEqual(code, 0, err)
        self.assertIn("masha: удалён", out + err)
        self.assertNotIn("masha", self.registry())
        self.assertEqual(set(self.env.proto_users("amneziawg")), {"owner"})
        code, out, _ = run_cli("group", "rm", "g2", "--delete-members", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["deleted"], [])

    def test_rm_main_and_flag_conflict(self):
        for flags in ((), ("--move-members",), ("--delete-members",)):
            code, out, err = run_cli("group", "rm", "main", *flags)
            self.assertEqual(code, 1, flags)
            self.assertIn("не удаляется", err)
        self.assertEqual(self.ids(), ["main", "g1", "g2"])
        with self.assertRaises(SystemExit):
            run_cli("group", "rm", "g1", "--move-members", "--delete-members")

    def test_merge(self):
        code, out, err = run_cli("group", "merge", "g1", "g2")
        self.assertEqual(code, 0, err)
        self.assertIn("masha: в группе «Друзья»", out + err)
        self.assertIn("«Семья» объединена с «Друзья»", out + err)
        self.assertEqual(self.ids(), ["main", "g2"])
        self.assertEqual(self.registry()["kolya"]["protocols"], ["hysteria2"])
        for argv in (("group", "merge", "g2", "g2"), ("group", "merge", "main", "g2"), ("group", "merge", "g2", "нет")):
            code, _, err = run_cli(*argv)
            self.assertEqual(code, 1, argv)
            self.assertTrue(err, argv)
        code, out, _ = run_cli("group", "merge", "g2", "main", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["group"]["id"], "main")


class ObfsGroupTest(GroupsBase):
    """Salamander (hysteria2-obfs) выбирается группой отдельно от Hysteria2; учётки у них общие (модуль hysteria2)."""

    def setUp(self):
        super().setUp()
        self.env.add_manifest("hysteria2-obfs", layer="udp", users_backend="hysteria-command", engine="hysteria",
                              name="Hysteria2 + Salamander", short="HY2 + Salamander")
        users.bootstrap()
        groups.ensure()

    def test_selectable_and_variants(self):
        self.assertEqual(users.variant_modules(), {"hysteria2-obfs": "hysteria2"})
        sel = users.selectable_protocols()
        self.assertEqual(sorted(sel), ["amneziawg", "hysteria2", "hysteria2-obfs", "vless-reality"])
        self.assertEqual(sel.index("hysteria2-obfs"), sel.index("hysteria2") + 1, "вариант — следом за модулем")
        self.env.add_manifest("hysteria2-obfs", enabled=False, users_backend="hysteria-command")
        self.assertEqual(users.variant_modules(), {})
        self.assertNotIn("hysteria2-obfs", users.selectable_protocols())
        with self.assertRaises(groups.GroupError):
            groups.create("Семья", ["hysteria2-obfs"])

    def test_group_stores_a_set_in_priority_order(self):
        g = groups.create("Семья", ["vless-reality", "hysteria2-obfs", "amneziawg", "hysteria2-obfs", "hysteria2"])
        self.assertEqual(g.protocols, ["hysteria2", "amneziawg", "hysteria2-obfs", "vless-reality"])
        self.assertEqual(self.groups_json()["groups"][1]["protocols"], g.protocols)
        # прочитанный в «чужом» порядке файл нормализуется; «*» остаётся единственным значением
        data = self.groups_json()
        data["groups"][1]["protocols"] = ["ss2022", "tuic", "vless-reality", "hysteria2-obfs", "hysteria2", "x"]
        data["groups"][0]["protocols"] = ["amneziawg", "*"]
        paths.groups_file().write_text(json.dumps(data), encoding="utf-8")
        gs = groups.Groups.load()
        self.assertEqual(gs.get("g1").protocols, ["hysteria2", "hysteria2-obfs", "tuic", "vless-reality", "ss2022", "x"])
        self.assertEqual(gs.get("main").protocols, ["*"])
        self.assertEqual(groups.by_priority(["b", "a", "tuic", "hysteria2"]), ["hysteria2", "tuic", "a", "b"])

    def test_resolve_maps_variants_to_modules_and_offered_keeps_them(self):
        managed, _ = users.managed_protocols()
        sel = users.selectable_protocols()
        only = groups.Group("g", "G", ["hysteria2-obfs"])
        self.assertEqual(only.resolve(managed), ["hysteria2"])
        self.assertEqual(only.offered(sel), ["hysteria2-obfs"])
        both = groups.Group("g", "G", ["hysteria2-obfs", "hysteria2", "amneziawg"])
        self.assertEqual(both.resolve(managed), ["hysteria2", "amneziawg"], "учётка модуля одна")
        self.assertEqual(both.offered(sel), ["hysteria2", "amneziawg", "hysteria2-obfs"])
        plain = groups.Group("g", "G", ["hysteria2"])
        self.assertEqual((plain.resolve(managed), plain.offered(sel)), (["hysteria2"], ["hysteria2"]))
        everything = groups.Group("main", "Основная", ["*"])
        self.assertEqual(everything.resolve(managed), managed)
        self.assertEqual(everything.offered(sel), sel)
        self.assertEqual(only.resolve(["amneziawg"]), [], "модуля нет среди включённых — учёток нет")

    def test_obfs_only_group_gives_hysteria2_credentials(self):
        g = groups.create("Салам", ["hysteria2-obfs"])
        rep = users.add_user("masha", group=g.id)
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual(self.registry()["masha"]["protocols"], ["hysteria2"])
        self.assertIn("masha", self.env.proto_users("hysteria2"))
        self.assertNotIn("masha", self.env.proto_users("vless-reality"))
        self.assertNotIn("custom", self.registry()["masha"])
        # sync доводит до протоколов группы, а не до всех включённых
        reg = users.Registry.load()
        reg.get("masha").protocols.clear()
        reg.save()
        users.sync_users(["masha"])
        self.assertEqual(self.registry()["masha"]["protocols"], ["hysteria2"])

    def test_swapping_hysteria2_for_salamander_keeps_credentials_and_asks_new_qr(self):
        g = groups.create("Телефон", ["hysteria2", "amneziawg"])
        users.add_user("masha", group=g.id)
        users.add_user("petya", group=g.id, only=["amneziawg"])   # свой набор
        before = list(self.env.calls())
        rep = groups.update(g.id, protocols=["hysteria2-obfs", "amneziawg"])
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual(sorted(self.registry()["masha"]["protocols"]), ["amneziawg", "hysteria2"], "учётки на месте")
        new_calls = self.env.calls()[len(before):]
        self.assertFalse([c for c in new_calls if c.startswith("hysteria2 user_")], new_calls)
        self.assertEqual(rep.needs_qr, ["masha"], "учётки те же, но ссылка другая — QR нужен; свой набор не тронут")
        self.assertEqual(rep.skipped, ["petya"])
        # тот же набор в другом порядке — ничего нового
        rep = groups.update(g.id, protocols=["amneziawg", "hysteria2-obfs"])
        self.assertEqual(rep.needs_qr, [])

    def test_add_obfs_to_group_without_new_credentials(self):
        g = groups.create("Телефон", ["hysteria2", "amneziawg"])
        users.add_user("masha", group=g.id)
        rep = groups.update(g.id, protocols=["hysteria2", "hysteria2-obfs", "amneziawg"])
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual(rep.needs_qr, ["masha"], "появилась ссылка Salamander")

    def test_clean_clients_counts_variant_protocols(self):
        self.assertEqual(groups.clean_clients({"android": ["v2rayng"]}, ["hysteria2-obfs"]), {"android": ["v2rayng"]})
        with self.assertRaises(groups.GroupError):   # Happ Salamander не заявлен
            groups.clean_clients({"android": ["happ"]}, ["hysteria2-obfs"])
        self.assertEqual(groups.clean_clients({"android": ["happ"]}, ["hysteria2-obfs", "hysteria2"]),
                         {"android": ["happ"]})

    def test_migration_adds_salamander_once_to_groups_with_hysteria2(self):
        groups.create("С Hysteria2", ["hysteria2", "amneziawg"])
        groups.create("Без неё", ["amneziawg"])
        data = self.groups_json()
        data.pop("obfs_split", None)   # как до разделения: метки нет
        paths.groups_file().write_text(json.dumps(data), encoding="utf-8")
        gs = groups.ensure()
        self.assertEqual({g.id: g.protocols for g in gs.groups},
                         {"main": ["*"], "g1": ["hysteria2", "amneziawg", "hysteria2-obfs"], "g2": ["amneziawg"]})
        self.assertEqual(self.groups_json().get("obfs_split"), 1)
        # один раз: потом Salamander можно снять, миграция его не вернёт
        groups.update("g1", protocols=["hysteria2", "amneziawg"])
        gs = groups.ensure()
        self.assertEqual(gs.get("g1").protocols, ["hysteria2", "amneziawg"])
        self.assertFalse(groups._pending(gs, users.Registry.load()))

    def test_migration_waits_while_salamander_is_off(self):
        groups.create("С Hysteria2", ["hysteria2"])
        data = self.groups_json()
        data.pop("obfs_split", None)
        paths.groups_file().write_text(json.dumps(data), encoding="utf-8")
        self.env.add_manifest("hysteria2-obfs", enabled=False, users_backend="hysteria-command")
        gs = groups.ensure()
        self.assertEqual(gs.get("g1").protocols, ["hysteria2"])
        self.assertNotIn("obfs_split", self.groups_json(), "метка не ставится: Salamander ещё не включён")
        self.assertFalse(groups._pending(gs, users.Registry.load()), "и страницы не берут блокировку на каждом показе")
        # включили позже — прежнее поведение (Salamander вместе с Hysteria2) возвращается один раз
        self.env.add_manifest("hysteria2-obfs", users_backend="hysteria-command")
        gs = groups.ensure()
        self.assertEqual(gs.get("g1").protocols, ["hysteria2", "hysteria2-obfs"])
        self.assertEqual(self.groups_json().get("obfs_split"), 1)

    def test_fresh_server_marks_migration_done(self):
        self.assertEqual(self.groups_json().get("obfs_split"), 1)

    LINKS = [protolib.Link("hysteria2://a@h:443/?sni=x", "", "hysteria2"),
             protolib.Link("hysteria2://a@h:443,20000-30000/?sni=x", "", "hysteria2"),
             protolib.Link("hysteria2://a@h:8443/?sni=x&obfs=salamander&obfs-password=p", "", "hysteria2"),
             protolib.Link("vless://u@h:443#x", "", "vless-reality")]

    def test_links_shown_only_for_selected_variants(self):
        from zoolib.web import userviews
        def shown(g, **kw):
            f = userviews.link_filter(users.User("masha", group=g.id, **kw), g)
            return None if f is None else [i for i, ln in enumerate(self.LINKS) if f(ln)]
        self.assertEqual(shown(groups.Group("g", "G", ["hysteria2-obfs"])), [2, 3], "только Salamander и VLESS")
        self.assertEqual(shown(groups.Group("g", "G", ["hysteria2"])), [0, 1, 3], "обычная и hop, без Salamander")
        self.assertEqual(shown(groups.Group("g", "G", ["hysteria2", "hysteria2-obfs"])), [0, 1, 2, 3])
        self.assertIsNone(shown(groups.Group("main", "Основная", ["*"])))
        self.assertIsNone(shown(groups.Group("g", "G", ["hysteria2-obfs"]), custom=True), "свой набор — всё")
        self.assertIsNone(userviews.link_filter(users.User("owner", group="g"), groups.Group("g", "G", ["hysteria2-obfs"])))
        self.assertIsNone(userviews.link_filter(users.User("masha"), None))

    def test_connect_panel_hands_off_only_selected_variants(self):
        from zoolib.web import clientviews
        ctx = clientviews.Ctx.load()
        def panel(protos):
            g = groups.Group("g", "G", protos, {"android": ["v2rayng"]})
            return str(clientviews.connect_panel(self.LINKS, "masha", ctx, g))
        out = panel(["hysteria2-obfs"])
        self.assertIn("obfs=salamander", out)
        self.assertNotIn("20000", out)
        self.assertNotIn("sni=x&amp;obfs", out.replace("obfs=salamander&amp;obfs-password=p", ""))
        self.assertEqual(out.count("<input type=\"text\""), 1, "одна ссылка: Salamander")
        out = panel(["hysteria2"])
        self.assertNotIn("salamander", out)
        self.assertIn("hysteria2://a@h:443/?sni=x", out)
        out = panel(["hysteria2", "hysteria2-obfs"])
        self.assertEqual(out.count("<input type=\"text\""), 2)
        self.assertEqual(re.findall(r'<div class="key-name">([^<]*)</div>', out), ["Протокол hysteria2", "Hysteria2 + Salamander"],
                         "по приоритету: Hysteria2, затем Salamander")


class ProtocolUsersTest(GroupsBase):
    """Счётчики пользователей на карточках «Обзора»: включённые, обычные, у которых протокол в реестре."""

    def collect(self):
        from zoolib import config, status, system
        units = {"x-ui.service": {"load": "loaded", "active": "active"}}
        with mock.patch.object(system, "listening_sockets", return_value=[]),                 mock.patch.object(system, "ufw_active", return_value=True),                 mock.patch.object(system, "unit_states", side_effect=lambda us: {
                    u: units.get(u, {"load": "not-found", "active": "inactive"}) for u in us}),                 mock.patch.object(system, "component_versions", return_value={}):
            return {p["id"]: p for p in status.collect(config.load(), cpu_interval=0, with_xui=False)["protocols"]}

    def setUp(self):
        super().setUp()
        for pid in ("vless-xhttp", "tuic"):
            self.env.add_protocol(pid)
        self.env.add_manifest("hysteria2-obfs", layer="udp", users_backend="hysteria-command", engine="hysteria")

    def wizard(self):
        users.bootstrap()
        users.add_user("masha")
        users.add_user("zoo-probe", system=True, partial=True)
        rep = groups.connect("Группа 2", ["vless-reality", "hysteria2", "amneziawg"], {}, None,
                             [("vasy", ""), ("vasy2", ""), ("vasy3", "")], [])
        self.assertTrue(rep.ok, rep.errors)

    def test_wizard_group_counts_owner_masha_and_new_users(self):
        self.wizard()
        by = self.collect()
        five = ("vless-reality", "hysteria2", "hysteria2-obfs", "amneziawg")
        self.assertEqual({p: by[p]["users"] for p in five}, dict.fromkeys(five, 5), "служебный zoo-probe не считается")
        self.assertEqual((by["tuic"]["users"], by["vless-xhttp"]["users"]), (2, 2), "только owner и masha")
        self.assertEqual(by["tuic"]["user_names"], ["owner", "masha"])
        self.assertEqual(by["tuic"]["lacking"], [["vasy", "нет в группе «Группа 2»"],
                                                 ["vasy2", "нет в группе «Группа 2»"], ["vasy3", "нет в группе «Группа 2»"]])
        self.assertEqual(by["tuic"]["users_off"], 0)

    def test_custom_and_unsynced_users_are_named_with_reason(self):
        users.bootstrap()
        groups.ensure()
        users.add_user("petya", only=["amneziawg"])
        users.add_user("masha")
        reg = users.Registry.load()
        reg.get("masha").protocols.remove("hysteria2")
        reg.get("masha").custom = False
        reg.save()
        by = self.collect()
        self.assertEqual(by["vless-reality"]["lacking"], [["petya", "свой набор протоколов"]])
        self.assertEqual(by["hysteria2"]["lacking"], [["petya", "свой набор протоколов"],
                                                      ["masha", "не заведён (zoo user sync)"]])
        self.assertEqual(by["hysteria2"]["users"], 1)

    def test_disabled_users_are_counted_separately(self):
        users.bootstrap()
        users.add_user("masha")
        users.set_enabled("masha", False)
        by = self.collect()
        self.assertEqual((by["amneziawg"]["users"], by["amneziawg"]["users_off"]), (1, 1))
        self.assertEqual((by["amneziawg"]["user_names"], by["amneziawg"]["off_names"]), (["owner"], ["masha"]))
        self.assertEqual(by["amneziawg"]["lacking"], [], "отключённый — не «без протокола»")


def mini_catalog(**changes):
    """Каталог из файла; changes — правки по id клиента: {"hiddify": {"protocols": {...}}}."""
    cat = clients.load()
    for cid, patch in changes.items():
        c = cat.client(cid)
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(c.get(k), dict):
                c[k].update(v)
            else:
                c[k] = v
    return cat


class ClientSetsTest(unittest.TestCase):
    def test_sets_cover_at_most_two_apps_and_best_first(self):
        cat = clients.load()
        proto3 = ["hysteria2", "vless-xhttp", "amneziawg"]
        sets = groups.client_sets(cat, "ios", proto3)
        self.assertEqual(sets[0]["ids"], ["happ", "amneziavpn"])
        self.assertEqual(sets[0]["missing"], [])
        self.assertEqual(sets[0]["foreign"], 1)
        self.assertTrue(all(1 <= len(x["ids"]) <= groups.MAX_APPS for x in sets))
        pairs = {frozenset(x["ids"]) for x in sets}
        self.assertEqual(len(pairs), len(sets), "наборы не повторяются")
        for x in sets:
            self.assertFalse(set(x["covers"]) & set(x["missing"]))
            self.assertEqual(sorted(x["covers"] + x["missing"]), sorted(proto3))
        # набор, где приложение ничего не добавляет, не предлагается: Happ + Hiddify для Hysteria2 — одно и то же
        for x in groups.client_sets(cat, "android", ["hysteria2"]):
            self.assertEqual(len(x["ids"]), 1, x)

    def test_equal_coverage_russian_store_beats_foreign_and_prefer_only_breaks_ties(self):
        cat = clients.load()
        by = groups.client_sets(cat, "ios", ["vless-reality", "vless-xhttp"])
        self.assertEqual(by[0]["ids"], ["incy"], "то же покрытие, что у Happ, но приложение из App Store РФ")
        # Android + AmneziaWG: prefer не вытесняет рекомендованное каталогом
        self.assertEqual(groups.client_sets(cat, "android", ["amneziawg"], prefer={"amneziavpn"})[0]["ids"], ["amneziawg"])
        self.assertEqual(groups.client_sets(cat, "android", ["hysteria2"], "admin", prefer={"hiddify"})[0]["ids"],
                         ["happ"], "prefer не сильнее рекомендации каталога")
        # при равенстве всего прочего (macOS, Hysteria2: каталог никого не рекомендует) prefer решает
        a = groups.client_sets(cat, "macos", ["hysteria2"], "admin")
        b = groups.client_sets(cat, "macos", ["hysteria2"], "admin", prefer={"hysteria"})
        self.assertEqual((a[0]["ids"], b[0]["ids"]), (["hiddify"], ["hysteria"]))
        self.assertEqual({tuple(x["ids"]) for x in a}, {tuple(x["ids"]) for x in b})

    def test_no_set_for_empty_or_unsupported(self):
        cat = clients.load()
        self.assertEqual(groups.client_sets(cat, "android", []), [])
        self.assertEqual(groups.client_sets(cat, "macos", ["vless-reality"]), [])
        self.assertEqual(groups.client_sets(cat, "macos", ["vless-reality", "amneziawg"])[0]["missing"], ["vless-reality"])

    def test_default_never_exceeds_two_apps(self):
        cat = clients.load()
        for mode in groups.INSTALL_MODES:
            for r in (1, 2, 3, 4):
                for protos in itertools.combinations(groups.PRIORITY, r):
                    for plat, ids in groups.default_clients(cat, list(protos), mode).items():
                        self.assertLessEqual(len(ids), groups.MAX_APPS, (mode, plat, protos, ids))

    def test_store_rules_by_mode(self):
        cat = clients.load()
        self.assertTrue(groups.in_store(cat.client("happ"), "android"))
        self.assertFalse(groups.in_store(cat.client("v2rayng"), "android"))
        self.assertFalse(groups.in_store(cat.client("singbox"), "ios"), "SFI в App Store недоступен")
        self.assertEqual(groups.default_clients(cat, ["vless-reality"], "self")["android"], ["happ"])
        # на iPhone — приложение магазина, даже если оно не из РФ-магазина (sing-box из магазина недоступен)
        for mode in groups.INSTALL_MODES:
            self.assertEqual(groups.suggest_set(cat, ["ios"], ["hysteria2"], mode), {"ios": ["happ"]})
        # админ на Android берёт любое: v2rayNG из APK покрывает xhttp + obfs одним приложением
        self.assertEqual(groups.suggest_set(cat, ["android"], ["vless-xhttp", "hysteria2-obfs"], "admin"),
                         {"android": ["v2rayng"]})
        self.assertEqual(groups.suggest_set(cat, ["android"], ["vless-xhttp", "hysteria2-obfs"], "self"),
                         {"android": ["happ", "hiddify"]}, "людям — только приложения из магазина")

    def test_simple_preset_is_one_app_per_device(self):
        cat = clients.load()
        every = ["hysteria2", "vless-xhttp", "amneziawg", "hysteria2-obfs", "tuic", "vless-reality", "ss2022"]
        for mode in groups.INSTALL_MODES:
            pr = {p["id"]: p for p in groups.presets(cat, every, mode)}
            simple = pr["simple"]
            self.assertEqual(simple["protocols"], ["hysteria2"], mode)
            self.assertEqual(simple["plan"], {p: ["hiddify"] for p in groups.MAIN_DEVICES}, mode)
            self.assertEqual((simple["apps"], simple["per_device"], simple["complete"]), (1, 1, True))
            rel = pr["reliable"]
            self.assertEqual(rel["protocols"], ["hysteria2", "vless-xhttp"], "TCP + UDP, самые устойчивые")
            self.assertLessEqual(rel["per_device"], groups.MAX_APPS)
            self.assertTrue(rel["complete"])
            self.assertNotIn("ss2022", rel["protocols"], "SS-2022 терял данные в полевом тесте")

    def test_self_presets_only_protocols_that_import_by_one_qr_from_a_store_app(self):
        cat = clients.load()
        easy = groups.easy_protocols(cat)
        self.assertEqual(easy, {"hysteria2", "vless-reality", "vless-xhttp", "amneziawg", "ss2022"})
        self.assertNotIn("tuic", easy, "TUIC: ни одного приложения из магазина с QR")
        self.assertNotIn("hysteria2-obfs", easy, "Salamander: QR только у v2rayNG (APK)")
        self.assertEqual(groups.presets(cat, ["tuic", "hysteria2-obfs"], "self"), [],
                         "людям нечего предложить без QR-протоколов")
        admin = groups.presets(cat, ["tuic"], "admin")
        self.assertEqual([p["id"] for p in admin], ["simple"], "ИТ: TUIC через Hiddify; пары нет")

    def test_hiddify_gone_changes_the_simple_preset(self):
        cat = mini_catalog(hiddify={"protocols": {"hysteria2": {"s": "no"}}})
        pr = groups.presets(cat, ["hysteria2", "vless-xhttp"], "admin")
        simple = next(p for p in pr if p["id"] == "simple")
        self.assertEqual(simple["apps"], 2, "общего приложения для Hysteria2 больше нет: Happ и v2rayN")
        self.assertNotIn("hiddify", {a for ids in simple["plan"].values() for a in ids})

    def test_install_mode_is_stored_and_defaults_to_self(self):
        g = groups.Group.from_dict({"id": "g1", "name": "Офис", "install_mode": "admin"})
        self.assertEqual(g.install_mode, "admin")
        self.assertEqual(g.to_dict()["install_mode"], "admin")
        for raw in ({"id": "g1"}, {"id": "g1", "install_mode": "other"}, {"id": "g1", "install_mode": 7}):
            self.assertEqual(groups.Group.from_dict(raw).install_mode, "self")
        self.assertNotIn("install_mode", groups.Group.from_dict({"id": "g1"}).to_dict(), "прежние группы файл не меняют")


@needs_bash
class InstallModeGroupTest(GroupsBase):
    def test_create_update_and_cli_keep_the_mode(self):
        users.bootstrap()
        g = groups.create("Офис", ["hysteria2"], {"android": ["hiddify"]}, install_mode="admin")
        self.assertEqual(self.groups_json()["groups"][-1]["install_mode"], "admin")
        groups.update(g.id, name="Офис 2")
        self.assertEqual(groups.Groups.load().get(g.id).install_mode, "admin", "без аргумента режим не меняется")
        groups.update(g.id, install_mode="self")
        self.assertEqual(groups.Groups.load().get(g.id).install_mode, "self")
        self.assertNotIn("install_mode", self.groups_json()["groups"][-1])
        code, out, _ = run_cli("group", "set", g.id, "--install", "admin")
        self.assertEqual(code, 0, out)
        self.assertEqual(groups.Groups.load().get(g.id).install_mode, "admin")
        code, out, _ = run_cli("group", "list", "--json")
        self.assertIn('"install_mode": "admin"', out)


if __name__ == "__main__":
    unittest.main()
