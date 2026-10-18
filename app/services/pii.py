"""Поиск и удаление персональных данных перед отправкой текста в нейросеть.

Текст в базе не меняется: персональные данные заменяются метками вроде [ФИО] или [телефон]
только в том, что уходит во внешний сервис ИИ.

Что распознаётся (по шаблонам, без отправки куда-либо):
  ФИО полностью и с инициалами, «Имя Фамилия» с распространёнными русскими именами,
  телефоны, e-mail, СНИЛС, паспорт, ИНН физлица (12 цифр), номера карт (с проверкой Луна),
  лицевые счета физлиц, даты рождения, адреса (улица + дом).

Распознавание по шаблонам не даёт 100% гарантии — поэтому пользователь видит, что найдено,
и может вычистить остальное вручную перед генерацией.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CAP = r"[А-ЯЁ][а-яё]+"
CAP_DASH = rf"{CAP}(?:-{CAP})?"
PATRONYMIC = r"[А-ЯЁ][а-яё]+(?:ович|евич|ич|овн|евн|ичн|иничн)[а-яё]{0,3}"

# Распространённые имена (именительный падеж). Падежные окончания добавляются в шаблоне.
FIRST_NAMES = (
    "Александр Алексей Анатолий Андрей Антон Аркадий Артём Артем Арсений Богдан Борис Вадим Валентин Валерий "
    "Василий Виктор Виталий Владимир Владислав Вячеслав Геннадий Георгий Глеб Григорий Даниил Денис Дмитрий "
    "Евгений Егор Иван Игорь Илья Кирилл Константин Леонид Максим Марк Матвей Михаил Никита Николай Олег "
    "Павел Пётр Петр Роман Руслан Семён Семен Сергей Станислав Степан Тимофей Тимур Фёдор Федор Юрий Ярослав "
    "Алина Алла Анастасия Ангелина Анна Антонина Валентина Валерия Варвара Вера Вероника Виктория Галина Дарья "
    "Диана Евгения Екатерина Елена Елизавета Жанна Зинаида Злата Инна Ирина Карина Кира Кристина Ксения Лариса "
    "Лидия Любовь Людмила Маргарита Марина Мария Милана Надежда Наталья Наталия Нина Оксана Ольга Полина "
    "Светлана Софья София Таисия Тамара Татьяна Ульяна Юлия Яна"
).split()
_NAME_STEMS = sorted({re.sub(r"(ий|ей|ай|ь|а|я|й)$", "", n) for n in FIRST_NAMES}, key=len, reverse=True)
FIRST_NAME = rf"(?:{'|'.join(_NAME_STEMS)})[а-яё]{{0,3}}"


@dataclass
class Rule:
    kind: str       # что найдено (для предупреждения)
    label: str      # чем заменяем
    pattern: re.Pattern
    group: int = 0  # какую часть совпадения заменять
    check: object = None


def _luhn(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            d -= 9 if d > 9 else 0
        total += d
    return total % 10 == 0


RULES = [
    Rule("e-mail", "[e-mail]", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    Rule("телефон", "[телефон]",
         re.compile(r"(?<!\d)(?:\+7|8)[\s\-(]{0,2}\d{3}[\s\-)]{0,2}\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)")),
    Rule("номер карты", "[номер карты]", re.compile(r"(?<!\d)(?:\d[ -]?){15,18}\d(?!\d)"), check=_luhn),
    Rule("СНИЛС", "[СНИЛС]", re.compile(r"(?<!\d)\d{3}-\d{3}-\d{3}[\s-]\d{2}(?!\d)")),
    Rule("паспорт", "[паспорт]", re.compile(r"(?i)паспорт[^\d\n]{0,25}(\d{2}\s?\d{2}\s?№?\s?\d{6})"), group=1),
    Rule("ИНН физлица", "[ИНН]", re.compile(r"(?i)ИНН[:\s№]{0,4}(\d{12})(?!\d)"), group=1),
    Rule("счёт физлица", "[номер счёта]", re.compile(r"(?<!\d)(?:40817|40820|423\d\d)\d{15}(?!\d)")),
    Rule("дата рождения", "[дата рождения]",
         re.compile(r"(?i)(?:дата рождения|родил(?:ся|ась)|г\.\s?р\.)[^\d\n]{0,20}(\d{1,2}[./]\d{1,2}[./]\d{2,4})"),
         group=1),
    Rule("адрес", "[адрес]", re.compile(
        r"(?i)(?:ул\.|улица|пр-т|проспект|пер\.|переулок|бульвар|б-р|шоссе|наб\.)\s+[А-ЯЁа-яё0-9.\- ]{2,40},?\s*"
        r"(?:д\.|дом)\s*\d+[а-яА-Я]?(?:[,\s]*(?:корп\.|к\.|стр\.)\s*\d+)?(?:[,\s]*(?:кв\.|квартира|оф\.)\s*\d+)?")),
    # ФИО: «Иванов Иван Иванович», «Иван Иванович Иванов», «Иванов И. И.», «И. И. Иванов»
    Rule("ФИО", "[ФИО]", re.compile(rf"\b{CAP_DASH}\s+{CAP}\s+{PATRONYMIC}\b")),
    Rule("ФИО", "[ФИО]", re.compile(rf"\b{CAP}\s+{PATRONYMIC}(?:\s+{CAP_DASH})?\b")),
    Rule("ФИО", "[ФИО]", re.compile(rf"\b{CAP_DASH}\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.")),
    Rule("ФИО", "[ФИО]", re.compile(rf"(?<![А-ЯЁа-яё])[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?{CAP_DASH}\b")),
    # «Сергей Петров», «Петрова Анна», «у Марии Ивановой»
    Rule("ФИО", "[ФИО]", re.compile(rf"\b{FIRST_NAME}\s+{CAP_DASH}(?:ов|ев|ин|ын|ский|цкий|ой|их|ых|ко|ук|юк|ер|ман)[а-яё]{{0,3}}\b")),
    Rule("ФИО", "[ФИО]", re.compile(rf"\b{CAP_DASH}(?:ов|ев|ин|ын|ский|цкий|ко|ук|юк)[а-яё]{{0,3}}\s+{FIRST_NAME}\b")),
]


@dataclass
class Finding:
    kind: str
    value: str


def _matches(text: str):
    """Все совпадения: (начало, конец, правило). Пересекающиеся находки объединяются в одну —
    лучше заменить лишнее слово, чем оставить кусок фамилии."""
    spans: list[tuple[int, int, Rule]] = []
    for rule in RULES:
        for m in rule.pattern.finditer(text):
            if rule.check and not rule.check(m.group(rule.group)):
                continue
            start, end = m.span(rule.group)
            spans.append((start, end, rule))
    spans.sort(key=lambda t: (t[0], -t[1]))
    merged: list[tuple[int, int, Rule]] = []
    for start, end, rule in spans:
        if merged and start < merged[-1][1]:
            s0, e0, r0 = merged[-1]
            merged[-1] = (s0, max(e0, end), r0)
        else:
            merged.append((start, end, rule))
    return merged


def find(text: str) -> list[Finding]:
    return [Finding(rule.kind, text[s:e]) for s, e, rule in _matches(text or "")]


def redact(text: str) -> str:
    text = text or ""
    out, pos = [], 0
    for start, end, rule in _matches(text):
        out.append(text[pos:start])
        out.append(rule.label)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def summary(findings: list[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.kind] = counts.get(f.kind, 0) + 1
    return counts


def describe(counts: dict[str, int]) -> str:
    """«ФИО — 2, телефон — 1» для предупреждений и истории."""
    return ", ".join(f"{kind} — {n}" for kind, n in counts.items())


def mask(value: str) -> str:
    """Как показать найденное пользователю, не выставляя данные целиком."""
    value = value.strip()
    if len(value) <= 4:
        return "•" * len(value)
    return value[:2] + "•" * (len(value) - 4) + value[-2:]
