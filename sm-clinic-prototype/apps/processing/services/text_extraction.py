"""
Извлечение текста из файлов протоколов (Strategy + Registry, принцип открытости/закрытости:
новый формат = новый класс, существующий код не меняется).

Особенность протоколов 1С: они выгружаются в .docx как таблица с объединёнными ячейками,
python-docx возвращает одну и ту же ячейку многократно — дубли схлопываем.
"""
import json
import re
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from io import BytesIO
from pathlib import Path


@dataclass
class ExtractedText:
    text: str
    study_type: str = ""
    study_date: date | None = None
    performed_by: str = ""
    card_number: str = ""
    meta: dict = field(default_factory=dict)


class TextExtractionError(Exception):
    pass


class DocumentTextExtractor(ABC):
    extensions: tuple[str, ...] = ()

    @abstractmethod
    def extract(self, data: bytes) -> ExtractedText: ...


class DocxTextExtractor(DocumentTextExtractor):
    extensions = (".docx",)

    def extract(self, data: bytes) -> ExtractedText:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        try:
            document = Document(BytesIO(data))
        except Exception as exc:
            # Текст ошибки видит координатор: по-русски и без технических подробностей (они — в журнале).
            raise TextExtractionError("Не удалось открыть .docx: файл повреждён или это не документ Word") from exc

        lines: list[str] = []
        # Обходим тело документа в исходном порядке: абзацы и таблицы вперемешку.
        for child in document.element.body.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                text = Paragraph(child, document).text.strip()
                if text:
                    lines.append(text)
            elif tag == "tbl":
                lines.extend(self._table_lines(Table(child, document)))
        return HeaderParser.enrich(ExtractedText(text="\n".join(lines)))

    @staticmethod
    def _table_lines(table) -> list[str]:
        result: list[str] = []
        for row in table.rows:
            cells: list[str] = []
            for cell in row.cells:
                value = cell.text.strip()
                # объединённая ячейка возвращается несколько раз подряд — оставляем одну
                if value and (not cells or cells[-1] != value):
                    cells.append(value)
            if cells:
                result.append(" | ".join(cells))
        return result


class DocTextExtractor(DocumentTextExtractor):
    """.doc (Word 97-2003): конвертация через LibreOffice в .docx, иначе — antiword."""

    extensions = (".doc",)

    def extract(self, data: bytes) -> ExtractedText:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "protocol.doc"
            src.write_bytes(data)
            soffice = shutil.which("soffice") or shutil.which("libreoffice")
            if soffice:
                subprocess.run(
                    [soffice, "--headless", "--convert-to", "docx", "--outdir", tmp, str(src)],
                    check=False, capture_output=True, timeout=120,
                )
                converted = Path(tmp) / "protocol.docx"
                if converted.exists():
                    return DocxTextExtractor().extract(converted.read_bytes())
            antiword = shutil.which("antiword")
            if antiword:
                out = subprocess.run([antiword, str(src)], capture_output=True, timeout=60)
                if out.returncode == 0:
                    return HeaderParser.enrich(ExtractedText(text=out.stdout.decode("utf-8", "ignore")))
        raise TextExtractionError("Для .doc нужен LibreOffice (soffice) или antiword")


class JsonTextExtractor(DocumentTextExtractor):
    """Протокол, пришедший событием из МИС в JSON: {"text": "...", "study_type": "...", "study_date": "YYYY-MM-DD"}."""

    extensions = (".json",)

    def extract(self, data: bytes) -> ExtractedText:
        try:
            body = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TextExtractionError(f"Некорректный JSON: {exc}") from exc
        text = body.get("text") or "\n".join(filter(None, [body.get("description", ""), body.get("conclusion", "")]))
        study_date = body.get("study_date")
        result = ExtractedText(
            text=text,
            study_type=body.get("study_type", ""),
            study_date=date.fromisoformat(study_date) if study_date else None,
            performed_by=body.get("performed_by", ""),
            card_number=str(body.get("card_number") or body.get("patient_mis_id") or body.get("external_mis_id") or "")[:64],
            meta={k: v for k, v in body.items() if k not in {"text"}},
        )
        return HeaderParser.enrich(result)


class HeaderParser:
    """Достаёт из шапки протокола тип исследования, дату и врача (шаблон 1С)."""

    DATE_RE = re.compile(r"Дата приема:\s*\|?\s*(\d{2}\.\d{2}\.\d{4})")
    DOCTOR_RE = re.compile(r"Врач:\s*\|?\s*([^\n|]+)")
    # Номер карты в шапке 1С: «Амбулаторная карта № | 12345», «Номер карты: | 12345», «Карта № 12345».
    # Значение — в той же строке (ячейке таблицы): пустая ячейка не должна «захватить» следующую строку шапки.
    CARD_RE = re.compile(r"(?:карт\w*[ \t]*№|Номер[ \t]+карты[ \t]*:)[ \t]*\|?[ \t]*([^\n|]{1,64})", re.IGNORECASE)
    TITLE_RE = re.compile(r"^(.*(ИССЛЕДОВАНИЕ|ПРОТОКОЛ)|\s*(УЗИ|УЗДС|ТРУЗИ|ТВУ?ЗИ|ЦДК|ДУПЛЕКСН\w*|ТРИПЛЕКСН\w*|МРТ|КТ)\b)[^\n]*$",
                          re.IGNORECASE | re.MULTILINE)

    @classmethod
    def enrich(cls, extracted: ExtractedText) -> ExtractedText:
        text = extracted.text
        if not extracted.study_date and (m := cls.DATE_RE.search(text)):
            extracted.study_date = datetime.strptime(m.group(1), "%d.%m.%Y").date()
        if not extracted.performed_by and (m := cls.DOCTOR_RE.search(text)):
            extracted.performed_by = m.group(1).strip()
        if not extracted.card_number and (m := cls.CARD_RE.search(text)):
            value = " ".join(m.group(1).split())[:64]
            if value and not value.endswith(":"):  # «Дата приема:» — это подпись соседнего поля, а не номер
                extracted.card_number = value
        if not extracted.study_type:
            for m in cls.TITLE_RE.finditer(text):
                line = m.group(0).strip()
                lowered = line.lower()
                # Название исследования, а не строка про аппарат/датчик («Исследование выполнено на аппарате …»).
                if ("исследовани" in lowered or STUDY_PREFIX_RE.match(lowered)) and len(line) < 160 \
                        and not re.search(r"аппарат|датчик|сканер|выполнен|проводил|проведено|диагноз", lowered):
                    title = " ".join(line.split())
                    title = title.capitalize() if title.isupper() else title[:1].upper() + title[1:]
                    extracted.study_type = re.sub(r"^(Узи|Уздс|Трузи|Тву?зи|Цдк|Мрт|Кт)\b", lambda x: x.group(1).upper(), title)
                    break
        return extracted


# Начало строки-названия исследования: УЗИ, УЗДС, ТРУЗИ, ТВУЗИ, ЦДК, дуплексное сканирование, МРТ, КТ.
STUDY_PREFIX_RE = re.compile(r"(узи|уздс|трузи|тву?зи|цдк|дуплексн\w*|триплексн\w*|мрт|кт)\b")


_REGISTRY: dict[str, DocumentTextExtractor] = {}


def register(extractor: DocumentTextExtractor) -> None:
    for ext in extractor.extensions:
        _REGISTRY[ext] = extractor


for _extractor in (DocxTextExtractor(), DocTextExtractor(), JsonTextExtractor()):
    register(_extractor)


def get_extractor(filename: str) -> DocumentTextExtractor:
    ext = Path(filename).suffix.lower()
    if ext not in _REGISTRY:
        raise TextExtractionError(f"Формат {ext or '(без расширения)'} не поддерживается")
    return _REGISTRY[ext]


# Номер карты в имени файла: «AK-0001_УЗИ.docx», «карта 12345.docx», «12345678_omt.docx».
FILENAME_CARD_RE = re.compile(r"^(?:карта[\s_-]*)?([A-ZА-Я]{1,4}-\d{3,}|\d{5,})(?=[\s_.-]|$)", re.IGNORECASE)


def card_number_from_filename(filename: str) -> str:
    m = FILENAME_CARD_RE.match(Path(filename).stem)
    return m.group(1).upper() if m else ""
