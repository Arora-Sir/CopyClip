"""Layering rules, enforced by reading imports. A violation fails the build, not a review."""

import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# package -> packages it may NOT import at runtime (type-checking-only imports are exempt)
FORBIDDEN = {
    "core": {"nodes", "domains", "transports", "windows_app", "winclip", "winimage"},
    "domains": {"nodes", "transports", "windows_app", "winclip", "winimage"},
    "transports": {"nodes", "domains", "windows_app", "winclip", "winimage"},
    "nodes": {"windows_app"},
}


def runtime_imports(path):
    """Top-level module names imported outside `if TYPE_CHECKING` blocks."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    found = set()

    def visit(body):
        for node in body:
            if isinstance(node, ast.Import):
                found.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
            elif isinstance(node, ast.If):
                test = ast.unparse(node.test)
                if "TYPE_CHECKING" not in test:
                    visit(node.body)
                visit(node.orelse)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Try, ast.With)):
                for field in ("body", "handlers", "orelse", "finalbody"):
                    sub = getattr(node, field, None)
                    if sub:
                        visit([h for h in sub] if field != "handlers" else [b for h in sub for b in h.body])
    visit(tree.body)
    return found


class LayeringTests(unittest.TestCase):
    def test_forbidden_imports(self):
        problems = []
        for package, banned in FORBIDDEN.items():
            pdir = os.path.join(ROOT, package)
            for name in sorted(os.listdir(pdir)):
                if not name.endswith(".py"):
                    continue
                if package == "nodes" and name == "base.py":
                    continue
                bad = runtime_imports(os.path.join(pdir, name)) & banned
                if bad:
                    problems.append(f"{package}/{name} imports {sorted(bad)}")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_node_base_shim_is_the_same_class(self):
        from core.interfaces import ClipboardNode
        from nodes.base import ClipboardNode as Shim
        self.assertIs(ClipboardNode, Shim)

    def test_core_has_no_third_party_imports(self):
        stdlib = set(__import__("sys").stdlib_module_names) | {"core", "__future__"}
        problems = []
        for package in ("core", "domains", "transports", "nodes", "windows_app"):
            pdir = os.path.join(ROOT, package)
            for name in sorted(os.listdir(pdir)):
                if name.endswith(".py"):
                    outside = runtime_imports(os.path.join(pdir, name)) - stdlib - {
                        "core", "nodes", "domains", "transports", "windows_app", "winclip",
                        "winimage", "copyclip", "dashboard"}
                    if outside:
                        problems.append(f"{package}/{name}: {sorted(outside)}")
        self.assertEqual(problems, [], "zero-dependency rule broken:\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
