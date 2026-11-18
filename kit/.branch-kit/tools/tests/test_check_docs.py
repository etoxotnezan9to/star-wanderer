"""check_docs.py обязан падать: на каждый вид нарушения — испорченный вход,
и он пойман.

Испорченные файлы создаются во временном клоне при каждом запуске, а не
лежат в tests: файл с BOM или CRLF в самом репозитории ломал бы его
проверку и зависел бы от настройки концов строк у того, кто клонирует."""

import contextlib
import copy
import io
import os
import unittest
from unittest import mock

import helpers
import check_docs

FENCE = "`" * 3


class Base(unittest.TestCase):

    def setUp(self):
        self.clone = helpers.Clone(files={"exists.md": "# Есть\n"})
        self.addCleanup(self.clone.remove)
        self.fmt = copy.deepcopy(helpers.CONFIG["формат документов"])

    def check(self, *rels, **kwargs):
        """Нарушения названных файлов; без файлов — полная проверка."""
        full = kwargs.get("full", False)
        paths = None if full else [self.clone.path(r) for r in rels]
        if full:
            self.clone.git("add", "-A")
        return check_docs.run(self.clone.root, self.fmt, paths,
                              self.clone.config_path,
                              self.clone.exceptions_path)[0]

    def caught(self, rel, content, kind, line=None, count=1):
        self.clone.write(rel, content)
        found = [v for v in self.check(rel) if v.kind == kind]
        self.assertEqual(len(found), count,
                         "%s: %s" % (kind, [str(v) for v in found]))
        if line is not None:
            self.assertEqual(found[0].line, line, str(found[0]))
        return found

    def clean(self, rel, content):
        self.clone.write(rel, content)
        self.assertEqual([str(v) for v in self.check(rel)], [])


class KindsTest(Base):

    def test_clean(self):
        """Чистый файл: таблица и блок кода длиннее предела, ссылки на
        существующие пути, адрес и якорь, ссылка в коде — нарушений нет."""
        long = "x" * 100
        text = ("# Чистый\n\nСсылка на [файл](exists.md), на [раздел]"
                "(exists.md#есть),\nна [площадку](http://127.0.0.1/x) и "
                "на [этот\nабзац](#чистый); `[код](нет.md)` не ссылка.\n\n"
                "| a | %s |\n|---|---|\n\n%s\n%s\n%s\n\n%syaml\nа: 1\n%s\n"
                % (long, FENCE, long, FENCE, FENCE, FENCE))
        self.clean("clean.md", text)
        self.clean("clean.yaml", "а: 1\n---\nб: 2\n")

    def test_bom_unwanted(self):
        self.caught("bom.md", b"\xef\xbb\xbf# A\n", "кодировка", 1)

    def test_bom_wanted(self):
        """При «utf-8 с BOM» файл без BOM ловится, с BOM — нет."""
        self.fmt["кодировка"] = "utf-8 с BOM"
        found = self.caught("nobom.md", "# A\n", "кодировка", 1)
        self.assertIn("нет BOM", found[0].text)
        self.clean("bom.md", b"\xef\xbb\xbf# A\n")

    def test_not_utf8(self):
        self.caught("cp1251.md", "# A\n\nТекст\n".encode("cp1251"),
                    "кодировка", 3)

    def test_crlf_unwanted(self):
        found = self.caught("crlf.md", b"# A\r\n\r\nB\r\n", "концы строк", 1)
        self.assertIn("CRLF в строках: 3", found[0].text)

    def test_lf_unwanted(self):
        """При «CRLF» строка с LF без CR ловится, файл в CRLF — нет."""
        self.fmt["концы строк"] = "CRLF"
        found = self.caught("mixed.md", b"# A\r\n\nB\r\n", "концы строк", 2)
        self.assertIn("LF без CR в строках: 1", found[0].text)
        self.clean("crlf.md", b"# A\r\n\r\nB\r\n")

    def test_lone_cr(self):
        found = self.caught("cr.md", b"# A\n\nB\rC\n", "концы строк", 3)
        self.assertIn("одиночный CR", found[0].text)

    def test_no_final_newline(self):
        self.caught("eol.md", "# A\n\nB", "конец файла", 3)
        self.fmt["перевод строки в конце"] = False
        self.clean("eol2.md", "# A\n\nB")

    def test_long_line(self):
        """Строка в 79 знаков ловится, в 78 — нет; 0 — не проверяется."""
        text = "# A\n\n%s\n%s\n" % ("я" * 78, "я" * 79)
        found = self.caught("long.md", text, "длина строки", 4)
        self.assertEqual(found[0].text, "79 знаков, допустимо 78")
        self.fmt["длина строки"] = 0
        self.clean("long.md", text)

    def test_long_line_blocks(self):
        """Блок кода — только огороженный, таблица — строки с ведущей «|»
        (README): длинная строка в блоке с отступом, в примечании HTML и в
        строке таблицы без ведущей «|» — нарушение, в огороженном блоке и в
        строке с ведущей «|» — нет."""
        long = "x" * 100
        text = ("# A\n\n    %s\n\n<!-- %s -->\n\nа | б\n---|---\n%s | в\n\n"
                "| %s |\n\n%s\n%s\n%s\n" % (long, long, long, long, FENCE,
                                            long, FENCE))
        found = self.caught("blocks.md", text, "длина строки", count=3)
        self.assertEqual([v.line for v in found], [3, 5, 9])

    def test_word(self):
        """Слово по образцу ловится в любой форме, с заменой в выводе."""
        found = self.caught("words.md", "# A\n\nНовые Фичи и фича.\n",
                            "слово", 3, count=2)
        self.assertEqual(found[0].text, "«Фичи» не употребляется (слово "
                         "«фича»); вместо — «новая возможность»")

    def test_word_in_yaml(self):
        self.caught("words.yaml", "# фича\nа: б\n", "слово", 1)

    def test_word_not_in_config(self):
        """В самой настройке слова названы, а не употреблены."""
        self.fmt["файлы"] = ["*.md", "*.yaml"]
        self.assertEqual([v for v in self.check(".branch-kit/config.yaml")
                          if v.kind == "слово"], [])

    def test_broken_link(self):
        """Ссылка на несуществующий путь ловится, в том числе с текстом в
        две строки и ссылкой-определением."""
        text = ("# A\n\n[нет](missing.md)\n\n[текст\nссылки](../out.md)\n\n"
                "[x]: нет/такого.md\n")
        found = self.caught("link.md", text, "ссылка", count=3)
        self.assertEqual([v.line for v in found], [3, 6, 8])
        self.fmt["ссылки"] = False
        self.clean("link.md", text)

    def test_link_case(self):
        """Ссылка в другом регистре букв — нарушение на любой системе: цель
        сверяется с путями git, а не с диском, который в Windows регистр не
        различает. Каталог с отслеживаемыми файлами и корень — пути."""
        self.clone.write("docs/guide.md", "# Г\n")
        self.clone.commit()
        found = self.caught("case.md", "# A\n\n[a](docs/Guide.md)\n\n"
                            "[b](Docs/)\n", "ссылка", count=2)
        self.assertEqual([v.line for v in found], [3, 5])
        self.assertEqual(found[0].text, "«docs/Guide.md»: git знает путь "
                         "«docs/guide.md» — регистр букв другой, и на "
                         "площадке ссылка не откроется")
        self.clean("case.md", "# A\n\n[a](docs/guide.md), [b](docs),\n"
                   "[c](docs/), [d](./), [e](/docs/guide.md#г)\n")

    def test_link_untracked(self):
        """Ссылка на файл, которого git не отслеживает, — нарушение, хотя
        файл на диске есть. Новый файл, который проверяется сам, — цель, а
        исключённый .gitignore — нет: на площадку он не попадёт."""
        self.clone.write(".gitignore", "ignored.md\n")
        self.clone.commit()
        self.clone.write("draft.md", "# Черновик\n")
        self.clone.write("ignored.md", "# Скрыт\n")
        text = "# A\n\n[d](draft.md)\n\n[i](ignored.md)\n"
        found = self.caught("new.md", text, "ссылка", count=2)
        self.assertEqual([v.line for v in found], [3, 5])
        self.assertEqual(found[0].text, "«draft.md» ведёт на путь, которого "
                         "git не отслеживает: на площадке его не будет")
        found = [v for v in self.check("new.md", "draft.md", "ignored.md")
                 if v.kind == "ссылка"]
        self.assertEqual([(v.path, v.line) for v in found], [("new.md", 5)])

    def test_link_untracked_full(self):
        """Полная проверка: ссылка из отслеживаемого файла на новый,
        которого git не отслеживает, — нарушение."""
        self.clone.write("index.md", "# A\n\n[d](draft.md)\n")
        self.clone.commit()
        self.clone.write("draft.md", "# Черновик\n")
        found = check_docs.run(self.clone.root, self.fmt, None,
                               self.clone.config_path,
                               self.clone.exceptions_path)[0]
        self.assertEqual([(v.path, v.line, v.kind) for v in found],
                         [("index.md", 3, "ссылка")])

    def test_link_deleted(self):
        """Файл удалён с диска без git rm — он ещё в индексе, но ссылка на
        него и на каталог, где других файлов нет, — нарушение: после
        git add -A файла не будет."""
        self.clone.write("docs/a.md", "# A\n")
        self.clone.write("index.md", "# И\n\n[a](docs/a.md)\n\n[d](docs/)\n")
        self.clone.commit()
        os.remove(self.clone.path("docs/a.md"))
        self.assertIn("docs/a.md", self.clone.git("ls-files", "--cached"))
        # Пустой каталог на диске остался, но git его не отслеживает.
        expected = [("index.md", 3, "«docs/a.md» ведёт на несуществующий "
                     "путь"),
                    ("index.md", 5, "«docs/» ведёт на путь, которого git не "
                     "отслеживает: на площадке его не будет")]
        for full in (False, True):
            with self.subTest(full=full):
                found = check_docs.run(self.clone.root, self.fmt,
                                       None if full
                                       else [self.clone.path("index.md")],
                                       self.clone.config_path,
                                       self.clone.exceptions_path)[0]
                self.assertEqual([(v.path, v.line, v.text) for v in found],
                                 expected)

    def test_link_parens(self):
        """Парные скобки в цели — часть пути, в том числе вложенные, и
        скобка после обратной косой — тоже; цель в угловых скобках — с
        пробелом; заголовок ссылки в путь не входит."""
        for rel in ("docs/a(b).md", "docs/a((b)).md", "docs/c d.md",
                    "docs/e(f.md"):
            self.clone.write(rel, "# A\n")
        self.clone.commit()
        self.clean("p.md", "# A\n\n[c](docs/a(b).md), [c](docs/a((b)).md "
                   "\"Заголовок\"),\n[c](docs/a\\(b\\).md), [c](docs/e\\(f.md) "
                   "и [c](<docs/c d.md>).\n")
        found = self.caught("p2.md", "# A\n\n[c](docs/x(y).md)\n\n"
                            "[c](<docs/x y.md>) и [c](docs/x.md \"Заголовок\")"
                            "\n\n[c](docs/y\\(z.md)\n", "ссылка", count=4)
        self.assertEqual([v.text for v in found],
                         ["«docs/x(y).md» ведёт на несуществующий путь",
                          "«docs/x y.md» ведёт на несуществующий путь",
                          "«docs/x.md» ведёт на несуществующий путь",
                          "«docs/y\\(z.md» ведёт на несуществующий путь"])

    def test_link_around_image(self):
        """Ссылка вокруг картинки: проверяются обе цели — картинки и
        ссылки; парные квадратные скобки в тексте ссылки, как в CommonMark,
        ссылку не обрывают."""
        self.clone.write("a.png", b"png")
        self.clone.commit()
        self.clean("img.md", "# A\n\n[![i](a.png)](exists.md) и [текст [в "
                   "скобках]](exists.md)\n")
        found = self.caught("img2.md", "# A\n\n[![i](a.png)](nope.md)\n\n"
                            "[![i](nope.png)](exists.md)\n\n[текст [в "
                            "скобках]](nope2.md)\n", "ссылка", count=3)
        self.assertEqual([(v.line, v.text) for v in found],
                         [(3, "«nope.md» ведёт на несуществующий путь"),
                          (5, "«nope.png» ведёт на несуществующий путь"),
                          (7, "«nope2.md» ведёт на несуществующий путь")])

    def test_link_not_link(self):
        """Как в CommonMark, ссылкой не становятся «[», открытая до другой
        ссылки, и скобки по разные стороны пустой строки; «[» и «]» после
        обратной косой — простой текст. «]» закрывает ближайшую открытую
        «[», и без «(» за ней — тоже; скобки в цели ссылки — часть цели,
        ссылок они не открывают."""
        self.clean("not.md", "# A\n\n[x [a](exists.md)](nope.md)\n\n[b\n\n"
                   "c](nope.md)\n\n\\[d](nope.md) и [e\\](nope.md)\n\n"
                   "[f] и g](nope.md), [h] (i) j](nope.md)\n")
        found = self.caught("not2.md", "# A\n\n[x [a](nope.md)](exists.md)\n"
                            "\n\\\\[d](nope2.md)\n\n[x [a](exists.md) "
                            "[b](nope3.md)\n\n[t](x[y](nope4.md))\n",
                            "ссылка", count=4)
        self.assertEqual([v.text for v in found],
                         ["«nope.md» ведёт на несуществующий путь",
                          "«nope2.md» ведёт на несуществующий путь",
                          "«nope3.md» ведёт на несуществующий путь",
                          "«x[y](nope4.md)» ведёт на несуществующий путь"])

    def test_bad_yaml_file(self):
        self.caught("bad.yaml", "а: [1, 2\nб: 3\n", "yaml")

    def test_unsafe_yaml(self):
        """Объект Python безопасным разбором не разбирается — ловится."""
        self.caught("unsafe.yaml", "а: !!python/object:os.system {}\n",
                    "yaml", 1)

    def test_bad_yaml_block(self):
        """Неразборный блок yaml в .md ловится со строкой в файле."""
        text = "# A\n\n%syaml\nа: 1\nб: [1\n%s\n" % (FENCE, FENCE)
        self.caught("block.md", text, "yaml")
        self.fmt["yaml"] = False
        self.clean("block.md", text)
        self.clean("bad.yaml", "а: [1, 2\n")

    def test_bad_date(self):
        """Дата вне календаря: PyYAML бросает ValueError, а не YAMLError —
        нарушение yaml у файла и у блока, и остальные файлы проверяются.
        Опорный: дата из календаря нарушения не даёт."""
        self.caught("date.yaml", "дата: 2026-02-30\n", "yaml", 1)
        self.caught("date.md", "# A\n\n%syaml\nдата: 2026-02-30\n%s\n"
                    % (FENCE, FENCE), "yaml", 4)
        self.clone.write("long.md", "# A\n\n%s\n" % ("я" * 79))
        self.assertEqual(
            [(v.path, v.kind) for v in self.check("date.yaml", "long.md")],
            [("date.yaml", "yaml"), ("long.md", "длина строки")])
        self.clean("date.yaml", "дата: 2026-02-28\n")
        self.clean("date.md", "# A\n\n%syaml\nдата: 2026-02-28\n%s\n"
                   % (FENCE, FENCE))


class FenceTest(Base):
    """Блок кода по CommonMark: незакрытый — нарушение со строкой открытия,
    закрывает его только такая же или более длинная строка того же знака
    без сведений, а строка с обратными кавычками в сведениях блока не
    открывает."""

    def test_unclosed(self):
        """Незакрытый блок прятал бы остаток файла от длины и ссылок."""
        for opener in (FENCE, "~~~", FENCE + "python", "~~~~", "  " + FENCE):
            with self.subTest(opener):
                found = self.caught("a.md", "# A\n\n%s\nкод\n%s\n[a](нет.md)"
                                    "\n" % (opener, "я" * 79), "блок кода", 3)
                self.assertIn("не закрыт", found[0].text)

    def test_wrong_closer(self):
        """Строка, которая блок не закрывает: короче, другого знака или со
        сведениями — блок остаётся незакрытым."""
        cases = ((FENCE + "`", FENCE),
                 (FENCE, "~~~"),
                 ("~~~", FENCE),
                 (FENCE, FENCE + " текст"),
                 (FENCE, "%sx%s" % (FENCE, FENCE)),
                 (FENCE, "%s `x`" % FENCE),
                 ("~~~", "~~~ `x`"))
        for opener, closer in cases:
            with self.subTest(opener=opener, closer=closer):
                self.caught("a.md", "# A\n\n%s\nкод\n%s\n"
                            % (opener, closer), "блок кода", 3)

    def test_closed(self):
        """Опорные: вложенный блок, длинный закрывающий забор, сведения —
        нарушений нет, и длинная строка в блоке не считается."""
        long = "я" * 100
        cases = ("%s`\n%s\n%s\n%s\n%s`\n" % (FENCE, FENCE, long, FENCE,
                                               FENCE),
                 "~~~\n%s\n%s\n~~~\n" % (FENCE, long),
                 "%s\n~~~\n%s\n%s\n" % (FENCE, long, FENCE),
                 "%s\n%s\n%s`\n" % (FENCE, long, FENCE),
                 "%spython\n%s\n%s\n" % (FENCE, long, FENCE),
                 "~~~ `сведения`\n%s\n~~~\n" % long,
                 "  %s\n%s\n  %s\n" % (FENCE, long, FENCE))
        for text in cases:
            with self.subTest(text):
                self.clean("a.md", "# A\n\n" + text)

    def test_backticks_in_info(self):
        """Строка «```x``` — пример» — обычный текст: ниже неё длина и ссылки
        проверяются, а строка блока не открывает."""
        self.clone.write("a.md", "# A\n\n%sx%s — пример\n\n%s\n\n[a](нет.md)\n"
                         % (FENCE, FENCE, "я" * 79))
        self.assertEqual([(v.kind, v.line) for v in self.check("a.md")],
                         [("длина строки", 5), ("ссылка", 7)])

    def test_unclosed_yaml(self):
        """Незакрытый блок yaml по-прежнему разбирается: нарушение и блока, и
        yaml; годный yaml — только нарушение блока."""
        self.clone.write("a.md", "# A\n\n%syaml\nа: [\n" % FENCE)
        self.assertEqual(sorted(v.kind for v in self.check("a.md")),
                         ["yaml", "блок кода"])
        self.clone.write("a.md", "# A\n\n%syaml\nа: 1\n" % FENCE)
        self.assertEqual([v.kind for v in self.check("a.md")], ["блок кода"])

    def test_closed_after_unclosed_text(self):
        """Обычный текст с обратными кавычками не сбивает пару заборов: блок
        ниже него закрыт, и длинная строка в нём не считается."""
        self.clean("a.md", "# A\n\n%sx%s — пример\n\n%s\n%s\n%s\n"
                   % (FENCE, FENCE, FENCE, "я" * 100, FENCE))


class FilesTest(Base):

    def test_masks_and_tracked(self):
        """Без параметров — только отслеживаемые git файлы по маскам; «*»
        совпадает и с косой чертой."""
        self.clone.write("docs/deep/a.md", "# A\n\nфича\n")
        self.clone.write("notes.txt", "фича\n")
        self.clone.commit()
        self.clone.write("untracked.md", "фича\n")
        found = check_docs.run(self.clone.root, self.fmt, None,
                               self.clone.config_path,
                               self.clone.exceptions_path)[0]
        self.assertEqual([(v.path, v.kind) for v in found],
                         [("docs/deep/a.md", "слово")])

    def test_mask_rules(self):
        """Маски — fnmatchcase: «*», «?» и «[!x]» берут и косую черту, как
        pathspec git, «**» — то же, что «*», регистр букв различается."""
        cases = (("docs/a/b.md", "docs/*.md", True),
                 ("docs/b.md", "docs?b.md", True),
                 ("docs/a/b.md", "docs[!x]a/b.md", True),
                 ("docs/a/b.md", "docs/**", True),
                 ("b.md", "**/b.md", False),
                 ("docs/A.md", "docs/a.md", False))
        for rel, mask, expected in cases:
            with self.subTest(mask=mask, rel=rel):
                self.assertEqual(check_docs.kit.matches(rel, [mask]),
                                 expected)

    def test_directory_and_file(self):
        """Каталог — его файлы по маскам, в том числе новые; файл, названный
        явно, проверяется и вне масок."""
        self.clone.write("docs/new.md", "фича\n")
        self.clone.write("docs/skip.txt", "фича\n")
        self.clone.write("named.txt", "фича\n")
        found = self.check("docs", "named.txt")
        self.assertEqual(sorted(v.path for v in found),
                         ["docs/new.md", "named.txt"])

    def test_bad_paths(self):
        outside = helpers.Clone()
        self.addCleanup(outside.remove)
        for path, text in ((outside.path("README.md"), "вне клона"),
                           (self.clone.path("нет.md"), "нет такого файла")):
            with self.subTest(text):
                with self.assertRaises(check_docs.kit.ToolError) as ctx:
                    check_docs.run(self.clone.root, self.fmt, [path],
                                   self.clone.config_path,
                                   self.clone.exceptions_path)
                self.assertIn(text, str(ctx.exception))


LONG = "я" * 80
OTHER_EXT = "файлов без проверки длины строки и ссылок: %d, например %s"


class ExceptionsTest(Base):

    def set_exceptions(self, text):
        self.clone.write(".branch-kit/exceptions.yaml",
                         "исключения:\n" + text)

    def test_covered(self):
        self.set_exceptions("  - средство: check_docs.py\n    место: long.md"
                            "\n    вид: длина строки\n    причина: таблица\n")
        self.clone.write("long.md", "# A\n\n%s\n" % LONG)
        self.assertEqual([str(v) for v in self.check("long.md")], [])

    def test_paragraph(self):
        """Исключение с абзацем снимает нарушение только в этом абзаце."""
        self.set_exceptions("  - средство: check_docs.py\n    место: long.md"
                            "\n    вид: длина строки\n    абзац: \"# Длин\"\n"
                            "    причина: заголовок\n")
        self.clone.write("long.md", "# Длинный %s\n\n%s\n" % (LONG, LONG))
        self.assertEqual([(v.kind, v.line) for v in self.check("long.md")],
                         [("длина строки", 3)])

    def test_without_reason(self):
        """Исключение без причины — нарушение, и оно ничего не снимает."""
        self.set_exceptions("  - средство: check_docs.py\n    место: long.md"
                            "\n    вид: длина строки\n")
        self.clone.write("long.md", "# A\n\n%s\n" % LONG)
        found = self.check("long.md")
        self.assertEqual(sorted(v.kind for v in found),
                         ["длина строки", "исключение"])
        self.assertIn(".branch-kit/exceptions.yaml:2: исключение: нет "
                      "причины", [str(v) for v in found])

    def test_unused(self):
        """При полной проверке исключение, которое ничего не покрыло, —
        нарушение; при проверке названных файлов — нет."""
        self.set_exceptions("  - средство: check_docs.py\n    место: нет.md\n"
                            "    вид: слово\n    причина: устарело\n")
        found = [str(v) for v in self.check(full=True)]
        self.assertEqual(found, [".branch-kit/exceptions.yaml:2: исключение: "
                                 "ничего не покрыло — уберите или поправьте "
                                 "запись"])
        self.assertEqual(self.check("exists.md"), [])

    def test_fence_kind(self):
        """Вид «блок кода» разбирают исключением, как и остальные виды."""
        self.set_exceptions("  - средство: check_docs.py\n    место: a.md\n"
                            "    вид: блок кода\n    причина: разобрано\n")
        self.clone.write("a.md", "# A\n\n%s\nкод\n" % FENCE)
        self.assertEqual([str(v) for v in self.check("a.md")], [])

    def test_bad_date(self):
        """Дата вне календаря в файле исключений — нарушение «исключение»,
        как негодный корень."""
        self.clone.write(".branch-kit/exceptions.yaml",
                         "исключения: []\nдата: 2026-02-30\n")
        self.assertEqual([v.kind for v in self.check("exists.md")],
                         ["исключение"])


class ZeroFilesTest(unittest.TestCase):
    """Ни одного проверенного файла — ошибка настройки или пути: код 2, а не
    «нарушений 0». Комплект сам приносит файлы .md, и в рабочем репозитории
    ноля быть не может."""

    def clone(self, masks, files=None):
        cfg = helpers.config()
        cfg["формат документов"]["файлы"] = masks
        clone = helpers.Clone(cfg, files=dict({"docs/a.md": "# A\n"},
                                              **(files or {})))
        self.addCleanup(clone.remove)
        return clone

    def not_done(self, run, *names):
        self.assertEqual(run.code, 2, run)
        self.assertEqual(run.out, "")
        self.assertIn("проверка не выполнена", run.err)
        for name in names:
            self.assertIn(name, run.err)

    def test_masks(self):
        """Маска без единого совпадения: каталог вместо маски, другой
        регистр, опечатка — маска названа."""
        for mask in ("docs/", "docs/*.MD", "*.mb"):
            with self.subTest(mask):
                self.not_done(self.clone([mask]).run("check_docs.py"), mask)

    def test_directory(self):
        """Каталог без файлов по маскам — и маски, и путь названы."""
        clone = self.clone(["*.md"], {"empty/a.txt": "текст\n"})
        self.not_done(clone.run("check_docs.py", "empty"), "*.md", "empty")

    def test_control(self):
        """Опорные: маска с совпадением, каталог с файлами и файл вне масок —
        проверка идёт; несуществующий каталог — прежний отказ."""
        clone = self.clone(["docs/*.md"], {"named.txt": "текст\n"})
        for args, lines in (
                ((), ["нарушений 0"]),
                (("docs",), ["нарушений 0"]),
                (("named.txt",), [OTHER_EXT % (1, "named.txt"),
                                  "нарушений 0"])):
            with self.subTest(args):
                run = clone.run("check_docs.py", *args)
                self.assertEqual(run.code, 0, run)
                self.assertEqual(run.lines, lines)
        run = clone.run("check_docs.py", "нет")
        self.assertEqual(run.code, 2, run)
        self.assertIn("нет такого файла или каталога", run.err)


class CommandLineTest(unittest.TestCase):

    def test_output_and_codes(self):
        """Строки «файл:строка: вид: что не так», итог «нарушений N»,
        коды 1 и 0."""
        clone = helpers.Clone(files={"long.md": "# A\n\n%s\n" % LONG})
        self.addCleanup(clone.remove)
        run = clone.run("check_docs.py")
        self.assertEqual(run.code, 1, run)
        self.assertEqual(run.lines, ["long.md:3: длина строки: 80 знаков, "
                                     "допустимо 78", "нарушений 1"])
        run = clone.run("check_docs.py", "README.md")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(run.lines, ["нарушений 0"])

    def test_other_extension_named(self):
        """Файл другого расширения проверяется без длины и ссылок: итог об
        этом говорит строкой перед собой, код и последняя строка прежние.
        YAML — тоже: длина и ссылки у него не проверяются."""
        clone = helpers.Clone(files={
            "b.txt": "%s\n\n[x](нет.md)\n" % LONG, "a.rst": "# A\n",
            "c.yaml": "а: 1\n"})
        self.addCleanup(clone.remove)
        run = clone.run("check_docs.py", "b.txt", "a.rst", "c.yaml",
                        "README.md")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(run.lines, [OTHER_EXT % (3, "a.rst"), "нарушений 0"])

    def test_other_extension_mask(self):
        """То же для файлов по маске, и строка стоит перед итогом, а не среди
        нарушений: код 1 от нарушения, а не от неё."""
        cfg = helpers.config()
        cfg["формат документов"]["файлы"] = ["*.txt"]
        clone = helpers.Clone(cfg, files={"doc.txt": "фича\n"})
        self.addCleanup(clone.remove)
        run = clone.run("check_docs.py")
        self.assertEqual(run.code, 1, run)
        self.assertEqual(run.lines[1:], [OTHER_EXT % (1, "doc.txt"),
                                         "нарушений 1"])
        self.assertTrue(run.lines[0].startswith("doc.txt:1: слово:"), run)

    def test_markdown_extensions_silent(self):
        """Опорный: у файлов .md и .markdown в любом регистре длина и ссылки
        проверяются, строки о других расширениях нет."""
        clone = helpers.Clone(files={"a.markdown": "# A\n", "b.MD": "# B\n"})
        self.addCleanup(clone.remove)
        run = clone.run("check_docs.py", "a.markdown", "b.MD", "README.md")
        self.assertEqual(run.code, 0, run)
        self.assertEqual(run.lines, ["нарушений 0"])

    def test_other_extension_in_check_repo(self):
        """Строка «строка в выводе» настройки ждёт итог целиком: новая строка
        перед ним проверку docs не ломает."""
        cfg = helpers.config()
        cfg["формат документов"]["файлы"] = ["*.md", "*.txt"]
        clone = helpers.Clone(cfg, files={"doc.txt": "текст\n"})
        self.addCleanup(clone.remove)
        run = clone.run("check_repo.py")
        self.assertEqual(run.code, 0, run)
        self.assertTrue(run.lines[0].startswith("docs: прошла;"), run)

    def test_no_format(self):
        clone = helpers.Clone(helpers.config(формат_документов=None))
        self.addCleanup(clone.remove)
        run = clone.run("check_docs.py")
        self.assertEqual(run.code, 2, run)
        self.assertIn("формат документов не задан", run.err)
        self.assertEqual(run.out, "")

    def test_bad_pattern(self):
        cfg = helpers.config()
        cfg["формат документов"]["слова"][0]["образец"] = "[фич"
        clone = helpers.Clone(cfg)
        self.addCleanup(clone.remove)
        run = clone.run("check_docs.py")
        self.assertEqual(run.code, 2, run)
        self.assertIn("поле «формат документов / слова / 1 / образец»: "
                      "образец не разбирается как регулярное выражение",
                      run.err)

    def test_other_directory(self):
        """Средство берёт клон и настройку по своему месту, а не по
        текущему каталогу."""
        clone = helpers.Clone(files={"long.md": "# A\n\n%s\n" % LONG})
        self.addCleanup(clone.remove)
        run = clone.run("check_docs.py", cwd=os.path.dirname(clone.root))
        self.assertEqual(run.lines[-1], "нарушений 1", run)

    def test_deep_yaml(self):
        """Падение на настоящем входе: глубина блока yaml упирается в предел
        рекурсии — код 2, а не код 1 «есть нарушения»; вид и текст в потоке
        ошибок, трассировки нет."""
        clone = helpers.Clone(files={"deep.yaml": "а: " + "[" * 3000 + "\n"})
        self.addCleanup(clone.remove)
        cfg = helpers.config()
        cfg["формат документов"]["файлы"] = ["*.yaml"]
        clone.set_config(cfg)
        run = clone.run("check_docs.py")
        self.assertEqual(run.code, 2, run)
        self.assertIn("RecursionError", run.err)
        self.assertNotIn("Traceback", run.err)
        self.assertEqual(run.out, "")


class InProcessTest(unittest.TestCase):
    """Падение и Ctrl+C — в том же процессе."""

    def main(self, error):
        clone = helpers.Clone()
        self.addCleanup(clone.remove)
        out, err = io.StringIO(), io.StringIO()
        with helpers.patch_env(GITLAB_TOKEN=helpers.TOKENS["ai-dev"]), \
                mock.patch.object(check_docs, "run", side_effect=error), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = check_docs.main(["--config", clone.config_path])
        self.assertEqual(out.getvalue(), "")
        self.assertNotIn(helpers.TOKENS["ai-dev"], err.getvalue())
        return code, err.getvalue()

    def test_crash(self):
        """Необработанное исключение — код 2, вид и текст, ключа нет."""
        code, err = self.main(RuntimeError(
            "сбой, ключ %s" % helpers.TOKENS["ai-dev"]))
        self.assertEqual(code, 2)
        self.assertIn("RuntimeError: сбой, ключ", err)
        self.assertNotIn("Traceback", err)

    def test_ctrl_c(self):
        """Ctrl+C — код 130, итога нет."""
        try:
            code, err = self.main(KeyboardInterrupt())
        except KeyboardInterrupt:
            self.fail("Ctrl+C вышел из main мимо перехвата")
        self.assertEqual(code, 130)
        self.assertIn("проверка прервана", err)


if __name__ == "__main__":
    unittest.main()
