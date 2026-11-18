"""Общая часть средств комплекта: настройка, ключ, площадка, исключения.

Средства лежат в .branch-kit/tools/ и запускаются из корня клона:
python .branch-kit/tools/<средство>. Настройка по умолчанию —
.branch-kit/config.yaml рядом с каталогом средств, исключения —
.branch-kit/exceptions.yaml там же; корень клона — корень рабочей копии
git, в которой лежат средства. Так средство, его настройка и клон всегда
из одной рабочей копии, из какого бы каталога его ни запустили.

Ключ к площадке берётся из переменной окружения GITLAB_TOKEN и не
выводится нигде: ни в сообщения, ни в адреса запросов (AGENTS.md, 6.3).
Подробно — README.md рядом.
"""

import argparse
import fnmatch
import http.client
import ipaddress
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import warnings

try:
    import yaml
except ImportError:  # сообщение — в load_config
    yaml = None

OK = 0
FOUND = 1
NOT_DONE = 2
INTERRUPTED = 130

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
KIT_DIR = os.path.dirname(TOOLS_DIR)
DEFAULT_CONFIG = os.path.join(KIT_DIR, "config.yaml")
DEFAULT_EXCEPTIONS = os.path.join(KIT_DIR, "exceptions.yaml")

NO_YAML_TEXT = "нужен PyYAML 5.1 или новее: python -m pip install pyyaml"


class ToolError(Exception):
    """Средство не может выполнить работу: код выхода 2, причина — в поток
    ошибок. Строк причины может быть несколько."""

    def __init__(self, *lines):
        Exception.__init__(self, "\n".join(lines))
        self.lines = lines


def report(error):
    """Причина отказа — в поток ошибок; код выхода 2."""
    for line in error.lines:
        print(line, file=sys.stderr)
    return NOT_DONE


def report_crash(error):
    """Необработанное исключение средства: код выхода 2 — работа не
    выполнена, — а не код 1 с его значением «есть нарушения» или «событий
    нет» и не трассировка. В поток ошибок — вид и текст исключения; ключ,
    попавший в текст, заменён (AGENTS.md, 6.3)."""
    text = "%s: %s" % (error.__class__.__name__, error)
    token = token_from_env()
    if token:
        text = text.replace(token, "<ключ>")
    print("средство не выполнило работу, сбой: %s" % text, file=sys.stderr)
    return NOT_DONE


def setup_output():
    """Вывод — UTF-8 на любой системе. В Windows вывод в канал иначе идёт
    в кодировке системы и падает на знаках вне неё; на переменную
    PYTHONIOENCODING средства не полагаются."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def relative(path, root):
    """Путь от root с косой чертой или None, если путь вне root."""
    try:
        rel = os.path.relpath(os.path.abspath(path), root)
    except ValueError:  # другой диск Windows
        return None
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return rel.replace(os.sep, "/")


def shown(path):
    """Путь для вывода: от текущего каталога, если файл в нём."""
    return relative(path, os.getcwd()) or os.path.abspath(path)


# --- Ключи командной строки по-русски --------------------------------------

_ARGPARSE_RU = {
    "usage: ": "вызов: ",
    "positional arguments": "параметры",
    "optional arguments": "ключи",
    "options": "ключи",
    "show this help message and exit": "показать эту справку и завершиться",
    "%(prog)s: error: %(message)s\n": "%(prog)s: ошибка: %(message)s\n",
    "the following arguments are required: %s": "не заданы обязательные: %s",
    "unrecognized arguments: %s": "неизвестные параметры: %s",
    "argument %(argument_name)s: %(message)s":
        "ключ %(argument_name)s: %(message)s",
    "expected one argument": "нужно значение",
    "invalid choice: %(value)r (choose from %(choices)s)":
        "недопустимое значение %(value)r; можно: %(choices)s",
    "invalid %(type)s value: %(value)r": "недопустимое значение %(value)r",
    "ambiguous option: %(option)s could match %(matches)s":
        "неоднозначный ключ %(option)s: подходят %(matches)s",
}


def _russian(text):
    return _ARGPARSE_RU.get(text, text)


class _Parser(argparse.ArgumentParser):
    """Ошибка в ключе — «ключ <ключ>: <причина>» на любом Python. argparse
    зовёт error(str(err)) из обработчика ArgumentError; причину он уже
    перевёл подменой argparse._, а строку «argument <ключ>: …» вокруг неё
    до Python 3.11 собирает без перевода. Текст собирается заново из
    частей ошибки, а не правкой готовой строки: на 3.11 и новее, где
    str(err) уже по-русски, выходит то же самое, без двойного перевода."""

    def error(self, message):
        err = sys.exc_info()[1]
        if (isinstance(err, argparse.ArgumentError) and message == str(err)
                and err.argument_name is not None):
            message = _russian("argument %(argument_name)s: %(message)s") % {
                "argument_name": err.argument_name, "message": err.message}
        argparse.ArgumentParser.error(self, message)


def make_parser(prog, description):
    """Разбор ключей с подсказками и сообщениями об ошибках по-русски на
    Python 3.8 и новее; ошибка в ключах — код выхода 2."""
    argparse._ = _russian
    return _Parser(prog=prog, description=description)


def add_config_argument(parser):
    parser.add_argument(
        "--config", metavar="<файл>",
        help="настройка; по умолчанию — .branch-kit/config.yaml рядом с "
             "каталогом средств")


# --- git -------------------------------------------------------------------

# Переменные, которыми окружение подменило бы клон: git работал бы не с
# той рабочей копией, в которой лежат средства.
CLONE_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
             "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
             "GIT_NAMESPACE")


def git(root, *args):
    """git в клоне root: (код выхода, вывод, поток ошибок). Клон — всегда
    root: переменные CLONE_ENV git не получает."""
    env = dict((name, value) for name, value in os.environ.items()
               if name.upper() not in CLONE_ENV)
    try:
        out = subprocess.run(["git", "-C", root] + list(args),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             stdin=subprocess.DEVNULL, env=env)
    except OSError as err:
        raise ToolError("git не запускается: %s" % err)
    return (out.returncode, out.stdout.decode("utf-8", "replace"),
            out.stderr.decode("utf-8", "replace"))


def clone_root():
    """Корень рабочей копии git, в которой лежат средства."""
    code, out, _ = git(TOOLS_DIR, "rev-parse", "--show-toplevel")
    if code != 0 or not out.strip():
        raise ToolError("средства лежат не в клоне git: %s" % TOOLS_DIR)
    return os.path.normpath(out.strip())


def git_dir(root):
    """Каталог git рабочей копии: в рабочей копии git worktree .git —
    файл, и каталог берут у git, а не по пути .git/."""
    code, out, err = git(root, "rev-parse", "--git-dir")
    if code != 0 or not out.strip():
        raise ToolError("не найден каталог git клона %s: %s"
                        % (root, err.strip()))
    return os.path.normpath(os.path.join(root, out.strip()))


def head(root):
    """Вершина рабочей копии или None, если изменений ещё нет."""
    code, out, _ = git(root, "rev-parse", "--verify", "-q", "HEAD")
    return out.strip() if code == 0 and out.strip() else None


# --- Настройка -------------------------------------------------------------

PLACE = re.compile(r"<[^<>\s](?:[^<>]*[^<>\s])?>")


class Spec:
    """Вид значения поля настройки и необязательная сверка значения."""
    what = ""

    def __init__(self, check=None):
        self.check = check

    def fits(self, value):
        raise NotImplementedError

    def place(self, value):
        """Незаполненное место «<…>» в строке value или None."""
        found = PLACE.search(value)
        return found.group(0) if found else None


class Text(Spec):
    what = "строка"

    def fits(self, value):
        return isinstance(value, str)


# Синтаксис регулярного выражения с «<»: имя группы в (?P<имя>…) и начало
# (?<=…) и (?<!…). Пара «\ и знак» берётся первой, как в re: в «\(?P<x>»
# скобка — обычный знак, и «<x>» — текст образца.
REGEX_ANGLE = re.compile(r"\\.|\(\?P<\w*>|\(\?<(?=[=!])", re.DOTALL)


def _without_angle_syntax(match):
    text = match.group(0)
    return text if text.startswith("\\") else "(?"


class Pattern(Text):
    """Строка с регулярным выражением: его синтаксис с «<» и «>» местом не
    считается, а «<…>» как текст образца — считается."""

    def place(self, value):
        return Text.place(self, REGEX_ANGLE.sub(_without_angle_syntax,
                                                value))


class Number(Spec):
    what = "число"

    def fits(self, value):
        return (isinstance(value, (int, float))
                and not isinstance(value, bool))


class Integer(Spec):
    what = "целое число"

    def fits(self, value):
        return isinstance(value, int) and not isinstance(value, bool)


class Flag(Spec):
    what = "true или false"

    def fits(self, value):
        return isinstance(value, bool)


class Record(Spec):
    """Запись с полями: [(имя, обязательно ли, вид)]."""
    what = "запись с полями"

    def __init__(self, fields, check=None):
        Spec.__init__(self, check)
        self.fields = fields

    def fits(self, value):
        return isinstance(value, dict)


class List(Spec):
    what = "список"

    def __init__(self, item, nonempty=False, check=None):
        Spec.__init__(self, check)
        self.item = item
        self.nonempty = nonempty

    def fits(self, value):
        return isinstance(value, list)


def _what_is(value):
    if value is None:
        return "пусто"
    if isinstance(value, bool):
        return "true или false"
    if isinstance(value, (int, float)):
        return "число"
    if isinstance(value, str):
        return "строка"
    if isinstance(value, list):
        return "список"
    if isinstance(value, dict):
        return "запись с полями"
    return type(value).__name__


# Пробел или управляющий знак внутри адреса: urlsplit в Python 3.8.10 их
# оставляет, в 3.10.11 отбрасывает табуляцию и перевод строки, в 3.11.9 —
# ещё и остальные в начале адреса.
URL_SPACE = re.compile(r"[\s\x00-\x1f\x7f]")
# Узел в квадратных скобках — адрес IPv6, за «]» — конец или порт.
URL_BRACKETS = re.compile(r"\[([0-9A-Fa-f:]+)\](?::[^\[\]]*)?$")
BRACKETS_TEXT = ("квадратные скобки — только парой вокруг адреса IPv6, "
                 "например http://[::1]:8080")


def _split_url(value):
    """Части адреса и None либо None и что не так. Пробелы по краям
    отбрасываются. Сверка одинакова на всех Python 3.8 и новее: выпуски
    urlsplit по-разному отбрасывают пробелы и управляющие знаки и
    по-разному сверяют узел в квадратных скобках, а адрес с незакрытой
    скобкой не разбирают вовсе (ValueError)."""
    value = value.strip()
    if URL_SPACE.search(value):
        return None, "внутри адреса — пробел или управляющий знак"
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        if "[" in value or "]" in value:
            return None, BRACKETS_TEXT
        return None, "адрес не разбирается"
    if "[" in parts.netloc or "]" in parts.netloc:
        users, _, host = parts.netloc.rpartition("@")
        found = URL_BRACKETS.match(host)
        if "[" in users or "]" in users or not found:
            return None, BRACKETS_TEXT
        try:
            ipaddress.IPv6Address(found.group(1))
        except ValueError:
            return None, BRACKETS_TEXT
    return parts, None


def _url(value):
    parts, problem = _split_url(value)
    if problem:
        return problem
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "адрес — целиком, с http:// или https://"
    if parts.username or parts.password:
        return ("в адресе — учётные данные: ключ сюда не пишут "
                "(AGENTS.md, 6.3)")
    return None


def _env_name(value):
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", value):
        return None
    return "имя переменной окружения — латинские буквы, цифры и «_»"


def _check_name(value):
    if re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", value):
        return None
    return "имя проверки — латиницей: буквы, цифры, «.», «-», «_»"


def _one_of(*allowed):
    def check(value):
        if value in allowed:
            return None
        return "допустимо: %s" % ", ".join(allowed)
    return check


def _positive(value):
    return None if value > 0 else "нужно больше нуля"


def _not_negative(value):
    return None if value >= 0 else "нужно 0 или больше"


# Образец слова: что Python 3.8 и новее разбирают по-разному.
NOT_EVERYWHERE_TEXT = "образец разбирается не на всех Python 3.8 и новее: "
# Повтор {m,n} так, как его понимает re; другое после «{» — обычные знаки.
REPEAT = re.compile(r"\{(?:[0-9]+(?:,[0-9]*)?|,[0-9]*)\}")
# Флаги: (?i) — на весь образец, (?i:…) и (?-i:…) — на группу.
FLAGS = re.compile(r"\(\?([aiLmsux]*)(?:-([aiLmsux]*))?([:)])")
VERBOSE_SPACE = " \t\n\r\v\f"


def _skip_to(text, i, stop):
    """Место сразу после знака stop, первого от места i; «\\» со следующим
    знаком — одна пара, как в re."""
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text[i] == stop:
            return i + 1
        else:
            i += 1
    return len(text)


def _set_end(text, i):
    """Место сразу после набора [...], начатого на месте i; «]» первым
    знаком набора — сам знак."""
    i += 2 if text.startswith("[^", i) else 1
    first = i
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text[i] == "]" and i > first:
            return i + 1
        else:
            i += 1
    return len(text)


def _not_everywhere(pattern):
    """Что в образце Python 3.8 и новее разбирают по-разному, или None.

    Атомарную группу (?>…) и повтор без возврата *+, ++, ?+, {m,n}+
    разбирают Python 3.11 и новее, 3.8–3.10 — нет. Флаги вида (?i) не в
    начале образца 3.8–3.10 разбирают с предупреждением, 3.11 и новее —
    нет. Условие (?(…)…) с группой не по имени и не цифрами 0–9 3.8–3.10
    разбирают, 3.12 и новее — нет. \\z разбирает только 3.14. Разбор
    повторяет re там, где это важно: пара «\\ и знак», набор [...],
    примечание (?#…), режим x с примечанием от «#» до конца строки."""
    verbose = [False]  # режим x: на весь образец и в каждой группе
    started = False  # прошло что-то кроме флагов на весь образец
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        flags = FLAGS.match(pattern, i)
        if verbose[-1] and ch in VERBOSE_SPACE:
            i += 1
            continue
        if verbose[-1] and ch == "#":
            i = _skip_to(pattern, i + 1, "\n")
            continue
        if pattern.startswith("(?#", i):
            i = _skip_to(pattern, i + 3, ")")
            continue
        if flags and flags.group(3) == ")":
            if started or len(verbose) > 1:
                return "флаги вида (?i) — только в самом начале образца"
            if "x" in flags.group(1):
                verbose[-1] = True
            i = flags.end()
            continue
        started = True
        if ch == "\\":
            if pattern.startswith("z", i + 1):
                return "\\z — только с Python 3.14; конец текста — \\Z"
            i += 2
        elif ch == "[":
            i = _set_end(pattern, i)
        elif pattern.startswith("(?>", i):
            return "атомарная группа (?>…) — только с Python 3.11"
        elif pattern.startswith("(?(", i):
            end = _skip_to(pattern, i + 3, ")")
            name = pattern[i + 3:end - 1]
            if not (name.isidentifier()
                    or (name.isascii() and name.isdigit())):
                return ("в условии (?(…)…) — имя группы или её номер "
                        "цифрами 0–9")
            verbose.append(verbose[-1])
            i = end
        elif pattern.startswith("(?P=", i):
            i = _skip_to(pattern, i + 4, ")")
        elif ch == "(":
            if flags:
                on, off = flags.group(1), flags.group(2) or ""
                verbose.append("x" in on or ("x" not in off and verbose[-1]))
                i = flags.end()
            else:
                verbose.append(verbose[-1])
                i += 2 if pattern.startswith("(?", i) else 1
        elif ch == ")":
            if len(verbose) > 1:
                verbose.pop()
            i += 1
        else:
            repeat = REPEAT.match(pattern, i) if ch == "{" else None
            if ch not in "*+?" and not repeat:
                i += 1
                continue
            i = repeat.end() if repeat else i + 1
            if pattern.startswith("+", i):
                return ("повтор без возврата *+, ++, ?+ или {m,n}+ — только "
                        "с Python 3.11")
            if pattern.startswith("?", i):
                i += 1
    return None


def _regex(value):
    problem = _not_everywhere(value)
    if problem:
        return NOT_EVERYWHERE_TEXT + problem
    with warnings.catch_warnings():
        # Python 3.8–3.10 о флагах не в начале образца лишь предупреждают,
        # 3.11 и новее его не разбирают: предупреждение — тоже нарушение.
        warnings.simplefilter("error", DeprecationWarning)
        try:
            re.compile(value, re.IGNORECASE)
        except DeprecationWarning as err:
            return NOT_EVERYWHERE_TEXT + str(err)
        except (re.error, OverflowError, RuntimeError) as err:
            # OverflowError — число повторов больше, чем знает re, на всех
            # версиях; RuntimeError — условие с группой, которой нет, на
            # Python 3.8.
            return "образец не разбирается как регулярное выражение: %s" % err
    return None


def _check_command(record):
    has_command = "команда" in record
    has_os = ("windows" in record, "linux" in record)
    if has_command and any(has_os):
        return ("«команда» и «windows»/«linux» вместе не ставят: либо одна "
                "команда, либо две")
    if not has_command and not all(has_os):
        return "нужно поле «команда» либо оба поля «windows» и «linux»"
    return None


SIGNATURE = Record([("имя", True, Text()), ("почта", True, Text())])

CHECK = Record([
    ("имя", True, Text(_check_name)),
    ("команда", False, Text()),
    ("windows", False, Text()),
    ("linux", False, Text()),
    ("успех", False, Record([
        ("код выхода", False, Integer()),
        ("строка в выводе", False, Text()),
    ])),
    ("не дольше", False, Number(_positive)),
], _check_command)

SCHEMA = Record([
    ("комплект", True, Record([
        ("источник", True, Text()),
        ("изменение", True, Text()),
    ])),
    ("площадка", True, Record([
        ("адрес", True, Text(_url)),
        ("группа", True, Text()),
        ("репозиторий", True, Text()),
    ])),
    ("основная ветка", True, Text()),
    ("ветки задач", True, List(Record([
        ("префикс", True, Text()),
        ("что", True, Text()),
    ]), nonempty=True)),
    ("учётные записи", True, Record([
        ("владелец", True, Text()),
        ("постановщик", True, Record([
            ("учётная запись", True, Text()),
            ("переменная ключа", True, Text(_env_name)),
        ])),
        ("авторы", True, List(Record([
            ("учётная запись", True, Text()),
            ("переменная ключа", True, Text(_env_name)),
            ("подпись", True, SIGNATURE),
        ]), nonempty=True)),
        ("проверяющий", True, Record([
            ("учётная запись", True, Text()),
            ("переменная ключа", True, Text(_env_name)),
            ("подпись", True, SIGNATURE),
        ])),
        ("люди", False, List(Record([
            ("учётная запись", True, Text()),
            ("роль", True, Text(_one_of("аналитик", "разработчик"))),
        ]))),
    ])),
    ("проверки", True, List(CHECK)),
    ("конвейер", False, Record([
        ("есть", True, Flag()),
        ("статусы", True, List(Text())),
    ])),
    ("опрос", True, Record([
        ("период", True, Number(_positive)),
        ("не дольше", True, Number(_not_negative)),
    ])),
    ("рабочие каталоги", True, Record([
        ("переменная базы", True, Text(_env_name)),
    ])),
    ("реестр отложенного", True, Text()),
    ("формат документов", False, Record([
        ("файлы", True, List(Text(), nonempty=True)),
        ("кодировка", True, Text(_one_of("utf-8 без BOM", "utf-8 с BOM"))),
        ("концы строк", True, Text(_one_of("LF", "CRLF"))),
        ("перевод строки в конце", True, Flag()),
        ("длина строки", True, Integer(_not_negative)),
        ("ссылки", True, Flag()),
        ("yaml", True, Flag()),
        ("слова", True, List(Record([
            ("слово", True, Text()),
            ("образец", True, Pattern(_regex)),
            ("вместо", True, Text()),
        ]))),
    ])),
])


class _Validator:
    def __init__(self):
        self.problems = []  # (строка, путь поля, что не так)

    def add(self, node, path, text):
        line = node.start_mark.line + 1 if node is not None else 1
        self.problems.append((line, " / ".join(path), text))

    def walk(self, spec, value, node, path):
        if isinstance(value, str):
            place = spec.place(value)
            if place:
                self.add(node, path, "осталось место «%s» — заполните"
                         % place)
                return
        if not spec.fits(value):
            self.add(node, path, "ожидается %s, а здесь %s"
                     % (spec.what, _what_is(value)))
            return
        if isinstance(spec, Text) and not value.strip():
            self.add(node, path, "пустая строка")
            return
        if isinstance(spec, Record):
            self.walk_record(spec, value, node, path)
        elif isinstance(spec, List):
            if spec.nonempty and not value:
                self.add(node, path, "пустой список")
            nodes = node.value if isinstance(node, yaml.SequenceNode) else []
            for i, item in enumerate(value):
                item_node = nodes[i] if i < len(nodes) else node
                self.walk(spec.item, item, item_node, path + [str(i + 1)])
        if spec.check is not None:
            problem = spec.check(value)
            if problem:
                self.add(node, path, problem)

    def walk_record(self, spec, value, node, path):
        known = dict((name, (required, sub))
                     for name, required, sub in spec.fields)
        nodes = {}
        if isinstance(node, yaml.MappingNode):
            for key_node, value_node in node.value:
                key = key_node.value
                if key in nodes:
                    self.add(key_node, path + [key], "поле повторяется")
                nodes[key] = (key_node, value_node)
        for key in value:
            if key not in known:
                key_node = nodes.get(str(key), (node, None))[0]
                self.add(key_node, path + [str(key)],
                         "неизвестное поле; здесь есть: %s"
                         % ", ".join(name for name, _, _ in spec.fields))
        for name, required, sub in spec.fields:
            if name not in value:
                if required:
                    self.add(node, path + [name], "не задано")
                continue
            self.walk(sub, value[name], nodes.get(name, (None, node))[1],
                      path + [name])


def validate(data, node):
    """Нарушения настройки: [(строка, путь поля, что не так)]."""
    checker = _Validator()
    if not isinstance(data, dict):
        checker.add(node, ["(весь файл)"], "ожидается запись с полями, а "
                    "здесь %s" % _what_is(data))
        return checker.problems
    checker.walk(SCHEMA, data, node, [])
    if not checker.problems:
        _cross_checks(data, node, checker)
    return sorted(checker.problems, key=lambda p: p[0])


def _cross_checks(data, node, checker):
    """Сверки между полями; только для настройки без других нарушений."""
    site = data["площадка"]
    if not site["репозиторий"].startswith(site["группа"] + "/"):
        checker.add(node, ["площадка", "репозиторий"],
                    "ожидается «<группа>/<репозиторий>» с группой «%s» из "
                    "поля «группа»" % site["группа"])
    seen = {}
    for i, check in enumerate(data["проверки"], start=1):
        name = check["имя"]
        if name in seen:
            checker.add(node, ["проверки", str(i), "имя"],
                        "имя «%s» уже есть у проверки %d" % (name, seen[name]))
        seen.setdefault(name, i)


def load_config(path=None):
    """Настройка, сверенная по README.md, раздел о настройке; нарушение —
    ToolError с полем и строкой."""
    path = path or DEFAULT_CONFIG
    if yaml is None:
        raise ToolError(NO_YAML_TEXT)
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except FileNotFoundError:
        raise ToolError("нет настройки %s; другой файл задаёт ключ --config"
                        % shown(path))
    except (OSError, UnicodeDecodeError) as err:
        raise ToolError("настройка %s не читается: %s" % (shown(path), err))
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
        data = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError) as err:
        # Дата вне календаря — ValueError: datetime строится при загрузке, и
        # PyYAML её не оборачивает.
        mark = getattr(err, "problem_mark", None)
        line = mark.line + 1 if mark else 1
        problem = getattr(err, "problem", None) or str(err).splitlines()[0]
        raise ToolError("%s:%d: настройка не разбирается: %s"
                        % (shown(path), line, problem),
                        "настройка не сверилась: нарушений 1")
    problems = validate(data, node)
    if problems:
        lines = ["%s:%d: поле «%s»: %s" % (shown(path), line, field, text)
                 for line, field, text in problems]
        lines.append("настройка не сверилась: нарушений %d" % len(problems))
        raise ToolError(*lines)
    return data


ROLE_NAMES = {
    "planner": "постановщик",
    "author": "автор",
    "reviewer": "проверяющий",
    "part-reviewer": "проверяющий частей",
    "owner": "владелец",
}


def role_accounts(cfg, role):
    """Учётные записи роли по настройке. Проверяющий частей — агент
    разработчика, а агент человека работает под учётной записью автора
    (AGENTS.md, 1.5, 3.7)."""
    accounts = cfg["учётные записи"]
    if role == "planner":
        return [accounts["постановщик"]["учётная запись"]]
    if role in ("author", "part-reviewer"):
        return [a["учётная запись"] for a in accounts["авторы"]]
    if role == "reviewer":
        return [accounts["проверяющий"]["учётная запись"]]
    if role == "owner":
        return [accounts["владелец"]]
    raise ValueError(role)


def key_variables(cfg):
    """Имена переменных окружения с ключами ролей из настройки."""
    accounts = cfg["учётные записи"]
    names = [accounts["постановщик"]["переменная ключа"],
             accounts["проверяющий"]["переменная ключа"]]
    names += [a["переменная ключа"] for a in accounts["авторы"]]
    return names


# --- Ключ и площадка -------------------------------------------------------

NO_TOKEN_TEXT = ("нет ключа: подайте его переменной окружения GITLAB_TOKEN "
                 "на одну команду (AGENTS.md, 6.3)")
BAD_TOKEN_TEXT = ("в GITLAB_TOKEN не ключ: ключ — только печатные знаки "
                  "ASCII без пробелов; проверьте, что в переменной сам ключ")
BOM_TOKEN_TEXT = ("в GITLAB_TOKEN не ключ: в начале — метка порядка байтов "
                  "(BOM), её оставляет файл ключа в UTF-8 с BOM; запишите "
                  "файл ключа без BOM")
BOM_CHAR = chr(0xFEFF)


def token_from_env():
    """Ключ из GITLAB_TOKEN; пробелы, перевод строки и возврат каретки по
    краям отбрасываются (файл ключа с концами строк CRLF)."""
    value = os.environ.get("GITLAB_TOKEN")
    return value.strip(" \t\r\n") if value is not None else None


def token_valid(token):
    """Ключ — только печатные знаки ASCII без пробелов; метка порядка
    байтов в начале краем не считается: с ней ключ не годен и для glab и
    git, и файл ключа поправляют, а не обходят."""
    return bool(token) and all(0x21 <= ord(ch) <= 0x7E for ch in token)


def bad_token_text(token):
    """Причина отказа негодному ключу: метка порядка байтов названа
    отдельно. Ни ключ, ни его знаки в причину не попадают."""
    if token and token.startswith(BOM_CHAR):
        return BOM_TOKEN_TEXT
    return BAD_TOKEN_TEXT


def require_token():
    """Ключ из GITLAB_TOKEN или отказ; ни ключ, ни его знаки в сообщение не
    попадают."""
    token = token_from_env()
    if not token:
        raise ToolError(NO_TOKEN_TEXT)
    if not token_valid(token):
        raise ToolError(bad_token_text(token))
    return token


def platform_host(cfg, explicit=None):
    """Адрес площадки: ключ --host либо поле «площадка», «адрес»."""
    host = (explicit if explicit else cfg["площадка"]["адрес"]).strip()
    parts, problem = _split_url(host)
    if problem:
        raise ToolError("площадка: %s" % problem)
    if parts.username or parts.password:
        raise ToolError("в адресе площадки — учётные данные; ключ подают "
                        "только переменной GITLAB_TOKEN (AGENTS.md, 6.3)")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ToolError("площадка «%s»: адрес — целиком, с http:// или "
                        "https://" % host)
    return host.rstrip("/")


def _no_proxy_entry(entry):
    """Узел и порт записи NO_PROXY: «узел:порт», «[адрес IPv6]:порт» или
    узел без порта — тогда порт None. Порт не числом — узел None: такая
    запись, как и у glab, не подходит ни одному узлу."""
    if entry.startswith("["):
        host, _, rest = entry[1:].partition("]")
        if not rest:
            return host, None
        if not rest.startswith(":"):
            return None, None
        port = rest[1:]
    elif entry.count(":") == 1:
        host, _, port = entry.partition(":")
    else:
        return entry, None
    if not port:
        return host, None
    if not re.match(r"^[0-9]+$", port):
        return None, None
    return host, int(port)


def no_proxy_for(hostname, port):
    """Обойти прокси для площадки: её узел hostname с портом port назван в
    NO_PROXY. Записи — как у glab: «*» — любой узел; имя — сам узел и
    имена, которые им оканчиваются, в том числе с точкой или «*.» в
    начале; адрес; сеть вида 10.0.0.0/8. У записи, кроме сети, может
    стоять порт — «узел:порт», у адреса IPv6 «[адрес]:порт»: такая запись
    действует только для этого порта. Сеть, «*» среди других записей и
    запись с портом при адресе площадки без порта urllib сам не понимает,
    а glab понимает; без этой сверки ключ шёл бы к площадке через
    прокси."""
    value = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    host = hostname.lower().strip("[]")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    for entry in value.split(","):
        entry = entry.strip().lower()
        if not entry:
            continue
        if entry == "*":
            return True
        if "/" in entry:
            try:
                if address is not None and address in ipaddress.ip_network(
                        entry, strict=False):
                    return True
            except ValueError:
                pass
            continue
        name, entry_port = _no_proxy_entry(entry)
        if name is None or entry_port not in (None, port):
            continue
        if name.startswith("*."):
            name = name[1:]
        name = name.lstrip(".")
        if name and (host == name or host.endswith("." + name)):
            return True
    return False


class ApiError(Exception):
    """Площадка ответила ошибкой или не ответила; code — код ответа HTTP,
    None — ответа не было; answer — текст ответа площадки, если он был."""

    def __init__(self, message, code=None, answer=None):
        Exception.__init__(self, message)
        self.code = code
        self.answer = answer

    @property
    def transient(self):
        """Сбой площадки, который может пройти сам: нет ответа, ошибка
        сервера, «слишком много запросов». Остальное — отказ."""
        return self.code is None or self.code >= 500 or self.code == 429


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Переадресацию площадки не выполнять: urllib перенёс бы заголовок с
    ключом на новый адрес, в том числе на другой узел. Ответ 3xx тогда
    приходит как ошибка с этим кодом."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def tls_context():
    """Контекст TLS для площадки по https: проверка сертификата и имени
    узла — как у ssl.create_default_context этой версии Python, протокол
    ALPN и post_handshake_auth — как у контекста, который http.client этой
    версии строит сам, но без файла ключей TLS. create_default_context
    пишет ключи шифрования в файл из переменной SSLKEYLOGFILE, а по ним
    расшифровывается запрос вместе с ключом площадки; с Python 3.12 urllib
    строит такой контекст уже при сборке клиента, и файл появляется даже у
    площадки по http."""
    # PROTOCOL_TLS_CLIENT сам требует сертификат и сверяет имя узла.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if sys.version_info >= (3, 13):
        # С Python 3.13 create_default_context сверяет цепочку строже.
        context.verify_flags |= (ssl.VERIFY_X509_PARTIAL_CHAIN
                                 | ssl.VERIFY_X509_STRICT)
    context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    # Так http.client настраивает контекст, который строит сам: ALPN
    # «http/1.1» он предлагает с Python 3.10, раньше — никакого.
    if sys.version_info >= (3, 10):
        context.set_alpn_protocols(["http/1.1"])
    if context.post_handshake_auth is not None:
        context.post_handshake_auth = True
    return context


PAGE_SIZE = 100


class Client:
    """Запросы к API v4 площадки без glab. Ответ — разобранный JSON. GET —
    у всех средств; POST и PUT — только у владельческих."""

    def __init__(self, host, token, timeout=60):
        if not token_valid(token):
            raise ToolError(bad_token_text(token))
        self.host = host.rstrip("/")
        self.base = self.host + "/api/v4/"
        self._token = token
        self.timeout = timeout
        parts = urllib.parse.urlsplit(self.host)
        hostname = parts.hostname or ""
        try:
            port = parts.port
        except ValueError:
            port = None
        if port is None:
            port = 443 if parts.scheme == "https" else 80
        # Прокси — только из переменных окружения, на любой системе, как у
        # glab и git. Без своего обработчика build_opener добавил бы
        # обработчик по умолчанию, а тот при пустых переменных берёт
        # системный прокси — в Windows из реестра, настроек Интернета, — и
        # ключ ушёл бы через него. Обход внутри обработчика (proxy_bypass)
        # при заданных переменных сверяет только их, а при пустых обработчик
        # прокси не знает и обход не зовёт.
        proxies = ({} if no_proxy_for(hostname, port)
                   else urllib.request.getproxies_environment())
        # Свой обработчик https и для площадки по http: иначе build_opener
        # добавит свой, а с Python 3.12 тот при сборке читает SSLKEYLOGFILE.
        handlers = [_NoRedirect(),
                    urllib.request.HTTPSHandler(context=tls_context()),
                    urllib.request.ProxyHandler(proxies)]
        self._opener = urllib.request.build_opener(*handlers)

    def _send(self, method, path, params=None, data=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"PRIVATE-TOKEN": self._token}
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers,
                                         method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as resp:
                raw = resp.read()
                next_page = resp.headers.get("X-Next-Page")
        except urllib.error.HTTPError as err:
            try:
                if 300 <= err.code < 400:
                    raise ApiError(
                        "%s %s: площадка ответила переадресацией %d на %s; "
                        "по ней средство не идёт, чтобы ключ не ушёл на "
                        "другой адрес, — проверьте адрес площадки"
                        % (method, path, err.code,
                           err.headers.get("Location")), err.code)
                text = _error_text(err)
                raise ApiError("%s %s: %d %s" % (method, path, err.code,
                                                 text), err.code, text)
            finally:
                # Ответ-ошибка держит соединение с площадкой: незакрытым
                # оно ждало бы уборки мусора.
                err.close()
        except urllib.error.URLError as err:
            raise ApiError("%s %s: площадка не отвечает (%s)"
                           % (method, path, err.reason))
        except (OSError, http.client.HTTPException) as err:
            raise ApiError("%s %s: площадка не отвечает (%s)"
                           % (method, path, err.__class__.__name__))
        try:
            data = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            raise ApiError("%s %s: ответ площадки не разбирается"
                           % (method, path))
        return data, None if next_page is None else next_page.strip()

    def _get(self, path, params=None):
        return self._send("GET", path, params)

    def get(self, path, params=None):
        return self._get(path, params)[0]

    def post(self, path, data):
        return self._send("POST", path, data=data)[0]

    def put(self, path, data):
        return self._send("PUT", path, data=data)[0]

    def pages(self, path, params=None):
        """Страницы списка по очереди, по PAGE_SIZE записей."""
        params = dict(params or {})
        params["per_page"] = PAGE_SIZE
        page = 1
        while page:
            params["page"] = page
            chunk, header = self._get(path, params)
            page = next_page_number(path, page, chunk, header)
            yield chunk or []

    def get_all(self, path, params=None):
        items = []
        for chunk in self.pages(path, params):
            items.extend(chunk)
        return items


def next_page_number(path, page, chunk, header):
    """Номер страницы после page по заголовку X-Next-Page; None — конец
    выдачи. Заголовок пуст — конец, в том числе при полной странице: так
    GitLab отвечает на ровно полную последнюю. Сбой площадки, а не конец и не
    повтор: заголовка нет вовсе при полной странице — прокси срезал его, и
    выдача молча оборвалась бы; номер не больше текущего или не номер — обход
    не кончился бы, и срок опроса его не прервал бы."""
    if header is None:
        if isinstance(chunk, list) and len(chunk) >= PAGE_SIZE:
            raise ApiError("GET %s: страница %d полна, а заголовка X-Next-Page "
                           "нет: выдача была бы неполной" % (path, page))
        return None
    if not header:
        return None
    if not re.fullmatch(r"[0-9]+", header) or int(header) <= page:
        raise ApiError("GET %s: заголовок X-Next-Page «%s» на странице %d — "
                       "нужен номер больше неё" % (path, header, page))
    return int(header)


def _error_text(err):
    """Текст ответа-ошибки площадки. Тело, оборванное при чтении, —
    OSError или HTTPException вроде IncompleteRead — не трассировка: тогда
    текст — причина из строки ответа."""
    try:
        body = err.read().decode("utf-8", "replace")
    except (OSError, http.client.HTTPException):
        body = ""
    try:
        payload = json.loads(body)
    except ValueError:
        return str(err.reason or "")
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("error") or payload)
    return str(payload)


def project_path(cfg):
    """Проект в адресе API: «группа/имя» с закодированной косой чертой."""
    return "projects/" + urllib.parse.quote(cfg["площадка"]["репозиторий"],
                                            safe="")


def check_account(client, cfg, role):
    """Учётная запись ключа; не та, что у роли в настройке, — отказ
    (AGENTS.md, 6.6)."""
    try:
        user = client.get("user")
    except ApiError as err:
        if err.code == 401:
            raise ToolError("площадка не приняла ключ: %s" % err)
        raise ToolError("учётная запись ключа не узнана: %s" % err)
    username = (user or {}).get("username")
    allowed = role_accounts(cfg, role)
    if username not in allowed:
        raise ToolError(
            "ключ — учётной записи %s, а у роли «%s» в настройке — %s; "
            "чужим ключом не работают (AGENTS.md, 6.6)"
            % (username, ROLE_NAMES[role], ", ".join(allowed)))
    return username


# --- Нарушения и разобранные исключения ------------------------------------

class Violation:
    def __init__(self, path, line, kind, text):
        self.path = path
        self.line = line
        self.kind = kind
        self.text = text

    def __str__(self):
        return "%s:%d: %s: %s" % (self.path, self.line, self.kind, self.text)


def normalize(text):
    return re.sub(r"\s+", " ", text).strip().lower().replace("ё", "е")


def paragraphs(lines):
    """Абзацы: (первая строка, последняя строка, текст), строки с 1."""
    result = []
    start = None
    for i, line in enumerate(list(lines) + [""], start=1):
        if line.strip():
            if start is None:
                start = i
        elif start is not None:
            result.append((start, i - 1,
                           normalize(" ".join(lines[start - 1:i - 1]))))
            start = None
    return result


EXCEPTION_TOOLS = ("check_docs.py", "check_merge_log.py")
EXCEPTION_FIELDS = ("средство", "место", "вид", "абзац", "причина")


class Exemption:
    def __init__(self, line, place, kind, paragraph, reason):
        self.line = line
        self.place = place
        self.kind = kind
        self.paragraph = paragraph
        self.reason = reason
        self.used = False

    def covers(self, violation, file_paragraphs):
        if violation.path != self.place or violation.kind != self.kind:
            return False
        if self.paragraph is None:
            return True
        return any(first <= violation.line <= last and self.paragraph in text
                   for first, last, text in file_paragraphs)


def load_exceptions(path, tool, kinds, rel):
    """Разобранные исключения средства tool и нарушения файла исключений.
    Нет файла — нет исключений. rel — путь файла для вывода."""
    problems = []

    def problem(line, text):
        problems.append(Violation(rel, line, "исключение", text))

    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except FileNotFoundError:
        return [], []
    except (OSError, UnicodeDecodeError) as err:
        problem(1, "файл не читается: %s" % err)
        return [], problems
    try:
        root_node = yaml.compose(text, Loader=yaml.SafeLoader)
        data = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError) as err:
        mark = getattr(err, "problem_mark", None)
        problem(mark.line + 1 if mark else 1, "файл не разбирается: %s"
                % (getattr(err, "problem", None) or err))
        return [], problems
    if not isinstance(data, dict) or not isinstance(
            data.get("исключения"), list):
        problem(1, "нет списка «исключения»; исключений нет — "
                   "«исключения: []»")
        return [], problems
    items = []
    for key_node, value_node in root_node.value:
        if key_node.value != "исключения":
            # Строка — у узла ключа: под записями поле искали бы не там.
            problem(key_node.start_mark.line + 1,
                    "неизвестное поле «%s»" % key_node.value)
        elif isinstance(value_node, yaml.SequenceNode):
            items = value_node.value
    result = []
    for i, entry in enumerate(data["исключения"]):
        line = items[i].start_mark.line + 1 if i < len(items) else 1
        if not isinstance(entry, dict):
            problem(line, "запись не разбирается: ожидается запись с полями")
            continue
        bad = False
        for key in entry:
            if key not in EXCEPTION_FIELDS:
                problem(line, "неизвестное поле «%s»; есть: %s"
                        % (key, ", ".join(EXCEPTION_FIELDS)))
                bad = True
        values = dict((k, entry.get(k)) for k in EXCEPTION_FIELDS)
        for key in ("средство", "место", "вид"):
            if not isinstance(values[key], str) or not values[key].strip():
                problem(line, "нет поля «%s»" % key)
                bad = True
        reason = values["причина"]
        if not isinstance(reason, str) or not reason.strip():
            problem(line, "нет причины")
            bad = True
        paragraph = values["абзац"]
        if paragraph is not None and (not isinstance(paragraph, str)
                                      or not paragraph.strip()):
            problem(line, "«абзац» — строка со словами абзаца")
            bad = True
        tool_name = values["средство"]
        if isinstance(tool_name, str) and tool_name.strip():
            tool_name = tool_name.strip()
            if tool_name not in EXCEPTION_TOOLS:
                problem(line, "неизвестное средство «%s»; есть: %s"
                        % (tool_name, ", ".join(EXCEPTION_TOOLS)))
                bad = True
            elif tool_name == tool and isinstance(values["вид"], str) and \
                    values["вид"].strip() not in kinds:
                problem(line, "неизвестный вид «%s»; у %s есть: %s"
                        % (values["вид"].strip(), tool, ", ".join(kinds)))
                bad = True
        if bad or tool_name != tool:
            continue
        result.append(Exemption(
            line, values["место"].strip(), values["вид"].strip(),
            normalize(paragraph) if paragraph else None, reason.strip()))
    return result, problems


def apply_exceptions(violations, exemptions, kinds, file_lines):
    """Нарушения, которые не покрыты исключениями; покрывшие исключения
    помечаются. file_lines — строки файлов для абзацев."""
    kept = []
    cache = {}
    for v in violations:
        covered = False
        if v.kind in kinds:
            if v.path not in cache:
                cache[v.path] = paragraphs(file_lines.get(v.path, []))
            for exemption in exemptions:
                if exemption.covers(v, cache[v.path]):
                    exemption.used = True
                    covered = True
                    break
        if not covered:
            kept.append(v)
    return kept


def unused_exceptions(exemptions, rel):
    """Нарушения: исключения, которые при полной проверке ничего не
    покрыли."""
    return [Violation(rel, e.line, "исключение",
                      "ничего не покрыло — уберите или поправьте запись")
            for e in exemptions if not e.used]


def matches(rel, masks):
    """Путь от корня совпал с одной из масок, с учётом регистра букв
    (fnmatchcase). «*» совпадает и с косой чертой, как в pathspec git
    (git ls-files '*.md'), в отличие от .gitignore: «docs/*.md» берёт и
    docs/a/b.md. «**» — то же, что «*», а «?» и «[!x]» берут и косую
    черту, как у fnmatch."""
    return any(fnmatch.fnmatchcase(rel, mask) for mask in masks)
