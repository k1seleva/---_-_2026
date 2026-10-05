import json

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from common import clock

from .mis import MIS_EVENT_EFFECTS, StubMisEventHandler, get_mis_handler, verify_signature


class MisEventWebhook(APIView):
    """
    Единая точка приёма событий от МИС 1С. В прототипе — заглушка (settings.MIS_WEBHOOK_MODE):
    POST /api/v1/mis/events/  — проверить событие по контракту; в режиме live — обработать
    GET  /api/v1/mis/events/  — режим, поддерживаемые типы событий и журнал заглушки
    """

    authentication_classes: list = []

    def get(self, request):
        return Response({"mode": settings.MIS_WEBHOOK_MODE, "event_types": MIS_EVENT_EFFECTS,
                         "journal": StubMisEventHandler.journal()})

    def post(self, request):
        if not verify_signature(request.body, request.headers.get("X-MIS-Signature", "")):
            return Response({"detail": "Неверная подпись"}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            result = get_mis_handler().handle(json.loads(request.body))
        except (KeyError, ValueError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_202_ACCEPTED)


class SimulationView(APIView):
    """Управление модельным временем (только демо, п. 12 кейса):
    POST /api/v1/sim/advance/ {"hours": 24}  — сдвинуть время и прогнать эскалации
    POST /api/v1/sim/reset/                   — вернуть реальное время
    """

    def post(self, request, op: str):
        from apps.routing.services.escalation import EscalationEngine

        if op == "advance":
            clock.advance(hours=float(request.data.get("hours", 0)), days=float(request.data.get("days", 0)))
        elif op == "reset":
            clock.reset()
        report = EscalationEngine().tick()
        return Response({"model_now": clock.now(), "tick": report.__dict__})


class HealthView(APIView):
    def get(self, request):
        return Response({"status": "ok", "model_now": clock.now()})
