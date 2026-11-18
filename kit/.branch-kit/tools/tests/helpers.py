"""Общее для проверок средств: временный клон с комплектом, настройка,
запуск средства отдельным процессом.

Средства копируются во временный клон, в .branch-kit/tools/, как в
репозитории, где стоит комплект: корень клона, настройка и каталог git у
средства тогда свои, а не этого репозитория.
"""

import copy
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from unittest import mock

import yaml

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.dirname(TESTS_DIR)
KIT_DIR = os.path.dirname(TOOLS_DIR)
sys.path.insert(0, TOOLS_DIR)

TOOL_FILES = ("kit_common.py", "check_repo.py", "check_docs.py", "poll.py",
              "check_merge_log.py", "owner_protect_branches.py",
              "owner_check_settings.py")

# Ключи — выдуманные; в каждой проверке с ключом сверяется, что его нет в
# выводе.
TOKENS = {
    "ai-dev": "glpat-TEST-dev-0123456789",
    "ai-reviewer": "glpat-TEST-reviewer-0123456789",
    "ai-planner": "glpat-TEST-planner-0123456789",
    "owner": "glpat-TEST-owner-0123456789",
    "stranger": "glpat-TEST-stranger-0123456789",
}

CONFIG = {
    "комплект": {"источник": "http://127.0.0.1/kit/branch-kit.git",
                 "изменение": "1a2b3c4d"},
    "площадка": {"адрес": "http://127.0.0.1:9", "группа": "example",
                 "репозиторий": "example/app"},
    "основная ветка": "main",
    "ветки задач": [{"префикс": "docs/", "что": "документы"},
                    {"префикс": "feat/", "что": "новое"}],
    "учётные записи": {
        "владелец": "owner",
        "постановщик": {"учётная запись": "ai-planner",
                        "переменная ключа": "BRANCH_KIT_PLANNER_TOKEN"},
        "авторы": [{"учётная запись": "ai-dev",
                    "переменная ключа": "BRANCH_KIT_AUTHOR_TOKEN",
                    "подпись": {"имя": "AI Developer",
                                "почта": "ai-dev@example.invalid"}}],
        "проверяющий": {"учётная запись": "ai-reviewer",
                        "переменная ключа": "BRANCH_KIT_REVIEWER_TOKEN",
                        "подпись": {"имя": "AI Reviewer",
                                    "почта": "ai-reviewer@example.invalid"}},
        "люди": [],
    },
    "проверки": [{"имя": "docs",
                  "команда": "python .branch-kit/tools/check_docs.py",
                  "успех": {"код выхода": 0,
                            "строка в выводе": "нарушений 0"}}],
    "конвейер": {"есть": False, "статусы": []},
    "опрос": {"период": 0.002, "не дольше": 0},
    "рабочие каталоги": {"переменная базы": "BRANCH_KIT_WORK_ROOT"},
    "реестр отложенного": ".branch-kit/deferred.md",
    "формат документов": {
        "файлы": ["*.md"],
        "кодировка": "utf-8 без BOM",
        "концы строк": "LF",
        "перевод строки в конце": True,
        "длина строки": 78,
        "ссылки": True,
        "yaml": True,
        "слова": [{"слово": "фича", "образец": r"\bфич\w*",
                   "вместо": "новая возможность"}],
    },
}


def config(**changes):
    """Заполненная настройка; changes — поля верхнего уровня, None —
    убрать поле."""
    data = copy.deepcopy(CONFIG)
    for key, value in changes.items():
        key = key.replace("_", " ")
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


def dump(data):
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def base_env(**extra):
    """Окружение запуска средства. Кодировка вывода враждебная — ascii:
    средства обязаны перенастроить потоки на UTF-8 сами. Прокси —
    несуществующий, а 127.0.0.1 назван в NO_PROXY сетью: средство,
    которое не понимает такой записи, к поддельной площадке не дойдёт."""
    env = dict((k, v) for k, v in os.environ.items()
               if not k.upper().endswith("_PROXY")
               and k.upper() not in ("GITLAB_TOKEN", "GITLAB_HOST",
                                     "PYTHONUTF8", "PYTHONIOENCODING")
               and not k.upper().startswith("BRANCH_KIT_"))
    env.update({"PYTHONIOENCODING": "ascii",
                "HTTP_PROXY": "http://127.0.0.1:9",
                "HTTPS_PROXY": "http://127.0.0.1:9",
                "NO_PROXY": "127.0.0.0/8"})
    env.update(extra)
    return env


def patch_env(**extra):
    """Подмена os.environ для средства, которое выполняется в этом
    процессе: окружение — то же, что у средства отдельным процессом
    (base_env), и только оно. Без clear=True в os.environ остались бы
    переменные машины, которых base_env не задаёт: в Linux имена
    различают регистр, и строчная no_proxy машины — не та переменная,
    что NO_PROXY из base_env, а kit_common.no_proxy_for берёт строчную
    первой."""
    return mock.patch.dict(os.environ, base_env(**extra), clear=True)


class Run:
    def __init__(self, code, out, err):
        self.code = code
        self.out = out
        self.err = err
        self.lines = out.splitlines()

    @property
    def last(self):
        return self.lines[-1] if self.lines else ""

    def __repr__(self):
        return "код %s\n--- вывод:\n%s--- ошибки:\n%s" % (self.code, self.out,
                                                         self.err)


def _writable(func, path, _):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_tree(path):
    shutil.rmtree(path, onerror=_writable)


def git(root, *args, **kwargs):
    out = subprocess.run(["git", "-C", root] + list(args),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         check=kwargs.get("check", True))
    return out.stdout.decode("utf-8").strip()


def new_repo(root):
    os.makedirs(root, exist_ok=True)
    git(root, "init", "-q")
    # Временный репозиторий проверки: подпись выдуманная, настройки машины
    # на него не влияют.
    for key, value in (("user.name", "Test Author"),
                       ("user.email", "author@example.invalid"),
                       ("core.autocrlf", "false"),
                       ("commit.gpgsign", "false")):
        git(root, "config", key, value)


class Clone:
    """Временный клон git с комплектом: средства — в .branch-kit/tools/,
    настройка — .branch-kit/config.yaml; всё сохранено одним
    изменением."""

    def __init__(self, cfg=None, exceptions=None, files=None):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="bk-test-"))
        new_repo(self.root)
        tools = self.path(".branch-kit/tools")
        os.makedirs(tools)
        for name in TOOL_FILES:
            shutil.copy(os.path.join(TOOLS_DIR, name), tools)
        self.config_path = self.path(".branch-kit/config.yaml")
        self.exceptions_path = self.path(".branch-kit/exceptions.yaml")
        self.set_config(config() if cfg is None else cfg)
        self.write(".branch-kit/exceptions.yaml",
                   exceptions if exceptions is not None
                   else "исключения: []\n")
        self.write("README.md", "# Пример\n")
        for rel, content in (files or {}).items():
            self.write(rel, content)
        self.commit("начальное изменение")

    def path(self, rel):
        return os.path.join(self.root, *rel.split("/"))

    def write(self, rel, content):
        path = self.path(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else content
        with open(path, "wb") as f:
            f.write(data)

    def read(self, rel):
        with open(self.path(rel), "rb") as f:
            return f.read()

    def set_config(self, cfg):
        self.write(".branch-kit/config.yaml",
                   cfg if isinstance(cfg, str) else dump(cfg))

    def git(self, *args, **kwargs):
        return git(self.root, *args, **kwargs)

    def commit(self, message="изменение"):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def git_dir(self):
        return os.path.normpath(os.path.join(
            self.root, self.git("rev-parse", "--git-dir")))

    def run(self, tool, *args, **kwargs):
        """Средство отдельным процессом из корня клона; вывод — строго
        UTF-8."""
        env = base_env(**kwargs.get("env", {}))
        if kwargs.get("token") is not None:
            env["GITLAB_TOKEN"] = kwargs["token"]
        script = os.path.join(kwargs.get("tools", self.path(
            ".branch-kit/tools")), tool)
        proc = subprocess.run([sys.executable, "-B", script] + list(args),
                              cwd=kwargs.get("cwd", self.root), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=kwargs.get("timeout", 120))
        return Run(proc.returncode, proc.stdout.decode("utf-8"),
                   proc.stderr.decode("utf-8"))

    def remove(self):
        remove_tree(self.root)
