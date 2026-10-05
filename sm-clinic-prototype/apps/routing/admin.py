from django.apps import apps
from django.contrib import admin

# Все модели модуля доступны в админке: матрица, словари, шаблоны и тексты — это настройки.
for model in apps.get_app_config(__name__.split(".")[1]).get_models():
    admin.site.register(model)
