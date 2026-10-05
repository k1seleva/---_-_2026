"""
Настройки прототипа «СМ-Клиника · Маршрут пациента».

Все чувствительные параметры читаются из переменных окружения (см. .env.example).
По умолчанию прототип запускается без внешних зависимостей: SQLite, Celery в eager-режиме,
распознавание находок правилами (без LLM).
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def load_env_file(path: Path) -> None:
    """Переменные из .env (python-dotenv) для запуска без Docker: `python manage.py runserver` видит те же
    настройки, что и docker compose. Уже заданные в окружении переменные не перезаписываются, комментарий
    в конце строки допускается (LLM_NUM_CTX=8192  # окно контекста).
    Тесты .env не читают: они не должны зависеть от локальной модели разработчика."""
    if not path.exists() or sys.argv[1:2] == ["test"]:
        return
    load_dotenv(path, override=False, encoding="utf-8")


load_env_file(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-insecure-key")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    # Общее ядро: шина событий, модельное время, идемпотентность.
    "common",
    # Пять изолированных модулей (будущие микросервисы).
    "apps.processing",
    "apps.routing",
    "apps.coordinator",
    "apps.patients",
    "apps.doctors",
    # Проверка достаточности рекомендаций (необходимость коррекции маршрута).
    "apps.audit",
    # API-шлюз: единая точка входа /api/v1 и приём событий МИС.
    "gateway",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    # Три роли: координатор (/coordinator/, /processing/), врач (/doctor/), пациент (свой кабинет).
    "gateway.staff_auth.StaffAccessMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "common.context_processors.demo_context",
                "gateway.context.workspace",
            ],
            # Человекочитаемые подписи доступны во всех шаблонах без {% load %}.
            "builtins": ["common.templatetags.labels", "common.templatetags.ui"],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# В прототипе — SQLite. В эксплуатации у каждого модуля своя схема/БД PostgreSQL
# (DATABASE_URL_<MODULE>), поэтому между модулями нет внешних ключей.
DATABASES = {
    "default": {
        "ENGINE": os.getenv("DB_ENGINE", "django.db.backends.sqlite3"),
        "NAME": os.getenv("DB_NAME", str(BASE_DIR / "db.sqlite3")),
        "USER": os.getenv("DB_USER", ""),
        "PASSWORD": os.getenv("DB_PASSWORD", ""),
        "HOST": os.getenv("DB_HOST", ""),
        "PORT": os.getenv("DB_PORT", ""),
    }
}
if DATABASES["default"]["ENGINE"].endswith("sqlite3"):
    # Фоновый разбор пишет в базу, пока страницы читают: ждём освобождения, а не падаем с «database is locked».
    # Режим WAL (чтение не блокирует запись) включается при подключении, см. processing/apps.py.
    DATABASES["default"]["OPTIONS"] = {"timeout": int(os.getenv("SQLITE_TIMEOUT_SEC") or 30)}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", BASE_DIR / "media"))

# Ограничения на загрузку протоколов.
UPLOAD_MAX_BYTES = int(os.getenv("UPLOAD_MAX_BYTES", 10 * 1024 * 1024))
UPLOAD_ALLOWED_EXTENSIONS = (".doc", ".docx", ".json")
# Пакетная загрузка: до 500 файлов за раз (zip-архив считается одним файлом формы).
DATA_UPLOAD_MAX_NUMBER_FILES = 600
# Папка-наблюдатель: <PROTOCOL_INBOX_DIR>/<код клиники>/*.docx забираются автоматически
# (команда watch_inbox или Celery beat), затем переносятся в processed/ или failed/.
PROTOCOL_INBOX_DIR = Path(os.getenv("PROTOCOL_INBOX_DIR", BASE_DIR / "inbox"))

REST_FRAMEWORK = {
    # ВНИМАНИЕ: в прототипе API открыт для демонстрации. В пилоте — JWT/OIDC клиники
    # и разграничение ролей (пациент / врач / координатор / руководитель).
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
}

# --- Celery -----------------------------------------------------------------
# eager = задачи выполняются синхронно в процессе Django (демо без Redis, брокер в памяти).
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", True)
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "memory://" if CELERY_TASK_ALWAYS_EAGER else "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", None)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TIMEZONE = TIME_ZONE
# Разбор протокола с Qwen идёт минутами: воркер берёт по одной задаче, подтверждает её после выполнения
# (упавшая задача вернётся в очередь), задачи модели идут в отдельную очередь llm (свой воркер, см. docker-compose).
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_ROUTES = {
    "apps.processing.tasks.process_document": {"queue": "llm"},
    "apps.processing.tasks.generate_routing_advice": {"queue": "llm"},
    "apps.processing.tasks.run_quality_check": {"queue": "llm"},
}

# --- Очередь разбора протоколов (processing/services/jobs.py) -----------------
# thread — фоновый поток в процессе сайта (runserver без Redis): загрузка пачки отвечает сразу,
#          протоколы разбираются по очереди, прогресс виден во «Входящих»;
# celery — Celery-воркер очереди llm (docker compose, Redis);
# sync   — разбор прямо в запросе загрузки (тесты, отладка).
TESTING = sys.argv[1:2] == ["test"]
PROCESSING_QUEUE = os.getenv("PROCESSING_QUEUE") or (
    "sync" if TESTING else "thread" if CELERY_TASK_ALWAYS_EAGER else "celery")
# Команды manage.py (seed_demo, watch_inbox, demo_batch) живут недолго: фоновый поток умер бы вместе с ними,
# поэтому в режиме thread они разбирают протоколы сами, по одному. Сайт и process_queue работают с очередью.
_COMMAND = sys.argv[1] if len(sys.argv) > 1 and sys.argv[0].endswith("manage.py") else ""
if PROCESSING_QUEUE == "thread" and _COMMAND not in ("", "runserver", "process_queue"):
    PROCESSING_QUEUE = "sync"
# Сколько протоколов разбирать одновременно. Локальная модель отвечает на запросы по очереди
# (Ollama: OLLAMA_NUM_PARALLEL), поэтому больше 1 имеет смысл, только если сервер модели тянет параллельно.
PROCESSING_WORKERS = int(os.getenv("PROCESSING_WORKERS") or 1)
# Задача «в работе» дольше этого срока считается зависшей (воркер упал) и возвращается в очередь.
PROCESSING_STALE_MINUTES = int(os.getenv("PROCESSING_STALE_MINUTES") or 20)
PROCESSING_MAX_ATTEMPTS = int(os.getenv("PROCESSING_MAX_ATTEMPTS") or 2)

# --- Шина событий -----------------------------------------------------------
# "inprocess" — обработчики вызываются в той же транзакции/процессе (прототип);
# "celery"    — каждый обработчик становится отдельной Celery-задачей (шаг к микросервисам).
EVENT_BUS_BACKEND = os.getenv("EVENT_BUS_BACKEND", "inprocess")

# --- AI-агент (LangChain) ---------------------------------------------------
def _num_predict() -> int:
    if os.getenv("LLM_NUM_PREDICT"):
        return int(os.getenv("LLM_NUM_PREDICT"))
    return 2048 if os.getenv("LLM_REASONING", "").lower() in ("0", "false") else 0



# provider: none | openai | anthropic | gigachat | ollama | ...
# none — только правила (детерминированно, без передачи данных наружу).
# Для реальных ПДн: только модель в контуре клиники (ollama / vLLM / GigaChat on-prem).
AI_AGENT = {
    "PROVIDER": os.getenv("LLM_PROVIDER", "none"),
    "MODEL": os.getenv("LLM_MODEL", ""),
    "BASE_URL": os.getenv("LLM_BASE_URL", ""),
    "TEMPERATURE": float(os.getenv("LLM_TEMPERATURE") or 0),
    "TIMEOUT_SEC": int(os.getenv("LLM_TIMEOUT_SEC") or 180),
    # Ollama: окно контекста в токенах. По умолчанию у Ollama 2–4 тыс., протокол с инструкцией обрезается.
    "NUM_CTX": int(os.getenv("LLM_NUM_CTX") or 8192),
    # Qwen3: false — отвечать без «размышлений» (быстрее); пусто — как настроено в самой модели.
    "REASONING": {"1": True, "true": True, "0": False, "false": False}.get(os.getenv("LLM_REASONING", "").lower()),
    # Предел длины ответа модели в токенах: не даёт «зациклившемуся» ответу занять минуты. По умолчанию
    # ставится только без «размышлений» (LLM_REASONING=0): у Qwen3 размышления входят в этот предел.
    "NUM_PREDICT": _num_predict(),
    # Сколько Ollama держит модель в памяти после запроса: без этого между протоколами она выгружается.
    "KEEP_ALIVE": os.getenv("LLM_KEEP_ALIVE") or "30m",
    # 2 — находки заключения и разметка фрагментов запрашиваются одновременно (нужен OLLAMA_NUM_PARALLEL >= 2).
    "PARALLEL_CALLS": int(os.getenv("LLM_PARALLEL_CALLS") or 1),
    # После тайм-аута или обрыва связи модель не спрашиваем столько секунд: пачка не ждёт по 3 минуты
    # на каждый протокол; фоновая очередь в это время ждёт модель (до LLM_WAIT_MINUTES), потом идёт по словарю.
    "COOLDOWN_SEC": int(os.getenv("LLM_COOLDOWN_SEC") or 60),
    "WAIT_MINUTES": int(os.getenv("LLM_WAIT_MINUTES") or 10),
    "PROMPT_VERSION": "extract-v1",
    # Если LLM упал или вернул невалидный JSON — используем правила.
    "FALLBACK_TO_RULES": True,
}

# --- Вход пациента в личный кабинет ---------------------------------------------------
# Номер карты + пароль (в пилоте — вход через ЛК СМ-Клиники или код из SMS). Защита от подбора паролей.
PATIENT_LOGIN = {
    "MAX_ATTEMPTS": int(os.getenv("PATIENT_LOGIN_MAX_ATTEMPTS", "5")),
    "LOCK_MINUTES": int(os.getenv("PATIENT_LOGIN_LOCK_MINUTES", "15")),
    # Пароль демо-пациентов (seed_demo). Подсказка на странице входа — только в режиме разработки.
    "DEMO_PASSWORD": os.getenv("PATIENT_DEMO_PASSWORD", "demo-2026"),
    "SHOW_DEMO_HINT": env_bool("PATIENT_DEMO_HINT", DEBUG),
}

# Вход сотрудников (gateway/staff_auth.py): координатор и врач, логин и пароль, роль из группы Django.
STAFF_LOGIN = {
    "MAX_ATTEMPTS": int(os.getenv("STAFF_LOGIN_MAX_ATTEMPTS", "5")),
    "LOCK_MINUTES": int(os.getenv("STAFF_LOGIN_LOCK_MINUTES", "15")),
    # Пароль демо-координатора и демо-врачей (seed_demo). Подсказка на странице входа — только в разработке.
    "DEMO_PASSWORD": os.getenv("STAFF_DEMO_PASSWORD", "demo-2026"),
    "SHOW_DEMO_HINT": env_bool("STAFF_DEMO_HINT", DEBUG),
}
LOGIN_URL = "/coordinator/login/"
if TESTING:
    # В тестах seed_demo создаёт десятки учётных записей: медленный боевой хеш паролей там не нужен.
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# --- Советы ИИ-агента по маршрутизации (Qwen через LangChain) ----------------------
# Совет — только рекомендация с пометкой «требует проверки координатором»; маршрут по нему не меняется.
# MODE: auto — Qwen, если задан провайдер (LLM_PROVIDER или ROUTING_ADVISOR_PROVIDER); если модель не ответила,
#       на странице видна ошибка и кнопка «Спросить Qwen ещё раз», подмены демо-советами нет.
#       Без провайдера — демо-режим (советы из триггеров, с явной пометкой).
#       qwen — только модель; demo — только демо; off — блок советов выключен.
ROUTING_ADVISOR = {
    "MODE": os.getenv("ROUTING_ADVISOR_MODE", "auto"),
    # ollama (Qwen локально в контуре клиники) или openai (vLLM с OpenAI-совместимым API).
    "PROVIDER": os.getenv("ROUTING_ADVISOR_PROVIDER", os.getenv("LLM_PROVIDER", "none")),
    # По умолчанию та же модель, что у ИИ-агента разметки (LLM_MODEL): один Qwen на всё.
    "MODEL": os.getenv("ROUTING_ADVISOR_MODEL") or os.getenv("LLM_MODEL") or "qwen2.5:14b-instruct",
    "BASE_URL": os.getenv("ROUTING_ADVISOR_BASE_URL", os.getenv("LLM_BASE_URL", "")),
    "TEMPERATURE": float(os.getenv("ROUTING_ADVISOR_TEMPERATURE", "0")),
    "TIMEOUT_SEC": int(os.getenv("ROUTING_ADVISOR_TIMEOUT_SEC") or os.getenv("LLM_TIMEOUT_SEC") or 180),
    "NUM_CTX": int(os.getenv("LLM_NUM_CTX") or 8192),
    "REASONING": {"1": True, "true": True, "0": False, "false": False}.get(os.getenv("LLM_REASONING", "").lower()),
    "NUM_PREDICT": _num_predict(),
    "KEEP_ALIVE": os.getenv("LLM_KEEP_ALIVE") or "30m",
    "MAX_ADVICE": int(os.getenv("ROUTING_ADVISOR_MAX_ADVICE", "5")),
    "PROMPT_VERSION": "route-advice-v1",
}

# --- Уведомления -------------------------------------------------------------
NOTIFICATIONS = {
    # Порядок каналов: личный кабинет приоритетен (по кейсу), затем push, затем SMS.
    "CHANNEL_PRIORITY": ["lk", "push", "sms", "call"],
    # Антиспам: не более N сообщений пациенту за сутки и «тихие часы».
    "MAX_PER_DAY": int(os.getenv("NOTIFY_MAX_PER_DAY", 3)),
    "QUIET_HOURS": (21, 9),
    "PUBLIC_BASE_URL": os.getenv("PUBLIC_BASE_URL", "http://localhost:8000"),
    # С этого возраста в настройках уведомлений показывается рекомендация включить SMS
    # (только подсказка: канал меняет сам пациент).
    "SMS_RECOMMENDED_AGE": int(os.getenv("NOTIFY_SMS_RECOMMENDED_AGE", 60)),
    # Канал по умолчанию для всех типов событий (личный кабинет включён всегда).
    "DEFAULT_CHANNELS": ("push",),
}

# --- Интеграция с МИС 1С ------------------------------------------------------
# stub — заглушка: вебхук проверяет формат события и пишет его в журнал, ничего не меняя в системе;
# live — событие обрабатывается (создаётся протокол, этапы стационара двигают маршрут).
MIS_WEBHOOK_MODE = os.getenv("MIS_WEBHOOK_MODE", "stub")

# --- Проверка качества аналитики ---------------------------------------------
# Пакетный прогон протоколов без записи пациентов и маршрутов: сколько файлов за один запуск.
QUALITY_MAX_PROTOCOLS = int(os.getenv("QUALITY_MAX_PROTOCOLS", 100))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
}
