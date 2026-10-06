import json
import os
import shutil
import subprocess
import unittest
from unittest import mock

from tests.helpers import BASH, ZooEnv, needs_bash
from tests.test_cli import run_cli
from zoolib import allowlist, clients, groups, paths, users

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
        g = groups.create("  Семья \n", ["hysteria2", "amneziawg"], {"android": "happ", "ios": ""})
        self.assertEqual((g.id, g.name, g.protocols, g.clients), ("g1", "Семья", ["hysteria2", "amneziawg"],
                                                                 {"android": "happ"}))
        self.assertEqual(groups.Groups.load().next_name(), "Группа 3")
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
        rep = groups.update(g.id, name="Мобильные", clients={"android": "happ"})
        self.assertEqual((rep.needs_qr, rep.skipped), ([], []))
        self.assertEqual(self.groups_json()["groups"][1]["name"], "Мобильные")
        self.assertEqual(self.groups_json()["groups"][1]["clients"], {"android": "happ"})
        with self.assertRaises(groups.GroupError):
            groups.update(g.id, name="Основная")
        with self.assertRaises(groups.GroupError):
            groups.update(g.id, protocols=["nope"])
        self.assertEqual(self.groups_json()["groups"][1]["protocols"], ["vless-reality", "amneziawg"])

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
                             groups.parse_new_users("masha сестра Маша\nkolya"), ["petya"])
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
        self.assertEqual(groups.parse_new_users("  Masha  сестра \n\n kolya\n"), [("masha", "сестра"), ("kolya", "")])
        with self.assertRaises(groups.GroupError):
            groups.parse_new_users("\n".join(f"u{i}" for i in range(groups.NEW_USERS_MAX + 1)))

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
        self.assertEqual(defaults["android"], "happ")
        self.assertNotIn("macos", defaults)

    def test_ios_default_is_not_happ(self):
        cat = clients.load()
        proto3 = ["vless-reality", "hysteria2", "amneziawg"]
        opts = groups.client_options(cat, "ios", proto3)
        ids = [o["client"]["id"] for o in opts]
        self.assertEqual(ids[0], "incy", "Happ нет в российском App Store")
        self.assertIn("happ", ids, "выбрать его осознанно можно")
        self.assertTrue(next(o for o in opts if o["client"]["id"] == "happ")["no_ru_store"])
        self.assertEqual(groups.default_clients(cat, proto3)["ios"], "incy")
        self.assertEqual(groups.default_clients(cat, ["vless-reality", "hysteria2"])["ios"], "incy",
                         "Happ покрывает оба протокола, но он не из РФ-магазина")
        # на Android Happ остаётся первым: он есть в Google Play и на GitHub
        self.assertEqual(groups.default_clients(cat, ["vless-reality", "hysteria2"])["android"], "happ")

    def test_default_client_follows_first_handoff_protocol(self):
        cat = clients.load()
        # поровну по охвату — рекомендованный каталога для первого по раздаче протокола группы
        opts = groups.client_options(cat, "android", ["hysteria2", "amneziawg"])
        self.assertEqual(opts[0]["client"]["id"], "amneziawg", "у Android первым в раздаче идёт AmneziaWG")
        self.assertTrue(opts[0]["recommended"])
        # только TUIC на десктопе: Hiddify не в выборе, рекомендованного нет
        for plat in ("windows", "macos", "linux"):
            opts = groups.client_options(cat, plat, ["tuic"])
            self.assertNotIn("hiddify", [o["client"]["id"] for o in opts])
            self.assertFalse(any(o["recommended"] for o in opts))

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
                                 "--client", "android=happ", "--allow", "whatsapp", "--allow", "Discord.exe")
        self.assertEqual(code, 0, err)
        g = self.groups_json()["groups"][1]
        self.assertEqual((g["id"], g["protocols"], g["clients"]), ("g1", ["hysteria2", "amneziawg"], {"android": "happ"}))
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


if __name__ == "__main__":
    unittest.main()
