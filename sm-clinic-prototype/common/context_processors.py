from common import clock


def demo_context(request):
    """Модельное время показывается в шапке каждой страницы прототипа."""
    try:
        return {"model_now": clock.now(),
                "sim_steps": [(1, "+1 ч"), (24, "+24 ч"), (72, "+72 ч"), (168, "+7 дн"), (336, "+14 дн"), (720, "+30 дн")]}
    except Exception:  # БД ещё не мигрирована
        return {}
