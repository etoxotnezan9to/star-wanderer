"""Проверка репозитория: проверки из настройки, поле «проверки» (AGENTS.md,
2.6, 4.7).

Запуск из корня клона:
    python .branch-kit/tools/check_repo.py [<имя проверки> …] [--config-only]
По строке на проверку: имя, исход, код выхода, длительность, путь полного
вывода; у непрошедшей — последние 20 строк вывода. Последняя строка —
итог с вершиной и чистотой рабочей копии. Код выхода: 0 — все проверки
прошли, 1 — хоть одна не прошла или прервана по сроку либо вершина сменилась
за время проверок, 2 — проверки не выполнены. Подробно — README.md рядом.
"""

import locale
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time

# Байт-код общей части не пишется: каталог __pycache__ в клоне сделал
# бы рабочую копию несохранённой.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kit_common as kit  # noqa: E402

TAIL = 20
WINDOWS = os.name == "nt"
OS_FIELD = "windows" if WINDOWS else "linux"
PYTHON_WORD = re.compile(r"^\s*python(?=\s|$)")


def same_python(command):
    """Команда, начинающаяся словом python, — тем же Python, что и это
    средство."""
    m = PYTHON_WORD.match(command)
    if not m or not sys.executable:
        return command
    if WINDOWS:
        exe = subprocess.list2cmdline([sys.executable])
    else:
        exe = shlex.quote(sys.executable)
    return exe + command[m.end():]


def check_env(cfg):
    """Окружение команд: окружение запуска без GITLAB_TOKEN и без
    переменных ключей из настройки — проверкам ключ не нужен."""
    drop = set(["GITLAB_TOKEN"] + kit.key_variables(cfg))
    if WINDOWS:
        drop = set(name.upper() for name in drop)
        return dict((k, v) for k, v in os.environ.items()
                    if k.upper() not in drop)
    return dict((k, v) for k, v in os.environ.items() if k not in drop)


def start(command, root, env, log):
    """Команда оболочкой своей системы: в Linux — sh, в Windows — cmd; в
    своей группе процессов, чтобы прервать её вместе с дочерними."""
    options = dict(cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                   stdin=subprocess.DEVNULL, shell=True)
    if WINDOWS:
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    try:
        return subprocess.Popen(command, **options)
    except OSError as err:
        raise kit.ToolError("команда не запустилась: %s" % err)


def kill_tree(proc):
    """Прервать команду вместе с дочерними процессами: в Windows
    proc.kill() дочерние процессы cmd не прерывает."""
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def legacy_encodings():
    """Кодировки вывода не в UTF-8, по порядку. В Windows cmd и консольные
    программы пишут в кодировке OEM (cp866), а Python и программы Windows
    в файл — в ANSI (cp1251); в Linux — кодировка системы."""
    ansi = locale.getpreferredencoding(False)
    return ["oem", ansi] if WINDOWS else [ansi]


def _letters(text):
    """Сколько букв и сколько строчных: в чужой кодировке буквы текста
    становятся знаками рамок и препинания."""
    return (sum(1 for ch in text if ch.isalpha()),
            sum(1 for ch in text if ch.islower()))


def decode_line(raw):
    """Строка вывода: UTF-8; иначе — из кодировок legacy_encodings та, что
    даёт больше букв, при равенстве — первая. Брать первую, которая
    прочла строку, нельзя: cp866 читает любой байт, и ANSI не выбиралась
    бы никогда."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    best = None
    for encoding in legacy_encodings():
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if best is None or _letters(text) > _letters(best):
            best = text
    if best is None:
        best = raw.decode("utf-8", "replace")
    return best


def decode(raw):
    """Вывод команды как текст, по строкам: в одном выводе бывают строки
    cmd в OEM и строки Python в ANSI."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return "\n".join(decode_line(line) for line in raw.split(b"\n"))


class Result:
    def __init__(self, name, passed, code, seconds, reason, log_path, tail):
        self.name = name
        self.passed = passed
        self.code = code
        self.seconds = seconds
        self.reason = reason
        self.log_path = log_path
        self.tail = tail


def wait_seconds(check):
    """Срок проверки в секундах или None. Сверка — до запуска команды: срок
    .inf проходит сверку настройки, а срок больше предела системы ожидание
    не примет, и команда осталась бы работать."""
    minutes = check.get("не дольше")
    if not minutes:
        return None
    seconds = minutes * 60
    if not seconds <= threading.TIMEOUT_MAX:
        raise kit.ToolError(
            "проверка %s не выполнена: срок «не дольше» %s мин больше, чем "
            "умеет ждать система" % (check["имя"], _number(minutes)))
    return seconds


def run_check(check, root, env, log_dir):
    name = check["имя"]
    command = same_python(check.get("команда") or check[OS_FIELD])
    minutes = check.get("не дольше")
    seconds_limit = wait_seconds(check)
    success = check.get("успех") or {}
    expected = success.get("код выхода", 0)
    wanted = success.get("строка в выводе")
    log_path = os.path.join(log_dir, name + ".log")
    began = time.monotonic()
    timed_out = False
    try:
        os.makedirs(log_dir, exist_ok=True)
        log = open(log_path, "wb")
    except OSError as err:
        # В Windows файл может держать дочерний процесс прошлого запуска.
        raise kit.ToolError("проверка %s не выполнена: файл вывода %s не "
                            "открылся (%s)"
                            % (name, kit.shown(log_path), err))
    with log:
        proc = start(command, root, env, log)
        try:
            proc.wait(timeout=seconds_limit)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            timed_out = True
        except KeyboardInterrupt:
            kill_tree(proc)
            raise
    seconds = time.monotonic() - began
    try:
        with open(log_path, "rb") as f:
            lines = decode(f.read()).splitlines()
    except OSError as err:
        raise kit.ToolError("проверка %s: файл вывода %s не прочитан (%s)"
                            % (name, kit.shown(log_path), err))
    reason = None
    if timed_out:
        reason = "прервана по сроку %s мин" % _number(minutes)
    elif proc.returncode != expected:
        reason = "нужен код выхода %d" % expected
    elif wanted is not None and wanted.strip() not in (
            line.strip() for line in lines):
        reason = "нет строки «%s» в выводе (строка целиком)" % wanted.strip()
    return Result(name, reason is None,
                  None if timed_out else proc.returncode, seconds, reason,
                  log_path, lines[-TAIL:])


def _number(value):
    return ("%g" % value).replace(".", ",")


def print_result(result, root):
    code = "нет (прервана)" if result.code is None else str(result.code)
    outcome = "прошла" if result.passed else \
        "НЕ ПРОШЛА — %s" % result.reason
    print("%s: %s; код выхода %s; %s с; вывод — %s"
          % (result.name, outcome, code, _number(round(result.seconds, 1)),
             kit.relative(result.log_path, root) or result.log_path),
          flush=True)
    if not result.passed:
        print("  последние строки вывода (не больше %d):" % TAIL)
        for line in result.tail:
            print("  | " + line)
        sys.stdout.flush()


def dirty(root):
    """Несохранённое в рабочей копии: (изменённые отслеживаемые, новые) —
    множества путей; новые — файлы, которых git не отслеживает и
    .gitignore не исключает, каждый файл отдельно, а не каталог целиком."""
    code, out, err = kit.git(root, "status", "--porcelain", "-z",
                             "--untracked-files=all")
    if code != 0:
        raise kit.ToolError("git status не выполнен: %s" % err.strip())
    changed, new = set(), set()
    entries = iter(out.split("\0"))
    for entry in entries:
        if not entry:
            continue
        if entry.startswith("??"):
            new.add(entry[3:])
        else:
            changed.add(entry[3:])
            if "R" in entry[:2] or "C" in entry[:2]:
                next(entries, None)  # прежний путь переименованного
    return changed, new


def dirty_text(changed, new):
    if not changed and not new:
        return "рабочая копия чистая"
    return ("В РАБОЧЕЙ КОПИИ ЕСТЬ НЕСОХРАНЁННЫЕ ИЗМЕНЕНИЯ: изменённых "
            "отслеживаемых файлов %d, новых файлов %d"
            % (len(changed), len(new)))


def _short(sha):
    return sha[:8] if sha else "— изменений нет"


def vertex_text(before, after):
    """Вершина в итоге. Итог сверяют со сводкой (AGENTS.md, 2.6): проверка,
    которая сохранила изменение или переключила ветку, относит его не к той
    вершине, поэтому называются обе."""
    if before == after:
        return "вершина %s" % _short(before)
    return ("ВЕРШИНА СМЕНИЛАСЬ ЗА ВРЕМЯ ПРОВЕРОК, ИТОГ НЕ ПРОШЁЛ: до %s, после "
            "%s" % (_short(before), _short(after)))


def select(checks, names):
    if not names:
        return list(checks)
    known = [c["имя"] for c in checks]
    unknown = [n for n in names if n not in known]
    if unknown:
        raise kit.ToolError("нет проверки %s; в настройке есть: %s"
                            % (", ".join("«%s»" % n for n in unknown),
                               ", ".join(known)))
    return [c for c in checks if c["имя"] in names]


def main(argv=None):
    kit.setup_output()
    parser = kit.make_parser(
        "check_repo.py",
        "Проверка репозитория: проверки из настройки, поле «проверки» "
        "(AGENTS.md, 4.7).")
    parser.add_argument("names", nargs="*", metavar="<имя проверки>",
                        help="выполнить только эти проверки; без имён — все "
                             "по порядку")
    parser.add_argument("--config-only", action="store_true",
                        help="только сверить настройку")
    kit.add_config_argument(parser)
    args = parser.parse_args(argv)
    try:
        cfg = kit.load_config(args.config)
        if args.config_only:
            print("настройка сверена: %s; нарушений 0"
                  % kit.shown(args.config or kit.DEFAULT_CONFIG))
            return kit.OK
        root = kit.clone_root()
        checks = cfg["проверки"]
        if not checks:
            raise kit.ToolError("проверок нет: поле «проверки» настройки "
                                "пусто")
        selected = select(checks, args.names)
        log_dir = os.path.join(kit.git_dir(root), "branch-kit", "check_repo")
        vertex = kit.head(root)
        changed, new = dirty(root)
        env = check_env(cfg)
        results = []
        for check in selected:
            result = run_check(check, root, env, log_dir)
            results.append(result)
            print_result(result, root)
        # До проверок или после них: и то, что оставила сама проверка.
        changed_after, new_after = dirty(root)
        changed |= changed_after
        new |= new_after
        vertex_after = kit.head(root)
        passed = sum(1 for r in results if r.passed)
        if len(selected) == len(checks):
            count = "проверок %d" % len(checks)
        else:
            count = "проверок %d из %d, выполнены не все" % (len(selected),
                                                            len(checks))
        print("%s: прошли %d, не прошли %d; %s; %s"
              % (count, passed, len(results) - passed,
                 vertex_text(vertex, vertex_after), dirty_text(changed, new)))
        if passed == len(results) and vertex == vertex_after:
            return kit.OK
        return kit.FOUND
    except kit.ToolError as err:
        return kit.report(err)
    except KeyboardInterrupt:
        print("прервано: проверки не закончены", file=sys.stderr)
        return kit.INTERRUPTED
    except Exception as err:
        return kit.report_crash(err)


if __name__ == "__main__":
    sys.exit(main())
