"""Защита веток задач: первый запуск ставит правила, повторный ничего не
меняет, отличающееся правило — расхождение без правки, ключ не владельца
— код 2 без единого обращения к правилам. Основная ветка и настройки
слияния только сверяются. Площадку изображает поддельная площадка."""

import contextlib
import io
import unittest
from unittest import mock

import helpers
import fake_api
import owner_protect_branches as opb
from fake_api import rule

TOKEN = helpers.TOKENS["owner"]
TASK_TEXT = ("отправка: Developer и Maintainer; слияние: Developer и "
             "Maintainer; принудительная отправка: запрещена")
MAIN_TEXT = ("отправка: никто; слияние: Maintainer; принудительная "
             "отправка: запрещена")


class ProtectTest(unittest.TestCase):

    def setUp(self):
        self.platform = fake_api.FakePlatform()
        self.addCleanup(self.platform.stop)
        self.platform.user(TOKEN, "owner")
        self.platform.user(helpers.TOKENS["ai-reviewer"], "ai-reviewer")
        self.repo = self.platform.repo(self.platform.project)
        self.repo.protected["main"] = rule("main", 0, 40)
        cfg = helpers.config()
        cfg["площадка"]["адрес"] = self.platform.url
        self.clone = helpers.Clone(cfg)
        self.addCleanup(self.clone.remove)

    def run_tool(self, *args, **kwargs):
        run = self.clone.run("owner_protect_branches.py", *args,
                             token=kwargs.get("token", TOKEN))
        for value in helpers.TOKENS.values():
            self.assertNotIn(value, run.out + run.err)
        return run

    def posts(self):
        return [w for w in self.platform.written("POST")
                if w[1].endswith("/protected_branches")]

    def test_first_and_repeated_run(self):
        """Первый запуск ставит правило на каждый префикс; «стало» — ответ
        площадки; повторный ничего не меняет; код 0 оба раза."""
        run = self.run_tool()
        self.assertEqual(run.code, 0, run)
        self.assertEqual([w[3] for w in self.posts()], [
            {"name": "docs/*", "push_access_level": 30,
             "merge_access_level": 30, "allow_force_push": False},
            {"name": "feat/*", "push_access_level": 30,
             "merge_access_level": 30, "allow_force_push": False}])
        self.assertIn("ветки docs/*: было — нет правила; стало — %s"
                      % TASK_TEXT, run.lines)
        self.assertIn("основная ветка main: было — %s; стало — без "
                      "изменений, средство её только сверяет — как нужно"
                      % MAIN_TEXT, run.lines)
        self.assertIn("способ слияния: merge — как нужно", run.lines)
        self.assertEqual(run.last, "итог: правил веток задач 2: создано 2, "
                         "без изменений 0; расхождений 0")
        self.assertEqual(sorted(self.repo.protected),
                         ["docs/*", "feat/*", "main"])
        run = self.run_tool()
        self.assertEqual(run.code, 0, run)
        self.assertEqual(len(self.posts()), 2)
        self.assertIn("ветки feat/*: было — %s; стало — без изменений"
                      % TASK_TEXT, run.lines)
        self.assertEqual(run.last, "итог: правил веток задач 2: создано 0, "
                         "без изменений 2; расхождений 0")

    def test_different_rule_untouched(self):
        """Правило, которое есть и отличается, не меняется: расхождение,
        код 1; недостающее при этом ставится."""
        self.repo.protected["feat/*"] = rule("feat/*", 30, 30, force=True)
        run = self.run_tool()
        self.assertEqual(run.code, 1, run)
        self.assertEqual([w[3]["name"] for w in self.posts()], ["docs/*"])
        self.assertTrue(self.repo.protected["feat/*"]["allow_force_push"])
        line = [x for x in run.lines if x.startswith("ветки feat/*")][0]
        self.assertIn("принудительная отправка: разрешена; стало — без "
                      "изменений: РАСХОЖДЕНИЕ", line)
        self.assertEqual(run.last, "итог: правил веток задач 2: создано 1, "
                         "без изменений 1; расхождений 1")

    def test_platform_answer_is_shown(self):
        """«Стало» — то, что ответила площадка, а не то, что просили:
        площадка поставила Maintainer — расхождение."""
        def store(item):
            item["push_access_levels"] = [fake_api.level(40)]
        self.platform.on_protect = store
        run = self.run_tool()
        self.assertEqual(run.code, 1, run)
        self.assertIn("ветки docs/*: было — нет правила; стало — отправка: "
                      "Maintainer; слияние: Developer и Maintainer; "
                      "принудительная отправка: запрещена; РАСХОЖДЕНИЕ — не "
                      "совпадает с нужным (%s)" % TASK_TEXT, run.lines)

    def test_refused(self):
        """Площадка отказала в правиле — расхождение с её ответом, код 1."""
        self.platform.refuse[("POST", "protected_branches")] = (
            403, "403 Forbidden")
        run = self.run_tool()
        self.assertEqual(run.code, 1, run)
        self.assertIn("ветки docs/*: было — нет правила; стало — "
                      "РАСХОЖДЕНИЕ: площадка отказала: POST "
                      "projects/example%2Fapp/protected_branches: 403 403 "
                      "Forbidden", run.lines)

    def test_dry_run(self):
        """--dry-run ничего не меняет; код 1, пока есть что создать, и 0,
        когда всё уже стоит."""
        run = self.run_tool("--dry-run")
        self.assertEqual(run.code, 1, run)
        self.assertEqual(self.platform.written(), [])
        self.assertIn("ветки docs/*: было — нет правила; стало бы — правило "
                      "будет создано: %s" % TASK_TEXT, run.lines)
        self.assertEqual(run.last, "итог без изменений (--dry-run): правил "
                         "веток задач 2: создать нужно 2, уже есть 0; "
                         "расхождений 0")
        self.run_tool()
        run = self.run_tool("--dry-run")
        self.assertEqual(run.code, 0, run)

    def test_main_and_merge_settings_checked_only(self):
        """Основная ветка и настройки слияния не те — расхождение по
        каждому; средство их не меняет."""
        self.repo.protected["main"] = rule("main", 30, 40, force=True)
        self.repo.protected["m*"] = rule("m*", 30, 30)
        self.repo.settings.update(merge_method="ff",
                                  squash_option="default_on",
                                  remove_source_branch_after_merge=True)
        run = self.run_tool()
        self.assertEqual(run.code, 1, run)
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(self.platform.written("PUT"), [])
        text = "\n".join(run.lines)
        self.assertIn("основная ветка main: было — отправка: Developer и "
                      "Maintainer; слияние: Maintainer; принудительная "
                      "отправка: разрешена; стало — без изменений, средство "
                      "её только сверяет; РАСХОЖДЕНИЕ — нужно: %s"
                      % MAIN_TEXT, text)
        self.assertIn("основная ветка main: РАСХОЖДЕНИЕ — к ней подходит и "
                      "правило m*", text)
        self.assertIn("способ слияния: ff — РАСХОЖДЕНИЕ", text)
        self.assertIn("склейка при слиянии: default_on — РАСХОЖДЕНИЕ", text)
        self.assertIn("удаление ветки после слияния по умолчанию: включено "
                      "— РАСХОЖДЕНИЕ", text)
        self.assertTrue(run.last.endswith("расхождений 5"), run)

    def test_squash_default_off(self):
        """Склейка разрешена, хоть и не включена по умолчанию (default_off),
        — расхождение: нужна только never, «Do not allow»."""
        self.repo.settings["squash_option"] = "default_off"
        run = self.run_tool()
        self.assertEqual(run.code, 1, run)
        self.assertIn("склейка при слиянии: default_off — РАСХОЖДЕНИЕ: "
                      "нужно never — «Do not allow», склейка запрещена; "
                      "поправьте сами в Settings → Merge requests", run.lines)
        self.assertEqual(self.platform.written("PUT"), [])
        self.assertTrue(run.last.endswith("расхождений 1"), run)

    def test_missing_settings_field(self):
        """Площадка не дала поле настройки — не «как нужно», а
        расхождение."""
        self.repo.settings["squash_option"] = None
        run = self.run_tool()
        self.assertEqual(run.code, 1, run)
        self.assertIn("склейка при слиянии: нет в ответе площадки — "
                      "РАСХОЖДЕНИЕ", "\n".join(run.lines))
        del self.repo.protected["main"]
        run = self.run_tool()
        self.assertIn("основная ветка main: было — нет правила", run.out)

    def test_not_owner(self):
        """Ключ не владельца — код 2: правила не читаются и не ставятся."""
        run = self.run_tool(token=helpers.TOKENS["ai-reviewer"])
        self.assertEqual(run.code, 2, run)
        self.assertIn("ключ — учётной записи ai-reviewer, а у роли "
                      "«владелец» в настройке — owner", run.err)
        self.assertEqual(self.platform.paths("protected_branches"), [])
        self.assertEqual(self.platform.written(), [])

    def test_ctrl_c(self):
        """Ctrl+C — код 130, и шапка средства его называет; проверяется в
        том же процессе."""
        out, err = io.StringIO(), io.StringIO()
        with helpers.patch_env(GITLAB_TOKEN=TOKEN), \
                mock.patch.object(opb.Protector, "run",
                                  side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = opb.main(["--config", self.clone.config_path])
        self.assertEqual(code, 130)
        self.assertIn("прервано", err.getvalue())
        self.assertEqual(self.platform.written(), [])
        self.assertIn("130 — прервано сочетанием Ctrl+C",
                      " ".join(opb.__doc__.split()))

    def test_platform_fails_on_second_rule(self):
        """Площадка не ответила на постановку второго правила — код 2:
        первое правило поставлено и названо строкой, итога нет."""
        self.platform.when("protected_branches", 4,
                           lambda: setattr(self.platform, "failures", 1))
        run = self.run_tool()
        self.assertEqual(run.code, 2, run)
        self.assertEqual([w[3]["name"] for w in self.posts()],
                         ["docs/*", "feat/*"])
        self.assertEqual(sorted(self.repo.protected), ["docs/*", "main"])
        self.assertIn("ветки docs/*: было — нет правила; стало — %s"
                      % TASK_TEXT, run.lines)
        self.assertIn("правило feat/* не поставлено, площадка не ответила",
                      run.err)
        self.assertFalse([x for x in run.lines if x.startswith("итог")],
                         run)

    def test_not_done(self):
        """Нет ключа, площадка не отвечает — код 2."""
        run = self.run_tool(token="")
        self.assertEqual(run.code, 2, run)
        self.assertEqual(self.platform.calls, [])
        self.platform.when("protected_branches", 1,
                           lambda: setattr(self.platform, "failures", 100))
        run = self.run_tool()
        self.assertEqual(run.code, 2, run)
        self.assertIn("правила защиты и настройки проекта не прочитаны",
                      run.err)
        self.assertEqual(self.platform.written(), [])


if __name__ == "__main__":
    unittest.main()
