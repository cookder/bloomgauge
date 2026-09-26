"""Every local module a bundled module imports must itself be bundled."""

import ast
import unittest
from pathlib import Path

NATIVE = Path(__file__).resolve().parent


def bundled():
    lines = (NATIVE / 'bundle-resources.txt').read_text().splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith('#')}


def local_imports(path):
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split('.')[0])
    return {name + '.py' for name in names if (NATIVE / (name + '.py')).is_file()}


class BundleResourceTests(unittest.TestCase):
    def test_bundled_modules_only_import_bundled_modules(self):
        resources = bundled()
        missing = {}
        for name in sorted(r for r in resources if r.endswith('.py')):
            absent = local_imports(NATIVE / name) - resources
            if absent:
                missing[name] = sorted(absent)
        self.assertEqual(missing, {}, 'Add these modules to native/bundle-resources.txt')


if __name__ == '__main__':
    unittest.main()
