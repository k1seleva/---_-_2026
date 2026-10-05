"""Архитектурный тест: модули общаются только через facade.py и события.
Импорт чужих models/services запрещён — так модуль можно вынести в микросервис."""
import ast
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parent.parent / "apps"
MODULES = {"processing", "routing", "coordinator", "patients", "doctors", "audit"}
ALLOWED = {"facade"}


class ModuleIsolationTests(SimpleTestCase):
    def test_no_cross_module_internal_imports(self):
        violations = []
        for path in ROOT.rglob("*.py"):
            owner = path.relative_to(ROOT).parts[0]
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("apps."):
                    parts = node.module.split(".")
                    target = parts[1]
                    if target in MODULES and target != owner and (len(parts) < 3 or parts[2] not in ALLOWED):
                        violations.append(f"{path.relative_to(ROOT)}: {node.module}")
        self.assertEqual(violations, [], "Нарушена изоляция модулей")
