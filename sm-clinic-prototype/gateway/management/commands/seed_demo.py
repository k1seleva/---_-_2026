"""
Начальные настройки и демо-данные: словарь находок, матрица маршрутизации, шаблоны маршрутов,
лестницы эскалаций, тексты уведомлений, врачи и расписание.

Всё это — НАСТРОЙКИ (редактируются в /admin), а не код: новый триггер или тип исследования
добавляется записью в БД (п. 11 кейса).

    python manage.py seed_demo            # настройки + демо-врачи + демо-пациенты
    python manage.py seed_demo --no-demo  # только настройки
"""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.doctors.models import ClinicLocation, Doctor, Specialty
from apps.doctors.services.booking import ScheduleService
from apps.patients.models import NotificationTemplate, Patient
from apps.patients.services.auth import PatientAuthService
from apps.audit.models import IndicationRule
from apps.processing.models import AttentionRule, FindingDefinition, SpecialtyAlias
from apps.routing.models import EscalationPolicy, RouteTemplate, RouteTemplateStep, TriggerRule
from apps.coordinator.models import Tag
from apps.coordinator.services.cases import ensure_system_tags
from gateway.staff_auth import StaffAuthService

from .import_run_summary import import_summary

# Свои метки координатора (пример): создаются и редактируются в «Настройки → Метки».
CUSTOM_TAGS = [("callback", "Перезвонить", "amber"), ("interpreter", "Нужен переводчик", "violet"),
               ("second_opinion", "Второе мнение", "gray"), ("vip", "Особое внимание", "blue")]

# ------------------------------------------------------------------ словарь находок
# (code, title, patterns, exclude, severity)
FINDINGS = [
    ("endometrial_polyp", "Полип эндометрия", [r"полип\w*\s+(в\s+полости\s+матки|эндометри)", r"полипоз\w*\s+эндометри", r"патологи\w*\s+эндометри\w*\s*\(\s*полип"], [], "urgent"),
    ("endometrial_hyperplasia", "Гиперплазия эндометрия", [r"гиперплази\w*\s+эндометри"], [], "urgent"),
    ("submucous_myoma", "Субмукозный миоматозный узел", [r"субмукозн\w*", r"FIGO\s*[0-2]\b", r"деформаци\w*\s+полости\s+матки"], [], "urgent"),
    ("uterine_myoma", "Миома матки", [r"миом\w*\s+матки", r"\bмиома\b", r"лейомиом\w*"], [r"без\s+признаков\s+роста"], "routine"),
    ("ovarian_mass", "Образование яичника", [r"образовани\w*\s+(правого\s+|левого\s+)?яичник", r"кист\w*\s+(правого\s+|левого\s+)?яичник", r"параовариальн\w*\s+кист", r"гидросальпинкс\w*"], [r"желт\w*\s+тел"], "routine"),
    ("breast_mass", "Образование молочной железы", [r"фиброаденом\w*", r"очагов\w*\s+образовани\w*\s+(правой\s+|левой\s+)?молочн", r"участ\w*\s+.{0,40}неровными\s+контурами"], [], "routine"),
    ("gallstones", "Желчнокаменная болезнь", [r"холецистолитиаз\w*", r"калькулезн\w*\s+холецистит", r"конкремент\w*.{0,40}желчн", r"желчнокаменн\w*", r"\bЖКБ\b"], [], "urgent"),
    ("gallbladder_polyp", "Полип желчного пузыря", [r"полип\w*\s+(в\s+)?желчн", r"полипоз\w*.{0,20}желчн", r"образовани\w*\s+в\s+желчном\s+пузыре", r"полипа\s+желчного"], [], "routine"),
    ("thyroid_nodule", "Узловое образование щитовидной железы", [r"узл\w*.{0,40}(щитовидн|доли)", r"узлов\w*\s+(образовани|зоб)", r"узлового\s+образования"], [], "routine"),
    ("hernia", "Грыжа", [r"грыж\w*"], [], "routine"),
    ("hydronephrosis", "Гидронефроз / конкременты почек", [r"гидронефроз\w*", r"конкремент\w*.{0,30}(почк|мочеточ)", r"(нефро|уро)литиаз\w*", r"пиелоэктази\w*"], [], "urgent"),
    ("bph", "Гиперплазия предстательной железы", [r"\bДГПЖ\b", r"гиперплази\w*.{0,40}предстательн", r"аденом\w*\s+простаты", r"гиперплазии\s+переходн"], [], "routine"),
    ("varicose_veins", "Варикозная болезнь вен нижних конечностей", [r"варикозн\w*\s+(трансформац|расширени)", r"несостоятельност\w*.{0,20}(БПВ|МПВ|перфорант|ствола)"], [r"малого\s+таза"], "routine"),
    ("stenotic_atherosclerosis", "Стенозирующий атеросклероз артерий", [r"(?<!не)стенозирующ\w*\s+атеросклероз", r"гемодинамически\s+значим\w*\s+атеросклероз"], [r"\bне\s*стенозирующ"], "routine"),
    ("arterial_stenosis", "Стеноз артерий (с указанием %)", [r"стеноз\w*", r"\bАС\s+бляшк\w*", r"атеросклероз\w*"], [], "routine"),
    ("dvt", "Тромбоз глубоких вен", [r"тромбоз\w*"], [], "emergency"),
]

# Тексты для пациента: что это простыми словами. Без вероятностей, прогнозов и советов по лечению —
# только «что увидели» и «кто решит, что делать». В эксплуатации их утверждает врач-эксперт.
# Экстренные находки пациенту не объясняются: с ним связывается врач.
PATIENT_TEXTS = {
    "endometrial_polyp": ("Полип эндометрия", "Полип — небольшое разрастание внутреннего слоя матки. Что с ним делать, решает гинеколог после консультации."),
    "endometrial_hyperplasia": ("Утолщение внутреннего слоя матки", "Внутренний слой матки толще обычного. Гинеколог объяснит причину и предложит план."),
    "submucous_myoma": ("Миоматозный узел у полости матки", "Узел мышечной ткани находится рядом с полостью матки. Тактику определит гинеколог."),
    "uterine_myoma": ("Миома матки", "Миома — узел из мышечной ткани матки. Нужно ли что-то делать, решает гинеколог на консультации."),
    "ovarian_mass": ("Образование в яичнике", "В яичнике видно образование (например, киста). Гинеколог оценит его и скажет, нужно ли наблюдение."),
    "breast_mass": ("Образование в молочной железе", "В ткани железы видно образование. Маммолог оценит его и скажет, какие шаги нужны."),
    "gallstones": ("Камни в желчном пузыре", "В желчном пузыре видны камни. Хирург расскажет, что это значит именно для вас и нужно ли лечение."),
    "gallbladder_polyp": ("Полип желчного пузыря", "На стенке желчного пузыря видно небольшое образование. Хирург определит, нужно ли наблюдение или лечение."),
    "thyroid_nodule": ("Узел щитовидной железы", "В щитовидной железе виден узел. Эндокринолог оценит его и скажет, нужно ли дополнительное обследование."),
    "hernia": ("Грыжа", "Описана грыжа. Хирург на консультации обсудит, нужно ли лечение."),
    "hydronephrosis": ("Изменения в почках", "Описано расширение чашечно-лоханочной системы или камни. Уролог объяснит, что это значит."),
    "bph": ("Увеличение предстательной железы", "Предстательная железа увеличена. Уролог оценит, нужна ли терапия."),
    "varicose_veins": ("Варикозное расширение вен", "Вены ног расширены, клапаны работают не полностью. Флеболог предложит варианты."),
    "stenotic_atherosclerosis": ("Сужение артерий", "В артериях есть бляшки, которые сужают просвет. Сосудистый хирург оценит, что делать."),
    "arterial_stenosis": ("Сужение артерии", "Просвет артерии сужен. Сосудистый хирург скажет, нужно ли лечение."),
}

SPECIALTY_ALIASES = [
    ("gynecologist", r"гинеколог"), ("mammologist", r"маммолог"), ("oncologist", r"онколог"),
    ("surgeon", r"\bхирург"), ("gastroenterologist", r"гастроэнтеролог"), ("endocrinologist", r"эндокринолог"),
    ("urologist", r"уролог"), ("phlebologist", r"флеболог"), ("vascular_surgeon", r"сосудист\w*\s+хирург|ангиохирург"),
    ("therapist", r"терапевт"),
]

SPECIALTIES = [
    ("gynecologist", "Гинеколог", "врача-гинеколога", False),
    ("gyn_surgeon", "Оперирующий гинеколог", "оперирующего гинеколога", True),
    ("mammologist", "Маммолог", "маммолога", False),
    ("oncologist", "Онколог", "онколога", True),
    ("surgeon", "Хирург", "хирурга", True),
    ("gastroenterologist", "Гастроэнтеролог", "гастроэнтеролога", False),
    ("endocrinologist", "Эндокринолог", "эндокринолога", False),
    ("urologist", "Уролог", "уролога", False),
    ("phlebologist", "Флеболог", "флеболога", True),
    ("vascular_surgeon", "Сосудистый хирург", "сосудистого хирурга", True),
    ("therapist", "Терапевт", "терапевта", False),
]

LOCATIONS = [
    ("vdnh", "ВДНХ", "Москва, пр-т Мира", False, True),
    ("tekstilshchiki", "Текстильщики", "Москва, Волгоградский пр-т", False, False),
    ("senezhskaya", "Сенежская", "Москва, ул. Сенежская", False, True),
    ("online", "Онлайн", "", True, False),
]

DOCTORS = [
    ("Иванова Е. А.", ["gyn_surgeon", "gynecologist"], ["vdnh", "online"], ["hysteroscopy", "hysteroresectoscopy"]),
    ("Смирнова О. В.", ["gyn_surgeon"], ["tekstilshchiki"], ["hysteroscopy"]),
    ("Кузнецова М. И.", ["gynecologist"], ["senezhskaya", "online"], []),
    ("Петрова А. С.", ["mammologist", "oncologist"], ["vdnh", "online"], ["breast_biopsy"]),
    ("Соколов Д. Н.", ["surgeon"], ["senezhskaya", "vdnh"], ["cholecystectomy", "hernia"]),
    ("Морозова Т. П.", ["gastroenterologist", "therapist"], ["tekstilshchiki", "online"], []),
    ("Волкова Н. Г.", ["endocrinologist"], ["vdnh", "online"], []),
    ("Лебедев А. В.", ["urologist"], ["tekstilshchiki"], ["turp"]),
    ("Новиков С. Ю.", ["phlebologist", "vascular_surgeon"], ["senezhskaya"], ["evla"]),
]

# ------------------------------------------------------------------ эскалации (часы от активации этапа)
POLICIES = {
    "booking_standard": ("Сценарий 2: нет записи после уведомления", [
        {"after_hours": 24, "action": "notify", "template": "reminder_24h", "channels": ["lk", "push"]},
        {"after_hours": 72, "action": "notify", "template": "reminder_72h", "channels": ["lk", "sms", "push"]},
        {"after_hours": 120, "action": "coordinator_task", "task_type": "call_patient"},
        {"after_hours": 336, "action": "notify", "template": "final_soft", "channels": ["lk", "push"]},
        {"after_hours": 720, "action": "close_not_engaged"},
    ]),
    "no_show": ("Сценарий 3: неявка к врачу", [
        {"after_hours": 0.5, "action": "notify", "template": "no_show", "channels": ["lk", "push"]},
        {"after_hours": 24, "action": "notify", "template": "reminder_24h", "channels": ["lk", "push"]},
        {"after_hours": 72, "action": "notify", "template": "reminder_72h", "channels": ["lk", "sms", "push"]},
        {"after_hours": 120, "action": "coordinator_task", "task_type": "call_patient"},
        {"after_hours": 336, "action": "notify", "template": "final_soft", "channels": ["lk", "push"]},
        {"after_hours": 720, "action": "close_not_engaged"},
    ]),
    "hospitalization_date": ("Этап 9: контроль даты госпитализации", [
        {"after_hours": 24, "action": "coordinator_task", "task_type": "hospitalization_date"},
        {"after_hours": 72, "action": "coordinator_task", "task_type": "hospitalization_date"},
        {"after_hours": 120, "action": "coordinator_task", "task_type": "head_escalation"},
    ]),
    "postop_control": ("Этап 12: контрольный визит после выписки", [
        {"after_hours": 0, "action": "coordinator_task", "task_type": "book_follow_up"},
        {"after_hours": 72, "action": "notify", "template": "reminder_24h", "channels": ["lk", "push"]},
        {"after_hours": 240, "action": "coordinator_task", "task_type": "call_patient"},
    ]),
}

# ------------------------------------------------------------------ шаблоны маршрутов
# code: (title, [(step_type, title, specialty, service, offset, window, auto_book, policy)])
TEMPLATES = {
    "gyn_surgical": ("Хирургический гинекологический маршрут", [("consultation", "Консультация оперирующего гинеколога", "gyn_surgeon", "", 0, 7, False, "booking_standard")]),
    "gyn_consult": ("Консультация гинеколога", [("consultation", "Консультация гинеколога", "gynecologist", "", 0, 14, False, "booking_standard")]),
    "breast": ("Маршрут маммолога / онколога", [("consultation", "Консультация маммолога", "mammologist", "", 0, 7, False, "booking_standard")]),
    "breast_low": ("Наблюдение маммолога", [("consultation", "Консультация маммолога", "mammologist", "", 0, 14, False, "booking_standard")]),
    "gb_surgical": ("Хирургический маршрут (желчный пузырь)", [("consultation", "Консультация хирурга", "surgeon", "", 0, 7, False, "booking_standard")]),
    "gb_polyp": ("Полип желчного пузыря", [("consultation", "Консультация хирурга", "surgeon", "", 0, 14, False, "booking_standard"),
                                         ("diagnostics", "Контрольное УЗИ желчного пузыря", "", "uzi_gb", 180, 30, False, "booking_standard")]),
    "thyroid": ("Узловое образование щитовидной железы", [("consultation", "Консультация эндокринолога", "endocrinologist", "", 0, 14, False, "booking_standard")]),
    "thyroid_biopsy": ("TI-RADS 4-5: эндокринолог и ТАБ", [("consultation", "Консультация эндокринолога", "endocrinologist", "", 0, 7, False, "booking_standard"),
                                                           ("diagnostics", "Тонкоигольная аспирационная биопсия (ТАБ)", "", "tab", 0, 14, False, "booking_standard")]),
    "surgery_general": ("Консультация хирурга", [("consultation", "Консультация хирурга", "surgeon", "", 0, 14, False, "booking_standard")]),
    "urology": ("Урологический маршрут", [("consultation", "Консультация уролога", "urologist", "", 0, 14, False, "booking_standard")]),
    "phlebology": ("Флебологический маршрут", [("consultation", "Консультация флеболога", "phlebologist", "", 0, 14, False, "booking_standard")]),
    "vascular": ("Сосудистый хирург", [("consultation", "Консультация сосудистого хирурга", "vascular_surgeon", "", 0, 7, False, "booking_standard")]),
    "emergency": ("Экстренная находка", [("consultation", "Срочная консультация", "vascular_surgeon", "", 0, 1, False, None)]),
    "postop_control": ("Послеоперационное наблюдение", [("follow_up", "Контрольный визит после лечения (день 7 ± 2)", "", "", 5, 4, True, "postop_control")]),
}

# ------------------------------------------------------------------ матрица маршрутизации
# (code, title, finding, conditions, template, first_specialty, potential_route, target_days, surgical, emergency, priority)
RULES = [
    ("endometrial_polyp", "Полип эндометрия", "endometrial_polyp", [], "gyn_surgical", "gyn_surgeon", "Гистероскопия, гистерорезектоскопия, РДВ", 7, True, False, 10),
    ("endometrial_hyperplasia", "Гиперплазия эндометрия", "endometrial_hyperplasia", [], "gyn_surgical", "gyn_surgeon", "Гистероскопия, РДВ", 7, True, False, 10),
    ("submucous_myoma", "Субмукозная миома матки", "submucous_myoma", [], "gyn_surgical", "gyn_surgeon", "Гистерорезектоскопия", 7, True, False, 10),
    ("uterine_myoma", "Миома матки", "uterine_myoma", [], "gyn_consult", "gynecologist", "Решение о тактике", 14, False, False, 50),
    ("ovarian_mass_orads", "Образование яичника O-RADS 3-5", "orads_category", [{"attr": "orads", "op": "gte", "value": 3}], "gyn_surgical", "gyn_surgeon", "Лапароскопия", 7, True, False, 15),
    ("ovarian_mass", "Образование яичника", "ovarian_mass", [], "gyn_consult", "gynecologist", "УЗИ в динамике / лапароскопия", 14, False, False, 40),
    ("birads_3_5", "Образование молочной железы BI-RADS 3-5", "birads_category", [{"attr": "birads", "op": "gte", "value": 3}], "breast", "mammologist", "Биопсия / дальнейший маршрут", 7, True, False, 10),
    ("breast_mass", "Образование молочной железы", "breast_mass", [], "breast_low", "mammologist", "Решение о биопсии", 14, False, False, 30),
    ("gallstones", "Желчнокаменная болезнь", "gallstones", [], "gb_surgical", "surgeon", "Лапароскопическая холецистэктомия", 7, True, False, 10),
    ("gallbladder_polyp", "Полип желчного пузыря", "gallbladder_polyp", [], "gb_polyp", "surgeon", "Холецистэктомия при полипе ≥ 10 мм", 14, True, False, 20),
    ("thyroid_tirads_4_5", "Узел щитовидной железы TI-RADS 4-5", "tirads_category", [{"attr": "tirads", "op": "gte", "value": 4}], "thyroid_biopsy", "endocrinologist", "ТАБ", 7, False, False, 10),
    ("thyroid_nodule", "Узловое образование щитовидной железы", "thyroid_nodule", [], "thyroid", "endocrinologist", "Наблюдение / ТАБ по показаниям", 14, False, False, 30),
    ("hernia", "Грыжа", "hernia", [], "surgery_general", "surgeon", "Герниопластика", 14, True, False, 20),
    ("hydronephrosis", "Гидронефроз / конкременты", "hydronephrosis", [], "urology", "urologist", "Литотрипсия / стентирование", 7, True, False, 15),
    ("bph", "Гиперплазия предстательной железы", "bph", [], "urology", "urologist", "ТУР / медикаментозная терапия", 14, False, False, 40),
    ("varicose_veins", "Варикозная болезнь", "varicose_veins", [], "phlebology", "phlebologist", "ЭВЛК / склеротерапия", 14, True, False, 30),
    ("stenotic_atherosclerosis", "Стенозирующий атеросклероз", "stenotic_atherosclerosis", [], "vascular", "vascular_surgeon", "Решение о реваскуляризации", 7, True, False, 20),
    ("arterial_stenosis_50", "Стеноз артерий ≥ 50%", "arterial_stenosis", [{"attr": "percent", "op": "gte", "value": 50}], "vascular", "vascular_surgeon", "Решение о реваскуляризации", 7, True, False, 25),
    ("dvt", "Тромбоз (экстренно)", "dvt", [], "emergency", "vascular_surgeon", "Экстренная консультация", 1, True, True, 1),
]

# Клинические группы: из одного протокола — один маршрут на группу.
GROUPS = {"gyn_surgical": "gyn", "gyn_consult": "gyn", "breast": "breast", "breast_low": "breast",
          "gb_surgical": "gallbladder", "gb_polyp": "gallbladder", "thyroid": "thyroid", "thyroid_biopsy": "thyroid",
          "vascular": "vascular", "phlebology": "veins", "emergency": "emergency"}

# ------------------------------------------------------------------ пороги «на что обратить внимание»
# (code, finding_code, conditions, message). Порог только поясняет, что превышено, и ничего не ранжирует.
# Значения — пример для демонстрации;
# в эксплуатации их утверждает клинический эксперт (редактируются в /admin без изменения кода).
ATTENTION = [
    ("birads_4_6", "birads_category", [{"attr": "birads", "op": "gte", "value": 4}], "BI-RADS {value}: подозрительная категория"),
    ("birads_3", "birads_category", [{"attr": "birads", "op": "eq", "value": 3}], "BI-RADS 3: нужен контроль в динамике"),
    ("birads_0", "birads_category", [{"attr": "birads", "op": "eq", "value": 0}], "BI-RADS 0: оценка неполная — нужно дообследование"),
    ("tirads_4_5", "tirads_category", [{"attr": "tirads", "op": "gte", "value": 4}], "TI-RADS {value}: оценить показания к ТАБ"),
    ("tirads_3", "tirads_category", [{"attr": "tirads", "op": "eq", "value": 3}], "TI-RADS 3: оценить размер узла"),
    ("orads_4_5", "orads_category", [{"attr": "orads", "op": "gte", "value": 4}], "O-RADS {value}: высокий риск"),
    ("orads_3", "orads_category", [{"attr": "orads", "op": "eq", "value": 3}], "O-RADS 3: промежуточный риск"),
    ("gb_polyp_10", "gallbladder_polyp", [{"attr": "size_mm", "op": "gte", "value": 10}], "Полип {value} мм (порог 10 мм)"),
    ("stenosis_70", "arterial_stenosis", [{"attr": "percent", "op": "gte", "value": 70}], "Стеноз {value}% (порог 70%)"),
    ("stenosis_50", "arterial_stenosis", [{"attr": "percent", "op": "gte", "value": 50}, {"attr": "percent", "op": "lt", "value": 70}], "Стеноз {value}% (порог 50%)"),
    ("thyroid_nodule_10", "thyroid_nodule", [{"attr": "size_mm", "op": "gte", "value": 10}], "Узел {value} мм (порог 10 мм)"),
    ("mass_30", "sign:mass", [{"attr": "size_mm", "op": "gte", "value": 30}], "Образование {value} мм (порог 30 мм)"),
    ("thrombus", "sign:thrombus", [], "Упоминание тромба — проверить немедленно"),
]

# ------------------------------------------------------------------ матрица показаний (проверка рекомендаций)
# (code, title, finding, conditions, requirement, specialty, accepted, service_regex, service_title, max_days, severity)
MATRIX = "Матрица маршрутизации кейса СМ-Клиники"
INDICATIONS = [
    ("gallstones_surgeon", "ЖКБ → консультация хирурга", "gallstones", [], "consultation", "surgeon", [], "", "", 14, "major"),
    ("gb_polyp_surgeon", "Полип желчного пузыря → консультация хирурга", "gallbladder_polyp", [], "consultation", "surgeon", [], "", "", 30, "major"),
    ("gb_polyp_10_surgeon", "Полип ЖП ≥ 10 мм → хирург в течение 14 дней", "gallbladder_polyp", [{"attr": "size_mm", "op": "gte", "value": 10}], "consultation", "surgeon", [], "", "", 14, "major"),
    ("endometrial_polyp_gyn", "Полип эндометрия → гинеколог", "endometrial_polyp", [], "consultation", "gynecologist", ["gyn_surgeon"], "", "", 14, "major"),
    ("endometrial_hyperplasia_gyn", "Гиперплазия эндометрия → гинеколог", "endometrial_hyperplasia", [], "consultation", "gynecologist", ["gyn_surgeon"], "", "", 14, "major"),
    ("submucous_myoma_gyn", "Субмукозная миома → гинеколог", "submucous_myoma", [], "consultation", "gynecologist", ["gyn_surgeon"], "", "", 14, "major"),
    ("uterine_myoma_gyn", "Миома матки → гинеколог", "uterine_myoma", [], "consultation", "gynecologist", ["gyn_surgeon"], "", "", 30, "major"),
    ("ovarian_mass_gyn", "Образование яичника → гинеколог", "ovarian_mass", [], "consultation", "gynecologist", ["gyn_surgeon"], "", "", 30, "major"),
    ("orads_3_gyn", "O-RADS ≥ 3 → гинеколог в течение 14 дней", "orads_category", [{"attr": "orads", "op": "gte", "value": 3}], "consultation", "gynecologist", ["gyn_surgeon"], "", "", 14, "major"),
    ("breast_mass_mammologist", "Образование молочной железы → маммолог", "breast_mass", [], "consultation", "mammologist", ["oncologist"], "", "", 30, "major"),
    ("birads_3_mammologist", "BI-RADS ≥ 3 → маммолог в течение 14 дней", "birads_category", [{"attr": "birads", "op": "gte", "value": 3}], "consultation", "mammologist", ["oncologist"], "", "", 14, "major"),
    ("birads_4_biopsy", "BI-RADS ≥ 4 → биопсия", "birads_category", [{"attr": "birads", "op": "gte", "value": 4}], "diagnostics", "", [], r"биопси|трепан|core|пункци", "Биопсия образования молочной железы", 14, "major"),
    ("thyroid_nodule_endo", "Узел щитовидной железы → эндокринолог", "thyroid_nodule", [], "consultation", "endocrinologist", [], "", "", 30, "major"),
    ("tirads_4_tab", "TI-RADS ≥ 4 → ТАБ", "tirads_category", [{"attr": "tirads", "op": "gte", "value": 4}], "diagnostics", "", [], r"\bТАБ\b|тонкоигольн|пункци|биопси", "ТАБ узла щитовидной железы", 14, "major"),
    ("hernia_surgeon", "Грыжа → хирург", "hernia", [], "consultation", "surgeon", [], "", "", 30, "major"),
    ("hydronephrosis_urologist", "Гидронефроз / конкременты почек → уролог", "hydronephrosis", [], "consultation", "urologist", [], "", "", 7, "major"),
    ("bph_urologist", "Гиперплазия предстательной железы → уролог", "bph", [], "consultation", "urologist", [], "", "", 30, "major"),
    ("varicose_phlebologist", "Варикозная болезнь → флеболог", "varicose_veins", [], "consultation", "phlebologist", ["vascular_surgeon"], "", "", 30, "major"),
    ("stenotic_vascular", "Стенозирующий атеросклероз → сосудистый хирург", "stenotic_atherosclerosis", [], "consultation", "vascular_surgeon", [], "", "", 14, "major"),
    ("stenosis_50_vascular", "Стеноз ≥ 50% → сосудистый хирург", "arterial_stenosis", [{"attr": "percent", "op": "gte", "value": 50}], "consultation", "vascular_surgeon", [], "", "", 14, "major"),
    ("dvt_vascular", "Тромбоз → сосудистый хирург в течение суток", "dvt", [], "consultation", "vascular_surgeon", ["surgeon", "phlebologist"], "", "", 1, "critical"),
    ("thrombus_vascular", "Тромб → сосудистый хирург в течение суток", "sign:thrombus", [], "consultation", "vascular_surgeon", ["surgeon", "phlebologist"], "", "", 1, "critical"),
]

# ------------------------------------------------------------------ тексты уведомлений (из кейса)
BOOK_BUTTONS = [{"action": "book", "label": "Записаться на очную консультацию"},
                {"action": "book_online", "label": "Онлайн-консультация"},
                {"action": "callback", "label": "Заказать звонок"}]
FINAL_BUTTONS = [{"action": "book", "label": "Записаться"}, {"action": "seen_elsewhere", "label": "Уже обратился к врачу"},
                 {"action": "decline", "label": "Не планирую обращаться"}]
SMS = "СМ-Клиника: в личном кабинете новое сообщение по результатам исследования. Подробнее в ЛК."
# Звонок голосового робота: без диагноза и деталей, только приглашение в личный кабинет.
CALL = ("Здравствуйте! Это СМ-Клиника. В вашем личном кабинете новое сообщение от клиники. "
        "Подробности — в личном кабинете. Чтобы поговорить с администратором, нажмите ноль.")
# Push читается за секунду: заголовок — что случилось, текст — одно действие.
# Push на экране блокировки: без диагноза, находки и специальности (экран видят посторонние),
# но с конкретным шагом: врач, дата, время и клиника ближайшего приёма ({doctor_offer}). Разбор — docs/PUSH_GUIDE.md.
PUSH_TEXT = {
    "result_ready": ("Результат исследования готов", "Рекомендуется консультация профильного специалиста. {doctor_offer}"),
    "next_step": ("Следующий шаг по рекомендации врача", "Выберите удобное время. {doctor_offer}"),
    "timer_due": ("Пора на контрольное исследование", "Выберите удобное время в личном кабинете"),
    "rebooking": ("Запись отменена", "Рекомендация врача остаётся актуальной. {doctor_offer}"),
    "reminder_24h": ("Консультация ещё не назначена", "Рекомендуется консультация профильного специалиста. {doctor_offer}"),
    "reminder_72h": ("Рекомендация врача ждёт вас", "Рекомендуется консультация профильного специалиста. Выберите время очно или онлайн"),
    "final_soft": ("Напоминаем о консультации", "Отметьте, если уже были у врача"),
    "no_show": ("Приём не состоялся", "Выберите другое время"),
    "booking_confirmed": ("Вы записаны: {date}, {time}", "{doctor}, {location}"),
    "postop_booked": ("Контрольный приём: {date}, {time}", "{doctor}, {location}"),
    "postop_choose_time": ("Выберите время контрольного приёма", "Через 7 дней после лечения"),
}
TEMPLATES_TEXT = {
    "result_ready": ("Ваш результат исследования готов",
                     "В исследовании описаны изменения, по которым рекомендуется консультация {specialty} для определения дальнейшей тактики. "
                     "{doctor_offer} Можно выбрать и другое время или врача.", BOOK_BUTTONS),
    "next_step": ("Следующий этап вашего маршрута", "Следующий этап: {step}. Выберите удобное время — очно или онлайн.", BOOK_BUTTONS),
    "timer_due": ("Подходит срок контрольного исследования", "Рекомендован следующий этап: {step}. Выберите удобное время.", BOOK_BUTTONS),
    "rebooking": ("Требуется повторная запись", "Запись отменена, но рекомендация остаётся актуальной: {step}. Выберите другое время.", BOOK_BUTTONS),
    "reminder_24h": ("Напоминание", "По результату исследования вам рекомендована консультация {specialty}. {doctor_offer} Можно выбрать очный приём или онлайн.", BOOK_BUTTONS),
    "reminder_72h": ("Рекомендация врача остаётся актуальной", "По результатам исследования остаётся рекомендация проконсультироваться с врачом. Подобрать специалиста и удобное время можно по ссылке.", BOOK_BUTTONS),
    "final_soft": ("Напоминаем о рекомендации", "Напоминаем о рекомендации обратиться к профильному врачу по результатам ранее выполненного исследования. "
                   "Если консультация уже состоялась в другой медицинской организации, вы можете отметить это здесь.", FINAL_BUTTONS),
    "no_show": ("Консультация не состоялась", "Сегодня не состоялась запланированная консультация врача. Если вопрос остаётся актуальным, мы можем предложить другое время или онлайн-консультацию.", BOOK_BUTTONS),
    "booking_confirmed": ("Вы записаны", "Приём {date} в {time}: {doctor}, {location}.", [{"action": "confirm", "label": "Подтвердить"}, {"action": "reschedule", "label": "Изменить время"}]),
    "postop_booked": ("Контрольный приём назначен", "Контрольный приём после проведённого лечения назначен на {date} в {time} ({doctor}, {location}).",
                      [{"action": "confirm", "label": "Подтвердить"}, {"action": "reschedule", "label": "Изменить время"}]),
    "postop_choose_time": ("Рекомендован контрольный приём", "После проведённого лечения рекомендован контрольный приём врача через 7 дней. Выберите удобное время.", BOOK_BUTTONS),
}


# Демо-пациенты (псевдонимы). Олег Петрович — старше 60: в его кабинете виден совет включить SMS.
DEMO_PATIENTS = [
    ("AK-0001", "Анна Сергеевна", 1985, "F", "+7 *** ***-12-34"),
    ("AK-0002", "Мария Ивановна", 1972, "F", "+7 *** ***-56-78"),
    ("AK-0003", "Олег Петрович", 1958, "M", "+7 *** ***-90-12"),
    ("AK-0004", "Елена Викторовна", 1990, "F", "+7 *** ***-34-56"),
    ("AK-0005", "Ирина Павловна", 1979, "F", "+7 *** ***-78-90"),
    ("AK-0006", "Татьяна Олеговна", 1955, "F", "+7 *** ***-11-22"),
    ("AK-0007", "Сергей Николаевич", 1968, "M", "+7 *** ***-33-44"),
    ("AK-0008", "Наталья Андреевна", 1983, "F", "+7 *** ***-55-66"),
]


class Command(BaseCommand):
    help = "Загрузить настройки (словари, матрицу, шаблоны) и демо-данные"

    def add_arguments(self, parser):
        parser.add_argument("--no-demo", action="store_true", help="Без демо-врачей и пациентов")
        parser.add_argument("--days", type=int, default=60, help="На сколько дней сгенерировать расписание")

    @transaction.atomic
    def handle(self, *args, **opts):
        for code, title, patterns, exclude, severity in FINDINGS:
            FindingDefinition.objects.update_or_create(code=code, defaults={
                "title": title, "patterns": patterns, "exclude_patterns": exclude, "severity": severity})
        SpecialtyAlias.objects.all().delete()
        SpecialtyAlias.objects.bulk_create(SpecialtyAlias(specialty_code=c, pattern=p) for c, p in SPECIALTY_ALIASES)

        policies = {code: EscalationPolicy.objects.update_or_create(code=code, defaults={"title": t, "ladder": l})[0]
                    for code, (t, l) in POLICIES.items()}
        templates = {}
        for code, (title, steps) in TEMPLATES.items():
            tpl, _ = RouteTemplate.objects.update_or_create(code=code, defaults={"title": title})
            tpl.steps.all().delete()
            for i, (stype, stitle, spec, service, offset, window, auto, policy) in enumerate(steps, start=1):
                RouteTemplateStep.objects.create(template=tpl, order=i, step_type=stype, title=stitle, specialty_code=spec,
                                                 service_code=service, offset_days=offset, window_days=window, auto_book=auto,
                                                 escalation_policy=policies.get(policy))
            templates[code] = tpl
        for code, title, finding, conds, tpl, spec, potential, target, surgical, emergency, priority in RULES:
            TriggerRule.objects.update_or_create(code=code, version=1, defaults={"route_group": GROUPS.get(tpl, tpl),
                "title": title, "finding_code": finding, "conditions": conds, "template": templates[tpl],
                "first_specialty_code": spec, "potential_route": potential, "target_days": target,
                "is_surgical": surgical, "is_emergency": emergency, "priority": priority,
                "responsible_unit": "Координатор хирургического маршрута" if surgical else "Контакт-центр"})

        for code, finding, conds, message in ATTENTION:
            AttentionRule.objects.update_or_create(code=code, defaults={
                "finding_code": finding, "conditions": conds, "message": message,
                "source": "Пример для демонстрации — утверждает клинический эксперт"})
        for code, title, finding, conds, req, spec, accepted, service_rx, service_title, max_days, severity in INDICATIONS:
            IndicationRule.objects.update_or_create(code=code, defaults={
                "title": title, "finding_code": finding, "conditions": conds, "requirement": req, "specialty_code": spec,
                "accepted_specialties": accepted, "service_pattern": service_rx, "service_title": service_title,
                "max_days": max_days, "severity": severity, "rationale": f"{MATRIX}: {title}"})

        for code, (title, body, buttons) in TEMPLATES_TEXT.items():
            NotificationTemplate.objects.update_or_create(code=code, channel="lk", defaults={
                "title": title, "body": body, "buttons": buttons})
            push_title, push_body = PUSH_TEXT.get(code, (title, body.split(". ")[0] + "."))
            NotificationTemplate.objects.update_or_create(code=code, channel="push", defaults={
                "title": push_title, "body": push_body, "buttons": buttons})
            NotificationTemplate.objects.update_or_create(code=code, channel="sms", defaults={"title": "", "body": SMS})
            NotificationTemplate.objects.update_or_create(code=code, channel="call", defaults={"title": "", "body": CALL})
        for code, (patient_title, explanation) in PATIENT_TEXTS.items():
            FindingDefinition.objects.filter(code=code).update(patient_title=patient_title, patient_explanation=explanation)
        ensure_system_tags()
        for code, title, color in CUSTOM_TAGS:
            Tag.objects.get_or_create(code=code, defaults={"title": title, "color": color, "is_system": False})

        for code, title, gen, surgical in SPECIALTIES:
            Specialty.objects.update_or_create(code=code, defaults={"title": title, "title_genitive": gen, "is_surgical": surgical})
        for code, title, address, online, hospital in LOCATIONS:
            ClinicLocation.objects.update_or_create(code=code, defaults={"title": title, "address": address,
                                                                         "is_online": online, "has_hospital": hospital})
        self.stdout.write(self.style.SUCCESS(
            f"Настройки: {len(FINDINGS)} находок, {len(RULES)} правил, {len(TEMPLATES)} шаблонов, "
            f"{len(ATTENTION)} порогов внимания, {len(INDICATIONS)} показаний"))
        # Реальные цифры: сводка прогона протоколов кейса (только счётчики, без текстов) — в «Качество разбора».
        summary = settings.BASE_DIR / "docs" / "case_run_summary.json"
        if summary.exists():
            run = import_summary(summary)
            self.stdout.write(self.style.SUCCESS(f"Сводка прогона: «{run.title}»"))
        if opts["no_demo"]:
            return

        slots = 0
        # Вход сотрудников (gateway/staff_auth.py): демо-координатор и врачи doctor1…doctor9, пароль STAFF_DEMO_PASSWORD.
        staff_password = settings.STAFF_LOGIN["DEMO_PASSWORD"]
        StaffAuthService.set_account("coordinator", staff_password, "coordinator", full_name="Координатор Демо")
        for number, (name, specs, locs, profiles) in enumerate(DOCTORS, start=1):
            doctor, _ = Doctor.objects.update_or_create(full_name=name, defaults={"surgical_profiles": profiles})
            if doctor.user_id is None or doctor.user.username == f"doctor{number}":
                doctor.user = StaffAuthService.set_account(f"doctor{number}", staff_password, "doctor", full_name=name)
                doctor.save(update_fields=["user", "updated_at"])
            doctor.specialties.set(specs)
            doctor.locations.set(locs)
            for i, loc in enumerate(locs):
                for spec in specs:
                    slots += ScheduleService().generate(
                        doctor, Specialty.objects.get(pk=spec), ClinicLocation.objects.get(pk=loc), days=opts["days"],
                        start_hour=9 + i * 4 + specs.index(spec) * 2, end_hour=min(21, 13 + i * 4 + specs.index(spec) * 2),
                        step_minutes=60)
        password = settings.PATIENT_LOGIN["DEMO_PASSWORD"]
        for mis_id, name, year, sex, phone in DEMO_PATIENTS:
            patient, _ = Patient.objects.update_or_create(external_mis_id=mis_id, defaults={
                "display_name": name, "birth_year": year, "sex": sex, "phone_masked": phone})
            # Вход в личный кабинет: номер карты + пароль демо-пациента (settings.PATIENT_LOGIN).
            PatientAuthService.set_password(patient, password)
        self.stdout.write(self.style.SUCCESS(f"Демо: {len(DOCTORS)} врачей, {slots} слотов, {len(DEMO_PATIENTS)} пациентов"))
