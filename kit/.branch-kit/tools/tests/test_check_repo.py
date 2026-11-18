"""check_repo.py: исходы проверок, итог с вершиной и чистотой рабочей
копии, срок вместе с дочерними процессами, окружение без ключей, коды
выхода 0, 1, 2 и 130."""

import contextlib
import io
import os
import re
import subprocess
import sys
import time
import unittest
from unittest import mock

import helpers
import check_repo
import kit_common as kit

SUMMARY = re.compile(r"^проверок (\d+)(?: из (\d+), выполнены не все)?: "
                     r"прошли (\d+), не прошли (\d+); вершина ([0-9a-f]{8}); "
                     r"(рабочая копия чистая|В РАБОЧЕЙ КОПИИ ЕСТЬ "
                     r"НЕСОХРАНЁННЫЕ ИЗМЕНЕНИЯ: изменённых отслеживаемых "
                     r"файлов \d+, новых файлов \d+)$")
DIRTY = ("В РАБОЧЕЙ КОПИИ ЕСТЬ НЕСОХРАНЁННЫЕ ИЗМЕНЕНИЯ: изменённых "
         "отслеживаемых файлов %d, новых файлов %d")
MOVED = ("проверок %d: прошли %d, не прошли %d; ВЕРШИНА СМЕНИЛАСЬ ЗА ВРЕМЯ "
         "ПРОВЕРОК, ИТОГ НЕ ПРОШЁЛ: до %s, после %s; %s")

# Сценарии проверок пишутся файлами: одна команда «python <файл>» годится
# и для sh, и для cmd.
LINES30 = "for i in range(1, 31):\n    print('line %d' % i)\nraise " \
          "SystemExit(3)\n"
# Дочерний процесс — в переменной: объект Popen без ссылки уничтожается
# сразу, и при живом процессе это ResourceWarning.
SLOW = ("import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, 'child.py'])\n"
        "time.sleep(60)\n")
CHILD = ("import time\n"
         "time.sleep(4)\n"
         "open('marker.txt', 'w').close()\n")
ENV = ("import os\n"
       "print('keys:', 'GITLAB_TOKEN' in os.environ,\n"
       "      'BRANCH_KIT_AUTHOR_TOKEN' in os.environ,\n"
       "      'OTHER_VAR' in os.environ)\n")


def checks(*items):
    return helpers.config(проверки=list(items))


class CheckRepoTest(unittest.TestCase):

    def clone(self, cfg, **kwargs):
        clone = helpers.Clone(cfg, files=dict({
            "lines30.py": LINES30, "slow.py": SLOW, "child.py": CHILD,
            "env.py": ENV}, **kwargs.get("files", {})))
        self.addCleanup(clone.remove)
        return clone

    def summary(self, run):
        m = SUMMARY.match(run.last)
        self.assertTrue(m, run)
        return m

    def test_all_pass(self):
        """Все прошли: код 0, строка на проверку, итог с вершиной."""
        clone = self.clone(checks(
            {"имя": "docs", "команда":
             "python .branch-kit/tools/check_docs.py",
             "успех": {"код выхода": 0, "строка в выводе": "нарушений 0"}},
            {"имя": "hello", "команда": "python -c \"print('hello')\""}))
        run = clone.run("check_repo.py")
        self.assertEqual(run.code, 0, run)
        self.assertRegex(run.lines[0], r"^docs: прошла; код выхода 0; "
                         r"[\d,]+ с; вывод — \.git/branch-kit/check_repo/"
                         r"docs\.log$")
        self.assertTrue(run.lines[1].startswith("hello: прошла;"), run)
        m = self.summary(run)
        self.assertEqual(m.group(1, 3, 4), ("2", "2", "0"))
        self.assertIsNone(m.group(2))
        self.assertEqual(m.group(5), clone.git("rev-parse", "HEAD")[:8])
        self.assertEqual(m.group(6), "рабочая копия чистая")
        with open(os.path.join(clone.git_dir(), "branch-kit", "check_repo",
                               "hello.log"), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "hello")

    def test_docs_no_files(self):
        """check_docs.py без единого файла — код 2: о такой проверке итог не
        пишет «прошла», а причина видна в хвосте вывода."""
        cfg = checks({"имя": "docs", "команда":
                      "python .branch-kit/tools/check_docs.py",
                      "успех": {"код выхода": 0,
                                "строка в выводе": "нарушений 0"}})
        cfg["формат документов"]["файлы"] = ["docs/"]
        run = self.clone(cfg).run("check_repo.py")
        self.assertEqual(run.code, 1, run)
        self.assertTrue(run.lines[0].startswith(
            "docs: НЕ ПРОШЛА — нужен код выхода 0; код выхода 2;"), run)
        self.assertIn("проверка не выполнена", run.out)
        self.assertEqual(self.summary(run).group(3, 4), ("0", "1"))

    def test_log_not_opened(self):
        """Файл вывода не открылся: на его месте каталог, на месте каталога
        вывода — файл. Код 2, проверка названа, трассировки нет; опорный —
        без помехи та же проверка проходит."""
        clone = self.clone(checks({"имя": "one", "команда":
                                   "python -c \"print(1)\""}))
        self.assertEqual(clone.run("check_repo.py").code, 0)
        logs = os.path.join(clone.git_dir(), "branch-kit", "check_repo")
        os.remove(os.path.join(logs, "one.log"))
        os.makedirs(os.path.join(logs, "one.log"))
        run = clone.run("check_repo.py")
        self.assertEqual(run.code, 2, run)
        self.assertIn("проверка one", run.err)
        self.assertIn("файл вывода", run.err)
        self.assertNotIn("Traceback", run.err)
        helpers.remove_tree(logs)
        with open(logs, "w", encoding="utf-8") as f:
            f.write("файл на месте каталога")
        run = clone.run("check_repo.py")
        self.assertEqual(run.code, 2, run)
        self.assertIn("проверка one", run.err)
        self.assertNotIn("Traceback", run.err)
        os.remove(logs)
        self.assertEqual(clone.run("check_repo.py").code, 0)

    def test_limit_inf(self):
        """Срок .inf проходит сверку настройки, а ждать столько система не
        умеет: код 2 с именем проверки, команда не запускалась. Опорный — с
        конечным сроком та же команда выполняется."""
        marker = "python -c \"open('started.txt', 'w').close()\""
        clone = self.clone(checks({"имя": "one", "команда": marker,
                                   "не дольше": float("inf")}))
        run = clone.run("check_repo.py")
        self.assertEqual(run.code, 2, run)
        self.assertIn("проверка one", run.err)
        self.assertIn("не дольше", run.err)
        self.assertNotIn("Traceback", run.err)
        self.assertFalse(os.path.exists(clone.path("started.txt")))
        clone.set_config(checks({"имя": "one", "команда": marker,
                                 "не дольше": 1}))
        self.assertEqual(clone.run("check_repo.py").code, 0)
        self.assertTrue(os.path.exists(clone.path("started.txt")))

    def test_failed_tail(self):
        """Не прошла: код 1, последние 20 строк вывода, полный вывод — в
        файле."""
        clone = self.clone(checks({"имя": "lines", "команда":
                                   "python lines30.py"}))
        run = clone.run("check_repo.py")
        self.assertEqual(run.code, 1, run)
        self.assertTrue(run.lines[0].startswith(
            "lines: НЕ ПРОШЛА — нужен код выхода 0; код выхода 3;"), run)
        tail = [l for l in run.lines if l.startswith("  | ")]
        self.assertEqual(tail, ["  | line %d" % i for i in range(11, 31)])
        with open(os.path.join(clone.git_dir(), "branch-kit", "check_repo",
                               "lines.log"), encoding="utf-8") as f:
            self.assertEqual(f.read().split(), [w for i in range(1, 31)
                                                for w in ("line", str(i))])
        m = self.summary(run)
        self.assertEqual(m.group(3, 4), ("0", "1"))

    def test_success_fields(self):
        """«код выхода» другой, чем 0; «строка в выводе» — строка целиком."""
        clone = self.clone(checks(
            {"имя": "code3", "команда": "python lines30.py",
             "успех": {"код выхода": 3}},
            {"имя": "whole", "команда": "python lines30.py",
             "успех": {"код выхода": 3, "строка в выводе": "line 30"}},
            {"имя": "part", "команда": "python lines30.py",
             "успех": {"код выхода": 3, "строка в выводе": "ine 30"}},
            {"имя": "absent", "команда": "python lines30.py",
             "успех": {"код выхода": 3, "строка в выводе": "line 31"}}))
        run = clone.run("check_repo.py")
        self.assertEqual(run.code, 1, run)
        outcome = dict(l.split(": ", 1) for l in run.lines
                       if re.match(r"^[a-z0-9]+: ", l))
        self.assertTrue(outcome["code3"].startswith("прошла"))
        self.assertTrue(outcome["whole"].startswith("прошла"))
        self.assertTrue(outcome["part"].startswith(
            "НЕ ПРОШЛА — нет строки «ine 30» в выводе (строка целиком);"),
            run)
        self.assertTrue(outcome["absent"].startswith(
            "НЕ ПРОШЛА — нет строки «line 31» в выводе (строка целиком);"),
            run)

    def test_timeout_kills_children(self):
        """Срок вышел: проверка прервана вместе с дочерним процессом, код 1;
        следующая проверка выполняется."""
        clone = self.clone(checks(
            {"имя": "slow", "команда": "python slow.py", "не дольше": 0.03},
            {"имя": "next", "команда": "python -c \"print(1)\""}))
        began = time.monotonic()
        run = clone.run("check_repo.py")
        self.assertLess(time.monotonic() - began, 30, run)
        self.assertEqual(run.code, 1, run)
        self.assertTrue(run.lines[0].startswith(
            "slow: НЕ ПРОШЛА — прервана по сроку 0,03 мин; код выхода нет "
            "(прервана);"), run)
        self.assertTrue(any(l.startswith("next: прошла") for l in run.lines))
        time.sleep(6)
        self.assertFalse(os.path.exists(clone.path("marker.txt")),
                         "дочерний процесс проверки не прерван")

    def test_os_fields(self):
        """Поля windows и linux: берётся поле своей системы."""
        clone = self.clone(checks(
            {"имя": "os", "windows": "echo field-windows",
             "linux": "echo field-linux"}))
        run = clone.run("check_repo.py")
        self.assertEqual(run.code, 0, run)
        with open(os.path.join(clone.git_dir(), "branch-kit", "check_repo",
                               "os.log"), encoding="utf-8") as f:
            text = f.read()
        mine = "field-windows" if os.name == "nt" else "field-linux"
        other = "field-linux" if os.name == "nt" else "field-windows"
        self.assertIn(mine, text)
        self.assertNotIn(other, text)

    def test_same_python(self):
        """Команда «python …» выполняется тем же Python, что и средство, а
        не тем, что первым найдётся в PATH."""
        clone = self.clone(checks(
            {"имя": "exe", "команда":
             "python -c \"import sys; print(sys.executable)\""}))
        fake = clone.root + "-bin"
        os.makedirs(fake)
        self.addCleanup(helpers.remove_tree, fake)
        with open(os.path.join(fake, "python.bat"), "w") as f:
            f.write("@echo fake-python")
        script = os.path.join(fake, "python")
        with open(script, "w", newline="") as f:
            f.write(chr(10).join(["#!/bin/sh", "echo fake-python", ""]))
        os.chmod(script, 0o755)
        run = clone.run("check_repo.py", env={
            "PATH": fake + os.pathsep + os.environ.get("PATH", "")})
        self.assertEqual(run.code, 0, run)
        with open(os.path.join(clone.git_dir(), "branch-kit", "check_repo",
                               "exe.log"), encoding="utf-8") as f:
            used = f.read().strip()
        self.assertEqual(os.path.normcase(os.path.realpath(used)),
                         os.path.normcase(os.path.realpath(sys.executable)))

    def test_env_without_keys(self):
        """Окружение проверки — без GITLAB_TOKEN и переменных ключей из
        настройки; ключей нет ни в выводе, ни в файле вывода."""
        clone = self.clone(checks(
            {"имя": "env", "команда": "python env.py",
             "успех": {"строка в выводе": "keys: False False True"}}))
        token = helpers.TOKENS["ai-dev"]
        run = clone.run("check_repo.py", token=token,
                        env={"BRANCH_KIT_AUTHOR_TOKEN": token + "-2",
                             "OTHER_VAR": "1"})
        self.assertEqual(run.code, 0, run)
        with open(os.path.join(clone.git_dir(), "branch-kit", "check_repo",
                               "env.log"), encoding="utf-8") as f:
            log = f.read()
        self.assertNotIn(token, run.out + run.err + log)

    def test_named_checks(self):
        """С именами — только названные, итог говорит, что не все."""
        clone = self.clone(checks(
            {"имя": "first", "команда": "python -c \"print(1)\""},
            {"имя": "second", "команда": "python lines30.py"}))
        run = clone.run("check_repo.py", "first")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(len(run.lines), 2, run)
        m = self.summary(run)
        self.assertEqual(m.group(1, 2, 3, 4), ("1", "2", "1", "0"))
        run = clone.run("check_repo.py", "third")
        self.assertEqual(run.code, 2, run)
        self.assertIn("нет проверки «third»; в настройке есть: first, "
                      "second", run.err)

    def test_dirty(self):
        """Изменённые отслеживаемые и новые файлы — в итоге
        «В РАБОЧЕЙ КОПИИ ЕСТЬ НЕСОХРАНЁННЫЕ ИЗМЕНЕНИЯ» и число тех и
        других отдельно; новый каталог — числом файлов в нём, а
        переименование — одним изменённым файлом."""
        clone = self.clone(checks({"имя": "one", "команда":
                                   "python -c \"print(1)\""}))
        self.assertEqual(self.summary(clone.run("check_repo.py")).group(6),
                         "рабочая копия чистая")
        cases = (("изменён", ["README.md"], [], (1, 0)),
                 ("новый", [], ["new.txt"], (0, 1)),
                 ("оба и каталог", ["README.md", "lines30.py"],
                  ["new/a.txt", "new/b.txt"], (2, 2)))
        for name, changed, new, counts in cases:
            with self.subTest(name):
                for rel in changed + new:
                    clone.write(rel, "изменено\n")
                run = clone.run("check_repo.py")
                self.assertEqual(run.code, 0, run)
                self.assertEqual(self.summary(run).group(6), DIRTY % counts)
                clone.git("checkout", "--", ".")
                clone.git("clean", "-fdq")
        clone.git("mv", "env.py", "env2.py")
        run = clone.run("check_repo.py")
        self.assertEqual(self.summary(run).group(6), DIRTY % (1, 0))

    def test_check_dirties_copy(self):
        """Проверка, которая оставила файл в рабочей копии, делает итог
        несохранённым: состояние до проверок и после них складывается."""
        clone = self.clone(checks(
            {"имя": "litter", "команда":
             "python -c \"open('out.txt', 'w').close()\""}))
        clone.write("README.md", "изменено\n")
        run = clone.run("check_repo.py")
        self.assertEqual(self.summary(run).group(6), DIRTY % (1, 1))

    def test_vertex_moved(self):
        """Проверка сменила вершину: итог не прошёл, код 1, названы обе
        вершины, даже если сама команда вернула код успеха."""
        clone = self.clone(checks(
            {"имя": "moves", "команда":
             "git commit -q --allow-empty -m change"},
            {"имя": "hello", "команда": "python -c \"print('hello')\""}))
        before = clone.git("rev-parse", "HEAD")
        run = clone.run("check_repo.py")
        after = clone.git("rev-parse", "HEAD")
        self.assertNotEqual(before, after)
        self.assertEqual(run.code, 1, run)
        self.assertTrue(run.lines[0].startswith("moves: прошла;"), run)
        self.assertEqual(run.last, MOVED % (2, 2, 0, before[:8], after[:8],
                                            "рабочая копия чистая"))

    def test_vertex_moved_and_failed(self):
        """Вершина сменилась и проверка не прошла: код тот же, в итоге —
        и то, и другое."""
        clone = self.clone(checks(
            {"имя": "moves", "команда":
             "git commit -q --allow-empty -m change && exit 3"}))
        before = clone.git("rev-parse", "HEAD")
        run = clone.run("check_repo.py")
        self.assertEqual(run.code, 1, run)
        self.assertEqual(run.last, MOVED % (1, 0, 1, before[:8],
                                            clone.git("rev-parse", "HEAD")[:8],
                                            "рабочая копия чистая"))

    def test_vertex_kept(self):
        """Опорный: вершина не менялась — итог прежний, код 0; условие
        «строка в выводе» работает по-прежнему."""
        clone = self.clone(checks(
            {"имя": "hello", "команда": "python -c \"print('hello')\"",
             "успех": {"строка в выводе": "hello"}}))
        run = clone.run("check_repo.py")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(self.summary(run).group(5),
                         clone.git("rev-parse", "HEAD")[:8])
        self.assertNotIn("СМЕНИЛАСЬ", run.out)

    def test_dirty_before_checks(self):
        """Изменение, которое было до проверок, итог несёт, даже если
        проверка его убрала: проверялось не сохранённое состояние."""
        clone = self.clone(checks({"имя": "restore", "команда":
                                   "git checkout -- README.md"}))
        clone.write("README.md", "изменено\n")
        run = clone.run("check_repo.py")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(clone.git("status", "--porcelain"), "")
        self.assertEqual(self.summary(run).group(6), DIRTY % (1, 0))

    @unittest.skipUnless(os.name == "nt", "cmd и кодировка OEM — в Windows")
    def test_cmd_output_in_oem(self):
        """Сообщение cmd о несуществующей команде — в кодировке OEM, а не
        ANSI: в последних строках вывода оно читается. Где сообщение cmd —
        только ASCII, проверка проходит и без этого."""
        clone = self.clone(checks({"имя": "nocmd", "команда":
                                   "nonexistent_cmd_bk_probe"}))
        run = clone.run("check_repo.py")
        self.assertEqual(run.code, 1, run)
        with open(os.path.join(clone.git_dir(), "branch-kit", "check_repo",
                               "nocmd.log"), "rb") as f:
            raw = [l for l in f.read().split(b"\n")
                   if b"nonexistent_cmd_bk_probe" in l]
        self.assertEqual(len(raw), 1, raw)
        self.assertIn("  | " + raw[0].decode("oem").rstrip("\r"), run.lines)

    def test_worktree(self):
        """Рабочая копия git worktree: вывод — в каталоге git этой копии."""
        clone = self.clone(checks({"имя": "one", "команда":
                                   "python -c \"print(1)\""}))
        tree = clone.root + "-tree"
        clone.git("worktree", "add", "-q", tree)
        self.addCleanup(helpers.remove_tree, tree)
        run = clone.run("check_repo.py", cwd=tree,
                        tools=os.path.join(tree, ".branch-kit", "tools"))
        self.assertEqual(run.code, 0, run)
        tree_git = os.path.normpath(os.path.join(tree, helpers.git(
            tree, "rev-parse", "--git-dir")))
        self.assertTrue(os.path.isfile(os.path.join(
            tree_git, "branch-kit", "check_repo", "one.log")))
        self.assertIn(os.path.join("worktrees", ""), tree_git + os.sep)
        self.assertEqual(self.summary(run).group(6), "рабочая копия чистая")

    def test_config_only(self):
        clone = self.clone(checks({"имя": "one", "команда": "exit 1"}))
        run = clone.run("check_repo.py", "--config-only")
        self.assertEqual(run.code, 0, run)
        self.assertFalse(os.path.exists(os.path.join(
            clone.git_dir(), "branch-kit", "check_repo")))

    def test_no_checks(self):
        clone = self.clone(checks())
        run = clone.run("check_repo.py")
        self.assertEqual(run.code, 2, run)
        self.assertIn("проверок нет", run.err)

    def test_not_a_clone(self):
        clone = self.clone(checks({"имя": "one", "команда": "exit 0"}))
        plain = clone.root + "-plain"
        os.makedirs(os.path.join(plain, ".branch-kit"))
        self.addCleanup(helpers.remove_tree, plain)
        import shutil
        shutil.copytree(clone.path(".branch-kit/tools"),
                        os.path.join(plain, ".branch-kit", "tools"))
        shutil.copy(clone.config_path, os.path.join(plain, ".branch-kit"))
        run = clone.run("check_repo.py", cwd=plain,
                        tools=os.path.join(plain, ".branch-kit", "tools"))
        self.assertEqual(run.code, 2, run)
        self.assertIn("средства лежат не в клоне git", run.err)


class InProcessTest(unittest.TestCase):
    """Случаи, которые нельзя вызвать отдельным процессом надёжно."""

    def setUp(self):
        self.clone = helpers.Clone(checks({"имя": "one", "команда":
                                           "python -c \"print(1)\""}))
        self.addCleanup(self.clone.remove)
        patcher = mock.patch.object(kit, "clone_root",
                                    return_value=self.clone.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def main(self):
        return check_repo.main(["--config", self.clone.config_path])

    def test_not_started(self):
        """Команда не запустилась — код 2."""
        real_popen = subprocess.Popen

        def popen(*args, **kwargs):
            if kwargs.get("shell"):
                raise OSError("нет cmd")
            return real_popen(*args, **kwargs)

        with mock.patch("subprocess.Popen", side_effect=popen):
            with mock.patch("sys.stderr") as err:
                code = self.main()
        self.assertEqual(code, 2)
        self.assertIn("команда не запустилась",
                      "".join(str(c) for c in err.write.call_args_list))

    def test_ctrl_c(self):
        """Ctrl+C во время проверки — команда прервана вместе с дочерними,
        код 130."""
        real_popen = subprocess.Popen
        started = []

        def popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            if kwargs.get("shell"):
                started.append(proc)
                proc.wait = mock.Mock(side_effect=[KeyboardInterrupt, 0, 0])
            return proc

        with mock.patch("subprocess.Popen", side_effect=popen):
            with mock.patch.object(check_repo, "kill_tree") as kill:
                with mock.patch("sys.stderr"), mock.patch("sys.stdout"):
                    code = self.main()
        self.assertEqual(code, 130)
        kill.assert_called_once_with(started[0])
        started[0].kill()
        # Ожидание настоящее, а не подменённое: процесс без кода выхода
        # дал бы при уборке мусора ResourceWarning.
        real_popen.wait(started[0], timeout=30)

    def test_crash(self):
        """Необработанное исключение — код 2, вид и текст в потоке ошибок,
        трассировки и ключа нет; падение и в итоговой строке — тоже."""
        token = helpers.TOKENS["ai-dev"]
        error = RuntimeError("сбой, ключ %s" % token)
        for place in ("run_check", "dirty_text"):
            with self.subTest(place):
                err = io.StringIO()
                with helpers.patch_env(GITLAB_TOKEN=token), \
                        mock.patch.object(check_repo, place,
                                          side_effect=error), \
                        contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(err):
                    code = self.main()
                self.assertEqual(code, 2)
                self.assertIn("RuntimeError: сбой, ключ", err.getvalue())
                self.assertNotIn(token, err.getvalue())
                self.assertNotIn("Traceback", err.getvalue())


class VertexTextTest(unittest.TestCase):

    def test_no_changes_yet(self):
        """Изменений ещё нет: вершины нет ни до проверок, ни после; первое
        изменение проверка сохранила сама — вершина сменилась."""
        self.assertEqual(check_repo.vertex_text(None, None),
                         "вершина — изменений нет")
        self.assertEqual(check_repo.vertex_text(None, "a" * 40), (
            "ВЕРШИНА СМЕНИЛАСЬ ЗА ВРЕМЯ ПРОВЕРОК, ИТОГ НЕ ПРОШЁЛ: до — "
            "изменений нет, после aaaaaaaa"))


class DecodeTest(unittest.TestCase):
    """Вывод команды не в UTF-8. OEM и ANSI Windows изображают cp866 и
    cp1251 — так проверка одна на любой системе."""

    def test_oem_and_ansi_lines(self):
        """Строка за строкой: строка cmd в OEM, строка Python в ANSI и
        строка в UTF-8 в одном выводе читаются каждая в своей кодировке."""
        lines = ["Команда не найдена: ошибка",
                 "Проверка не прошла: ошибка в строке 3",
                 "готово"]
        raw = (lines[0].encode("cp866") + b"\r\n"
               + lines[1].encode("cp1251") + b"\n"
               + lines[2].encode("utf-8") + b"\n")
        with mock.patch.object(check_repo, "legacy_encodings",
                               return_value=["cp866", "cp1251"]):
            self.assertEqual(check_repo.decode(raw).splitlines(), lines)

    def test_windows_encodings(self):
        """В Windows после UTF-8 — OEM, затем ANSI; в Linux — кодировка
        системы."""
        if os.name == "nt":
            self.assertEqual(check_repo.legacy_encodings()[0], "oem")
            self.assertEqual(len(check_repo.legacy_encodings()), 2)
        else:
            self.assertEqual(len(check_repo.legacy_encodings()), 1)


if __name__ == "__main__":
    unittest.main()
