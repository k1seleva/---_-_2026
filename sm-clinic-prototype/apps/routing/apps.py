from django.apps import AppConfig


class RoutingConfig(AppConfig):
    name = "apps.routing"
    label = "routing"
    verbose_name = "Модуль составления маршрута"

    def ready(self) -> None:
        from common import labels

        from . import handlers
        from .facade import RoutingFacade
        from .models import PatientRoute, RouteStep, StepType

        handlers.register()
        labels.register_choices("route_status", PatientRoute.Status)
        labels.register_choices("route_kind", PatientRoute.Kind)
        labels.register_choices("step_status", RouteStep.Status)
        labels.register_choices("step_source", RouteStep.Source)
        labels.register_choices("step_type", StepType)
        # trigger_code маршрута — код находки или «recommendation»; подписи — из правил матрицы.
        labels.register("trigger", {"recommendation": "Рекомендации в протоколе", "postop": "Послеоперационное наблюдение",
                                    "manual": "Маршрут координатора"})
        labels.register_resolver("trigger", RoutingFacade.trigger_titles)
        labels.register_resolver("rule", RoutingFacade.rule_titles)
        labels.register_resolver("route_template", lambda keys: {r["code"]: r["title"] for r in RoutingFacade.route_catalog()})
