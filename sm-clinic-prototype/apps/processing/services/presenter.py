"""
Представление разметки для врача — без системного ранжирования.

Порядок по умолчанию — порядок самого протокола:
  «Заключение»            — пункты заключения карточками вместе с деталями из описания (размер, количество);
  «Изменения в описании»  — то, что не связано с пунктом заключения, сгруппировано по органам;
  затем рекомендации, неклассифицированное, норма и параметры, служебные строки.

Сверху — «В фокусе»: карточки, которые врач закрепил в этом протоколе, и карточки, совпавшие
с его личными приоритетами (FocusProfile) или с фильтрами, выбранными для этого случая.
Порядок фокуса задаёт врач, система только сопоставляет коды.

Экстренная находка дополнительно выводится предупреждением со ссылкой на фрагмент: это требование
безопасности (экстренное нельзя пропустить), а не оценка важности; карточка остаётся на своём месте.

Инвариант: каждый фрагмент протокола выводится ровно один раз — ничего не теряется и не дублируется.
"""
from .annotation import HIGHLIGHT_TYPES, SIGN_TITLES

CARD_KINDS = ("finding", "scale", "abnormal", "conclusion_item")
SCALE_TITLES = {"birads_category": "BI-RADS", "tirads_category": "TI-RADS", "orads_category": "O-RADS"}
SECTION_TITLES = {"header": "Шапка", "description": "Описание", "conclusion": "Заключение",
                  "recommendation": "Рекомендации", "tail": "После заключения"}


class PresentationError(Exception):
    pass


def focus_key_title(key: str, finding_titles: dict[str, str]) -> str:
    """Человекочитаемое название ключа фокуса: находка, признак или причина подсветки."""
    if key.startswith("sign:"):
        return SIGN_TITLES.get(key[5:], key[5:])
    if key.startswith("hl:"):
        return HIGHLIGHT_TYPES.get(key[3:], key[3:])
    return finding_titles.get(key) or SCALE_TITLES.get(key) or key


def segment_keys(segment: dict) -> list[str]:
    keys = list(segment.get("finding_codes", []))
    keys += [f"sign:{s}" for s in segment.get("signs", []) if s != "us_signs"]
    keys += [f"hl:{t}" for t in segment.get("highlight_types", [])]
    return keys


def _titles(segment: dict, finding_titles: dict[str, str]) -> list[str]:
    titles = [finding_titles.get(c) or SCALE_TITLES.get(c) or c for c in segment.get("finding_codes", [])]
    if not titles:
        titles = [SIGN_TITLES.get(s, s) for s in segment.get("signs", []) if s != "us_signs"]
    return list(dict.fromkeys(titles))


def build_doctor_view(segments: list[dict], finding_titles: dict[str, str] | None = None, *,
                      focus: list[str] | None = None, pinned: set[int] | None = None) -> dict:
    finding_titles = finding_titles or {}
    focus = list(dict.fromkeys(focus or []))
    pinned = set(pinned or ())

    anchors = [s for s in segments if s["section"] == "conclusion" and s["kind"] in CARD_KINDS and s.get("linked_to") is None]
    conclusion_cards = []
    for anchor in anchors:
        # Сначала дополнения из заключения (шкала риска), затем детали описания — в порядке протокола.
        details = sorted((s for s in segments if s.get("linked_to") == anchor["id"]),
                         key=lambda s: (s["section"] != "conclusion", s["id"]))
        conclusion_cards.append(_card(anchor, details, finding_titles))
    # Изменения описания, не привязанные к заключению, — отдельные карточки.
    description_cards = [_card(s, [], finding_titles) for s in segments
                         if s["section"] != "conclusion" and s["kind"] in CARD_KINDS and s.get("linked_to") is None]

    for card in conclusion_cards + description_cards:
        member_ids = {card["anchor"]["id"], *(d["id"] for d in card["details"])}
        if member_ids & pinned:
            card["focus_index"], card["focus_reason"], card["pinned"] = -1, "Закреплено", True
            continue
        for index, key in enumerate(focus):
            if key in card["keys"]:
                card["focus_index"], card["focus_reason"] = index, focus_key_title(key, finding_titles)
                break
    focus_cards = sorted((c for c in conclusion_cards + description_cards if c["focus_index"] is not None),
                         key=lambda c: (c["focus_index"], c["anchor"]["id"]))
    conclusion_cards = [c for c in conclusion_cards if c["focus_index"] is None]
    description_groups: dict[str, list] = {}
    for card in description_cards:
        if card["focus_index"] is None:
            description_groups.setdefault(card["anchor"].get("organ") or "Без указания органа", []).append(card)

    used = [i for c in focus_cards + conclusion_cards + [c for cards in description_groups.values() for c in cards]
            for i in [c["anchor"]["id"]] + [d["id"] for d in c["details"]]]
    rest = [s for s in segments if s["id"] not in set(used)]
    groups = {
        "recommendations": [s for s in rest if s["kind"] == "recommendation"],
        "unclassified": [s for s in rest if s["kind"] == "unclassified"],
        "norm": [s for s in rest if s["kind"] in ("norm", "measurement")],
        "service": [s for s in rest if s["kind"] in ("meta", "technical", "heading", "disclaimer")],
    }
    # Детали, привязанные к пункту, который сам не карточка (на всякий случай), — тоже не теряем.
    grouped = {s["id"] for items in groups.values() for s in items}
    groups["other"] = [s for s in rest if s["id"] not in grouped]
    for items in groups.values():
        items.sort(key=lambda s: s["id"])

    shown = used + [s["id"] for items in groups.values() for s in items]
    if sorted(shown) != sorted(s["id"] for s in segments):
        raise PresentationError("Нарушен инвариант: фрагмент протокола потерян или выведен дважды")

    return {
        "emergency": [{"id": s["id"], "text": s["text"]} for s in segments if s.get("emergency")],
        "focus": {"cards": focus_cards, "keys": [{"key": k, "title": focus_key_title(k, finding_titles)} for k in focus]},
        "conclusion": conclusion_cards,
        "description_groups": [{"organ": organ, "cards": cards} for organ, cards in description_groups.items()],
        "recommendations": groups["recommendations"],
        "unclassified": groups["unclassified"] + groups["other"],
        "norm": groups["norm"],
        "service": groups["service"],
        "options": _focus_options(segments, finding_titles, focus),
        "total": len(segments),
        "shown": len(shown),
    }


def _focus_options(segments: list[dict], finding_titles: dict[str, str], focus: list[str]) -> list[dict]:
    """Что есть в этом протоколе и можно вынести в фокус: находки, признаки, причины подсветки."""
    counts: dict[str, int] = {}
    for s in segments:
        if s["kind"] not in CARD_KINDS:
            continue
        for key in dict.fromkeys(segment_keys(s)):
            counts[key] = counts.get(key, 0) + 1
    order = {"": 0, "sign": 1, "hl": 2}
    return [{"key": k, "title": focus_key_title(k, finding_titles), "count": n, "active": k in focus,
             "group": k.split(":")[0] if ":" in k else "finding"}
            for k, n in sorted(counts.items(), key=lambda kv: (order.get(kv[0].split(":")[0] if ":" in kv[0] else "", 0),
                                                               focus_key_title(kv[0], finding_titles)))]


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]


def _card(anchor: dict, details: list[dict], finding_titles: dict[str, str]) -> dict:
    members = [anchor] + details
    highlights, seen = [], set()
    for m in members:
        for h in m.get("highlights", []):
            if h["text"] not in seen:
                seen.add(h["text"])
                highlights.append(h)
    titles = list(dict.fromkeys(t for m in members for t in _titles(m, finding_titles)))
    return {
        "title": _capitalize(", ".join(titles)) or "Изменение без кода словаря",
        "anchor": anchor, "details": details, "highlights": highlights,
        "types": list(dict.fromkeys(h["type"] for h in highlights)),
        "keys": {k for m in members for k in segment_keys(m)},
        "emergency": any(m.get("emergency") for m in members),
        "from_conclusion": anchor["section"] == "conclusion",
        "not_in_conclusion": anchor.get("not_in_conclusion", False),
        "focus_index": None, "focus_reason": "", "pinned": False,
    }


def evidence_html(text: str, findings: list[dict]) -> str:
    """Исходный текст протокола с подсветкой цитат-доказательств (объяснимость: что именно вызвало находку)."""
    from django.utils.html import escape

    spans = sorted((f["span_start"], f["span_end"], f.get("negated"), f.get("label", "")) for f in findings
                   if f.get("span_start") is not None and f.get("span_end") is not None)
    out, pos = [], 0
    for start, end, negated, label in spans:
        if start < pos:
            continue
        out.append(escape(text[pos:start]))
        cls = ' class="neg"' if negated else ""
        title = f"{label} — отрицание, не триггер" if negated else label
        out.append(f'<mark{cls} title="{escape(title)}">{escape(text[start:end])}</mark>')
        pos = end
    out.append(escape(text[pos:]))
    return "".join(out)


# ------------------------------------------------------------------ многослойная подсветка (2.1–2.3)
def layered_evidence_html(text: str, analysis: dict) -> str:
    """Исходный текст с подсветкой всех маркеров и триггеров без потерь при перекрытиях.

    Текст режется на непересекающиеся кусочки по всем границам маркеров и триггеров; у каждого кусочка
    список того, что его покрывает (data-m, data-t, data-types). Поэтому вложенные и пересекающиеся
    маркеры не «съедают» друг друга: фон берётся у самого внутреннего маркера, а каждый слой рисует
    своё подчёркивание. Триггер сильнее маркера: жирный текст, толстая линия под строкой и номер «Т1»
    перед первой цитатой. Номер выводится через CSS (::before), поэтому текст внутри блока совпадает
    с протоколом символ в символ (проверяется тестом).
    Стили здесь — режим «все маркеры»; режимы «только триггеры», «только найденное ИИ» и «только выбранный
    тип» переключает CSS (evidence_css) без перерисовки текста. Найденное только ИИ-агентом (source=llm)
    обведено пунктиром и помечено «ИИ» (тоже через CSS, текст не меняется)."""
    from django.utils.html import escape

    from .markers import PALETTE, TRIGGER_PALETTE

    markers = [m for m in analysis.get("markers") or [] if 0 <= m["start"] < m["end"] <= len(text)]
    spans = []  # (trigger, start, end, главная ли цитата)
    for t in analysis.get("triggers") or []:
        for i, (s, e) in enumerate([(t["start"], t["end"])] + [(a["start"], a["end"]) for a in t.get("also") or []]):
            if 0 <= s < e <= len(text):
                spans.append((t, s, e, i == 0))
    bounds = sorted({0, len(text)} | {m["start"] for m in markers} | {m["end"] for m in markers}
                    | {s for _t, s, _e, _main in spans} | {e for _t, _s, e, _main in spans})
    out, badged = [], set()
    ai_starts = {m["start"] for m in markers if m.get("source") == "llm"}
    for a, b in zip(bounds, bounds[1:]):
        piece = escape(text[a:b])
        active = sorted((m for m in markers if m["start"] <= a and b <= m["end"]), key=lambda m: m["layer"])
        trig = [(t, s, main) for t, s, e, main in spans if s <= a and b <= e]
        if not active and not trig:
            out.append(piece)
            continue
        types = list(dict.fromkeys(m["type"] for m in active))
        classes = ["ev"] + [f"has-{t}" for t in types]
        attrs = [f'data-m="{" ".join(m["id"] for m in active)}"', f'data-types="{" ".join(types)}"']
        if any(m.get("source") == "llm" for m in active) or any(t.get("source") == "llm" for t, _s, _m in trig):
            classes.append("ev-ai")
            if a in ai_starts:
                attrs.append('data-ai="ИИ"')
        style = []
        if active:
            inner = min(active, key=lambda m: (m["end"] - m["start"], -m["layer"]))
            style.append(f"background:{PALETTE[inner['type']][0]}")
            lines = [m for m in active if m["type"] != "negation"][:4]
            shadows = [f"inset 0 -{2 * (i + 1)}px 0 {PALETTE[m['type']][1]}" for i, m in enumerate(reversed(lines))]
            if any(m["type"] == "negation" for m in active):
                style.append(f"text-decoration:underline dotted {PALETTE['negation'][1]} 2px")
        else:
            shadows = []
        if trig:
            t0 = trig[0][0]
            classes += ["ev-trig", f"ev-trig-{t0['type']}"]
            attrs.append(f'data-t="{" ".join(dict.fromkeys(t["id"] for t, _s, _m in trig))}"')
            shadows.append(f"0 3px 0 0 {TRIGGER_PALETTE[t0['type']]}")
            for t, s, main in trig:
                if s == a and (t["id"], s) not in badged:
                    badged.add((t["id"], s))
                    attrs.append(f'data-badge="{escape(t["number"])}" data-tstart="{t["id"]}"')
                    break
        if shadows:
            style.append("box-shadow:" + ",".join(shadows))
        out.append(f'<span class="{" ".join(classes)}" {" ".join(attrs)} style="{";".join(style)}">{piece}</span>')
    return "".join(out)


def evidence_css() -> str:
    """Стили режимов подсветки и легенды из той же палитры, что и сервер (markers.PALETTE)."""
    from .markers import AI_COLOR, PALETTE, TRIGGER_PALETTE

    rules = []
    for kind, (bg, line) in PALETTE.items():
        rules.append(f".lg-{kind}{{background:{bg};box-shadow:inset 0 -2px 0 {line}}}")
        rules.append(f'.ev-wrap[data-mode="type"][data-type="{kind}"] .ev.has-{kind}'
                     f'{{background:{bg}!important;box-shadow:inset 0 -3px 0 {line}!important}}')
    rules.append(f".ev.ev-ai{{outline:2px dashed {AI_COLOR};outline-offset:1px}}")
    rules.append(f".ev[data-ai]::after{{background:{AI_COLOR}}}")
    rules.append(f".lg-ai{{background:#fff;outline:2px dashed {AI_COLOR};outline-offset:-2px}}")
    rules.append(f'.ev-wrap[data-mode="ai"] .ev.ev-ai{{background:#EEF0FF!important;box-shadow:inset 0 -3px 0 {AI_COLOR}!important}}')
    for kind, color in TRIGGER_PALETTE.items():
        rules.append(f".ev-wrap[data-mode=\"triggers\"] .ev-trig-{kind}{{box-shadow:0 3px 0 0 {color},inset 0 -1.2em 0 {color}22!important}}")
        rules.append(f".ev-trig-{kind}[data-badge]::before{{background:{color}}}")
        rules.append(f".tg-{kind}{{border-left:4px solid {color}}}")
    return "\n".join(rules)
