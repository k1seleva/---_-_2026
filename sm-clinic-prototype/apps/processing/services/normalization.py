"""
Нормализация текста протокола с картой позиций.

Признаки ищутся по нормализованному тексту, а подсвечиваются в исходном: каждый символ
нормализованного текста помнит, из какого символа исходного он получился. Поэтому исправленная
опечатка или раскладка не сдвигает подсветку и не меняет цитату-доказательство.

Что нормализуется:
* регистр и «ё» -> «е»;
* неразрывные и прочие пробелы -> обычный пробел, серия пробелов -> один (переводы строк остаются);
* длинные тире и минус -> «-», кавычки «ёлочки» и «лапки» -> «"»;
* смешанная раскладка в одном слове: латинская буква среди кириллических и наоборот
  («Тi-rads» с кириллической «Т», «обрaзование» с латинской «a»);
* частые опечатки протоколов (TYPO_FIXES): список собран по прогону протоколов кейса.

Исправления раскладки и опечаток запоминаются (corrections): маркер, найденный только благодаря
им, получает пониженную уверенность.
"""
import re
from dataclasses import dataclass, field
from functools import lru_cache

SPACE_CHARS = frozenset("           \t\x0b\x0c")
DROP_CHARS = frozenset("\r​‌‍﻿­")
DASHES = frozenset("‐‑‒–—―−")
QUOTES = frozenset("«»„“”‟″")
# Буквы, которые одинаково выглядят в латинице и кириллице (после перевода в нижний регистр).
LAT_TO_CYR = dict(zip("aceopxykmthb", "асеорхукмтнв"))
CYR_TO_LAT = {v: k for k, v in LAT_TO_CYR.items()}
LATIN = re.compile(r"[a-z]")
CYRILLIC = re.compile(r"[а-я]")
TOKEN_RE = re.compile(r"[a-zа-я0-9]+(?:-[a-zа-я0-9]+)*")
# Опечатка -> верное написание (подстрока, в нижнем регистре, «ё» уже заменена).
TYPO_FIXES = {
    "гидросальпингс": "гидросальпинкс",
    "вуизуализ": "визуализ",
    "образованя": "образовани",
    "раширен": "расширен",
    "регестр": "регистр",
    "рекомендовнао": "рекомендовано",
    "конкримент": "конкремент",
    "эндометреоз": "эндометриоз",
    "аденомеоз": "аденомиоз",
    "холецистолитаз": "холецистолитиаз",
}
TYPO_RE = re.compile("|".join(sorted(map(re.escape, TYPO_FIXES), key=len, reverse=True)))


@dataclass
class NormalizedText:
    original: str
    text: str
    index: list[int]                                   # позиция в text -> позиция в original
    corrections: list[tuple[int, int, str]] = field(default_factory=list)  # (start, end, вид) в text

    def to_original(self, start: int, end: int) -> tuple[int, int]:
        """Границы совпадения в нормализованном тексте -> границы в исходном."""
        if not self.text:
            return 0, 0
        o_start = self.index[min(start, len(self.index) - 1)]
        o_end = self.index[min(max(end - 1, start), len(self.index) - 1)] + 1 if end > start else o_start
        return o_start, o_end

    def corrected(self, start: int, end: int) -> bool:
        """Попало ли совпадение на исправленную опечатку или раскладку."""
        return any(c_start < end and start < c_end for c_start, c_end, _kind in self.corrections)


def _map_char(ch: str) -> str:
    if ch in SPACE_CHARS:
        return " "
    if ch in DROP_CHARS:
        return ""
    if ch in DASHES:
        return "-"
    if ch in QUOTES:
        return '"'
    if ch in "ёЁ":
        return "е"
    return ch.lower()


def normalize_text(original: str) -> NormalizedText:
    chars: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(original or ""):
        for out in _map_char(ch):
            if out == " " and chars and chars[-1] == " ":
                continue  # серия пробелов -> один
            chars.append(out)
            index.append(i)
    text = "".join(chars)
    corrections: list[tuple[int, int, str]] = []

    # Смешанная раскладка: слово (с дефисами) приводится к алфавиту, которого в нём больше.
    fixed = list(text)
    for m in TOKEN_RE.finditer(text):
        token = m.group(0)
        lat, cyr = len(LATIN.findall(token)), len(CYRILLIC.findall(token))
        if not lat or not cyr or lat == cyr:
            continue
        table = LAT_TO_CYR if cyr > lat else CYR_TO_LAT
        changed = False
        for k, ch in enumerate(token):
            if ch in table:
                fixed[m.start() + k] = table[ch]
                changed = True
        if changed:
            corrections.append((m.start(), m.end(), "раскладка"))
    text = "".join(fixed)

    # Опечатки: длина замены может отличаться — карта позиций пересчитывается.
    if TYPO_RE.search(text):
        out_chars: list[str] = []
        out_index: list[int] = []
        shifted: list[tuple[int, int, str]] = []
        pos = 0
        for m in TYPO_RE.finditer(text):
            out_chars.extend(text[pos:m.start()])
            out_index.extend(index[pos:m.start()])
            replacement = TYPO_FIXES[m.group(0)]
            start = len(out_chars)
            for k, ch in enumerate(replacement):
                out_chars.append(ch)
                out_index.append(index[min(m.start() + k, m.end() - 1)])
            shifted.append((start, len(out_chars), "опечатка"))
            pos = m.end()
        out_chars.extend(text[pos:])
        out_index.extend(index[pos:])
        # Исправления раскладки сдвигаются вместе с текстом.
        old_to_new = _offset_map(text, TYPO_RE)
        corrections = [(old_to_new(s), old_to_new(e), kind) for s, e, kind in corrections] + shifted
        text, index = "".join(out_chars), out_index
    return NormalizedText(original=original or "", text=text, index=index, corrections=corrections)


def _offset_map(text: str, pattern: re.Pattern):
    """Позиция до замены опечаток -> позиция после (для сдвига ранее найденных исправлений)."""
    deltas = []
    shift = 0
    for m in pattern.finditer(text):
        shift += len(TYPO_FIXES[m.group(0)]) - (m.end() - m.start())
        deltas.append((m.end(), shift))

    def convert(pos: int) -> int:
        current = 0
        for end, delta in deltas:
            if pos >= end:
                current = delta
        return pos + current

    return convert


def normalize_pattern(pattern: str) -> str:
    """Шаблон словаря приводится к тем же правилам, что и текст («ё», тире)."""
    out = pattern.replace("ё", "е").replace("Ё", "Е")
    for dash in DASHES:
        out = out.replace(dash, "-")
    return out


@lru_cache(maxsize=4096)
def _compile(pattern: str, flags: int) -> re.Pattern:
    return re.compile(normalize_pattern(pattern), flags | re.IGNORECASE)


def normalized(pattern: re.Pattern) -> re.Pattern:
    return _compile(pattern.pattern, pattern.flags & ~re.UNICODE)


def finditer(pattern: re.Pattern, norm: NormalizedText):
    """Совпадения шаблона в нормализованном тексте: (start, end) в исходном, совпадение, исправлено ли."""
    for m in normalized(pattern).finditer(norm.text):
        if m.end() == m.start():
            continue
        start, end = norm.to_original(m.start(), m.end())
        yield start, end, m, norm.corrected(m.start(), m.end())


def search(pattern: re.Pattern, norm: NormalizedText) -> bool:
    return bool(normalized(pattern).search(norm.text))
