# Celery поднимается вместе с Django, чтобы декоратор @shared_task видел приложение.
from .celery import app as celery_app

__all__ = ("celery_app",)
