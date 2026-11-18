"""Проверка формата документов по настройке (AGENTS.md, 5.2, 5.4).

Запуск из корня клона:
    python .branch-kit/tools/check_docs.py [<файл или каталог> …]
Без параметров проверяются файлы, которые отслеживает git и которые
совпали с масками настройки, поле «формат документов», «файлы»; маски —
как pathspec git, «*» берёт и косую черту (kit_common.matches). Вывод —
строка на нарушение «<файл>:<строка>: <вид>: <что не так>», затем, если такие
файлы есть, строка о файлах без проверки длины и ссылок, последняя —
«нарушений N». Код выхода: 0 — нарушений нет, 1 — есть, 2 — проверка не
выполнена, в том числе когда нет ни одного файла для проверки; 130 —
прервана Ctrl+C. Подробно — README.md рядом.
"""

import os
import posixpath
import re
import sys
import urllib.parse

# Байт-код общей части не пишется: каталог __pycache__ в клоне сделал
# бы рабочую копию несохранённой.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kit_common as kit  # noqa: E402
from kit_common import Violation  # noqa: E402

TOOL = "check_docs.py"
KINDS = ("кодировка", "концы строк", "конец файла", "длина строки", "слово",
         "ссылка", "yaml", "блок кода")
MD_EXT = (".md", ".markdown")
YAML_EXT = (".yaml", ".yml")
BOM = b"\xef\xbb\xbf"


def is_markdown(rel):
    return rel.lower().endswith(MD_EXT)


def is_yaml(rel):
    return rel.lower().endswith(YAML_EXT)


# --- Проверки файла --------------------------------------------------------

def check_file(root, rel, fmt, words, known):
    """Нарушения файла и его строки (для абзацев исключений); known — пути,
    на которые может вести ссылка (link_paths)."""
    with open(os.path.join(root, rel), "rb") as f:
        raw = f.read()
    found = []
    want_bom = fmt["кодировка"] == "utf-8 с BOM"
    has_bom = raw.startswith(BOM)
    if has_bom and not want_bom:
        found.append(Violation(rel, 1, "кодировка",
                               "в начале файла BOM — нужен UTF-8 без BOM"))
    elif want_bom and not has_bom:
        found.append(Violation(rel, 1, "кодировка",
                               "в начале файла нет BOM — нужен UTF-8 с BOM"))
    if has_bom:
        raw = raw[len(BOM):]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as err:
        line = raw[:err.start].count(b"\n") + 1
        found.append(Violation(rel, line, "кодировка",
                               "не UTF-8: байт 0x%02X" % raw[err.start]))
        return found, []
    found.extend(check_line_ends(rel, text, fmt["концы строк"]))
    if fmt["перевод строки в конце"] and text and not text.endswith("\n"):
        found.append(Violation(rel, text.count("\n") + 1, "конец файла",
                               "нет перевода строки в конце файла"))
    text = text.replace("\r\n", "\n")
    lines = text.split("\n")
    if text.endswith("\n"):
        lines = lines[:-1]
    found.extend(check_words(rel, text, words))
    if is_markdown(rel):
        found.extend(check_markdown(root, rel, lines, fmt, known))
    elif is_yaml(rel) and fmt["yaml"]:
        found.extend(check_yaml_text(rel, text, 1))
    return found, lines


def check_line_ends(rel, text, mode):
    found = []
    pieces = text.split("\n")
    if mode == "LF":
        crlf = [i for i, piece in enumerate(pieces[:-1], start=1)
                if piece.endswith("\r")]
        if crlf:
            found.append(Violation(
                rel, crlf[0], "концы строк",
                "CRLF в строках: %d, первая — эта; нужны LF" % len(crlf)))
    else:
        lf = [i for i, piece in enumerate(pieces[:-1], start=1)
              if not piece.endswith("\r")]
        if lf:
            found.append(Violation(
                rel, lf[0], "концы строк",
                "LF без CR в строках: %d, первая — эта; нужны CRLF"
                % len(lf)))
    lone = re.search(r"\r(?!\n)", text)
    if lone:
        found.append(Violation(rel, text.count("\n", 0, lone.start()) + 1,
                               "концы строк",
                               "одиночный CR; нужны %s" % mode))
    return found


def check_words(rel, text, words):
    found = []
    for word, regex, instead in words:
        for m in regex.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            found.append(Violation(
                rel, line, "слово",
                "«%s» не употребляется (слово «%s»); вместо — «%s»"
                % (" ".join(m.group(0).split()), word, instead)))
    return found


# Забор блока кода, как в CommonMark: в сведениях забора из обратных кавычек
# их быть не может, поэтому «```x``` — пример» — обычный текст.
FENCE = re.compile(r"^\s*(`{3,}(?=[^`]*$)|~{3,})\s*(\S*)")
INLINE_CODE = re.compile(r"`+[^`\n]*`+")
# Что разбирает links: знак после обратной косой, открывающая скобка
# ссылки или картинки, закрывающая скобка, пустая строка.
LINK_MARK = re.compile(r"\\[\s\S]|!?\[|\]|\n[ \t]*\n")
LINK_END = re.compile(r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?\s*\)")
REF_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)",
                     re.MULTILINE)
# Знак препинания ASCII после обратной косой в цели — сам этот знак.
ESCAPED = re.compile(r"\\([!-/:-@\[-`{-~])")


def link_target(text, i):
    """Цель ссылки Markdown, которая начинается за «](» в позиции i:
    (цель, её начало, конец ссылки) или None, если ссылки нет. Как в
    CommonMark, в цели допустимы парные скобки любой вложенности и скобка
    после обратной косой, а цель в угловых скобках — с пробелами."""
    while i < len(text) and text[i] in " \t\n":
        i += 1
    if text.startswith("<", i):
        end = text.find(">", i + 1)
        if end < 0 or "\n" in text[i + 1:end] or "<" in text[i + 1:end]:
            return None
        start, target, i = i + 1, text[i + 1:end], end + 1
    else:
        start = i
        depth = 0
        while i < len(text):
            ch = text[i]
            if ch == "\\" and i + 1 < len(text):
                i += 2
                continue
            if ch.isspace():
                break
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            i += 1
        if depth:
            return None
        target = text[start:i]
    end = LINK_END.match(text, i)
    if not end:
        return None
    return target, start, end.end()


def links(text):
    """Цели ссылок Markdown с их началом: встроенные и ссылки-определения.
    Скобки текста разбираются, как в CommonMark: «]» закрывает ближайшую
    открытую «[» или «![», поэтому в тексте ссылки допустимы парные
    квадратные скобки и картинка, и у ссылки вокруг картинки
    `[![i](a.png)](b.md)` берутся обе цели. Найденная ссылка, не картинка,
    делает простым текстом «[», открытые до неё: ссылки внутри ссылки
    CommonMark не допускает. Пустая строка закрывает все открытые скобки:
    ссылка не выходит за абзац."""
    found = []
    # Открытые «[» и «![»; пустая строка — «[», которой уже не стать
    # ссылкой.
    openers = []
    pos = 0
    while True:
        m = LINK_MARK.search(text, pos)
        if not m:
            break
        mark = m.group(0)
        pos = m.end()
        if mark[0] == "\\":
            continue
        if mark[0] == "\n":
            openers = []
            continue
        if mark != "]":
            openers.append(mark)
            continue
        opener = openers.pop() if openers else ""
        if not opener or not text.startswith("(", pos):
            continue
        link = link_target(text, pos + 1)
        if not link:
            continue
        found.append((link[0], link[1]))
        pos = link[2]
        if opener == "[":
            openers = [o if o == "![" else "" for o in openers]
    found += [(m.group(1), m.start(1)) for m in REF_DEF.finditer(text)]
    return sorted(found, key=lambda t: t[1])


def check_markdown(root, rel, lines, fmt, known):
    """Длина строки вне таблиц и блоков кода, блоки yaml, ссылки. Блок
    кода — только огороженный, таблица — строки, которые начинаются с «|»;
    блок с отступом, примечание HTML и строка таблицы без ведущей «|»
    проверяются на длину, как текст (README.md рядом). Незакрытый блок —
    нарушение: он спрятал бы остаток файла от длины и ссылок."""
    found = []
    limit = fmt["длина строки"]
    masked = []
    fence = None
    block_start = 0
    block_lang = ""
    block = []
    for no, line in enumerate(lines, start=1):
        m = FENCE.match(line)
        if fence is None and m:
            fence = m.group(1)
            block_start = no
            block_lang = m.group(2).lower()
            block = []
            masked.append("")
            continue
        if fence is not None:
            stripped = line.strip()
            if (m and stripped.startswith(fence[0] * len(fence))
                    and not m.group(2)):
                if block_lang in ("yaml", "yml") and fmt["yaml"]:
                    found.extend(check_yaml_text(
                        rel, "\n".join(block) + "\n", block_start + 1))
                fence = None
            else:
                block.append(line)
            masked.append("")
            continue
        masked.append(INLINE_CODE.sub(lambda x: " " * len(x.group(0)), line))
        if line.lstrip().startswith("|"):
            continue
        if limit and len(line) > limit:
            found.append(Violation(rel, no, "длина строки",
                                   "%d знаков, допустимо %d"
                                   % (len(line), limit)))
    if fence is not None:
        found.append(Violation(rel, block_start, "блок кода",
                               "блок кода открыт здесь и не закрыт: ниже него "
                               "длина строк и ссылки не проверяются"))
        if block_lang in ("yaml", "yml") and fmt["yaml"]:
            found.extend(check_yaml_text(rel, "\n".join(block) + "\n",
                                         block_start + 1))
    if fmt["ссылки"]:
        found.extend(check_links(root, rel, "\n".join(masked), known))
    return found


def link_paths(root, checked):
    """Пути от корня клона, на которые может вести ссылка: файлы, которые
    отслеживает git и которые лежат на диске, и каталоги с ними, а из
    проверяемых файлов checked — ещё новые, которых git не отслеживает и
    .gitignore не исключает: так у автора проверка не ломается до первого
    git add. Сверка — со списком git, а не с диском: файловая система
    Windows не различает регистр букв, а файл, которого нет в git, на
    площадке не появится. Файл, удалённый с диска без git rm, ещё в
    индексе, но после git add -A его не будет — он не цель."""
    files = set(git_files(root, "--cached"))
    files -= set(git_files(root, "--deleted"))
    new = set(checked) - files
    if new:
        files |= new & set(git_files(root, "--others", "--exclude-standard"))
    known = set([""])
    for name in files:
        known.add(name)
        parent = posixpath.dirname(name)
        while parent and parent not in known:
            known.add(parent)
            parent = posixpath.dirname(parent)
    return known


def check_links(root, rel, text, known):
    """Относительные ссылки Markdown ведут на пути из known с учётом
    регистра букв; адреса и якоря не проверяются."""
    found = []
    folded = None
    base = posixpath.dirname(rel)
    for target, pos in links(text):
        if target.startswith("#") or re.match(r"^[A-Za-z][\w+.-]*:", target):
            continue
        path = target.split("#", 1)[0].split("?", 1)[0]
        path = urllib.parse.unquote(ESCAPED.sub(r"\1", path))
        if not path:
            continue
        if path.startswith("/"):
            path = posixpath.normpath(path.lstrip("/") or ".")
        else:
            path = posixpath.normpath(posixpath.join(base, path))
        if path == ".":
            path = ""
        if path in known:
            continue
        what = "«%s» ведёт на несуществующий путь" % target
        if path != ".." and not path.startswith("../"):
            if folded is None:
                folded = dict((p.lower(), p) for p in known)
            same = folded.get(path.lower())
            if same is not None:
                what = ("«%s»: git знает путь «%s» — регистр букв другой, "
                        "и на площадке ссылка не откроется" % (target, same))
            elif os.path.lexists(os.path.join(root, *path.split("/"))):
                what = ("«%s» ведёт на путь, которого git не отслеживает: "
                        "на площадке его не будет" % target)
        found.append(Violation(rel, text.count("\n", 0, pos) + 1, "ссылка",
                               what))
    return found


def check_yaml_text(rel, text, first_line):
    """Разбор YAML безопасным загрузчиком; first_line — строка файла, с
    которой начинается текст."""
    try:
        for _ in kit.yaml.safe_load_all(text):
            pass
    except (kit.yaml.YAMLError, ValueError) as err:
        # Дата вне календаря — ValueError: datetime строится при загрузке, и
        # PyYAML её не оборачивает.
        mark = getattr(err, "problem_mark", None)
        line = first_line + (mark.line if mark else 0)
        problem = getattr(err, "problem", None) or str(err).splitlines()[0]
        return [Violation(rel, line, "yaml", "не разбирается: %s" % problem)]
    return []


# --- Какие файлы ------------------------------------------------------------

def git_files(root, *args):
    code, out, err = kit.git(root, "--literal-pathspecs", "ls-files", "-z",
                             *args)
    if code != 0:
        raise kit.ToolError("git ls-files не выполнен: %s" % err.strip())
    return [name for name in out.split("\0") if name]


def files_to_check(root, masks, paths):
    """Пути файлов от корня клона. Без путей — отслеживаемые git файлы по
    маскам; каталог — его файлы по маскам, которые git отслеживает или
    видит новыми; файл, названный явно, проверяется и вне масок."""
    if not paths:
        return sorted(n for n in git_files(root, "--cached")
                      if kit.matches(n, masks)
                      and os.path.isfile(os.path.join(root, n)))
    result = set()
    for path in paths:
        full = os.path.abspath(path)
        rel = kit.relative(full, root)
        if rel is None:
            raise kit.ToolError("%s — вне клона %s" % (path, root))
        if os.path.isdir(full):
            names = git_files(root, "--cached", "--others",
                              "--exclude-standard", "--", rel)
            result.update(n for n in names if kit.matches(n, masks)
                          and os.path.isfile(os.path.join(root, n)))
        elif os.path.isfile(full):
            result.add(rel)
        else:
            raise kit.ToolError("нет такого файла или каталога: %s" % path)
    return sorted(result)


# --- Запуск -----------------------------------------------------------------

def other_extension_line(checked):
    """Строка о файлах другого расширения: длина и ссылки у них не
    проверяются (check_file), а «нарушений 0» читалось бы как полная проверка.
    None — таких файлов нет."""
    other = [rel for rel in checked if not is_markdown(rel)]
    if not other:
        return None
    return ("файлов без проверки длины строки и ссылок: %d, например %s"
            % (len(other), other[0]))


def run(root, fmt, paths=None, config_path=kit.DEFAULT_CONFIG,
        exceptions_path=kit.DEFAULT_EXCEPTIONS):
    """(все нарушения, проверенные файлы); paths=None — полная проверка."""
    words = [(w["слово"], re.compile(w["образец"], re.IGNORECASE),
              w["вместо"]) for w in fmt["слова"]]
    exc_rel = (kit.relative(exceptions_path, root)
               or kit.shown(exceptions_path))
    exemptions, violations = kit.load_exceptions(exceptions_path, TOOL, KINDS,
                                                 exc_rel)
    # Слова в самой настройке названы, а не употреблены.
    config_rel = kit.relative(config_path, root)
    found = []
    file_lines = {}
    checked = files_to_check(root, fmt["файлы"], paths)
    if not checked:
        # Комплект сам приносит файлы .md: ноль файлов — ошибка настройки или
        # пути, и «нарушений 0» оставило бы проверку выключенной навсегда.
        raise kit.ToolError(
            "проверка не выполнена: нет ни одного файла для проверки; маски "
            "поля «файлы»: %s; пути: %s"
            % (", ".join(fmt["файлы"]) or "нет",
               ", ".join(paths) if paths else "не названы"))
    known = link_paths(root, checked) if fmt["ссылки"] else set()
    for rel in checked:
        items, lines = check_file(root, rel, fmt,
                                  [] if rel == config_rel else words, known)
        found.extend(items)
        file_lines[rel] = lines
    found = kit.apply_exceptions(found, exemptions, KINDS, file_lines)
    found.sort(key=lambda v: (v.path, v.line))
    violations += found
    if paths is None:
        violations += kit.unused_exceptions(exemptions, exc_rel)
    return violations, checked


def main(argv=None):
    kit.setup_output()
    parser = kit.make_parser(
        "check_docs.py",
        "Проверка формата документов по настройке, поле «формат "
        "документов» (AGENTS.md, 5.2, 5.4).")
    parser.add_argument(
        "paths", nargs="*", metavar="<файл или каталог>",
        help="что проверить; без параметров — файлы, которые отслеживает "
             "git, по маскам поля «файлы»")
    kit.add_config_argument(parser)
    args = parser.parse_args(argv)
    try:
        cfg = kit.load_config(args.config)
        fmt = cfg.get("формат документов")
        if fmt is None:
            raise kit.ToolError("формат документов не задан: в настройке нет "
                                "поля «формат документов»")
        root = kit.clone_root()
        try:
            violations, checked = run(root, fmt, args.paths or None,
                                      args.config or kit.DEFAULT_CONFIG)
        except OSError as err:
            raise kit.ToolError("проверка не выполнена: %s" % err)
        for v in violations:
            print(v)
        other = other_extension_line(checked)
        if other:
            print(other)
        print("нарушений %d" % len(violations))
        return kit.FOUND if violations else kit.OK
    except kit.ToolError as err:
        return kit.report(err)
    except KeyboardInterrupt:
        print("проверка прервана", file=sys.stderr)
        return kit.INTERRUPTED
    except Exception as err:
        return kit.report_crash(err)


if __name__ == "__main__":
    sys.exit(main())
