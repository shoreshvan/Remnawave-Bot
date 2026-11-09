"""Read-only localization inventory. Findings are review candidates, not coverage proof.

Run from the repository root:
    python scripts/audit_persian_localization.py --output docs/i18n-inventory.json
No application imports, configuration, database, or network access are needed.
"""

import argparse
import ast
import json
import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROSE = re.compile(r'[А-Яа-яЁё]|[A-Za-z]{3,}[\s:!?]+[A-Za-z]')
DISPLAY_CALLS = {
    'answer', 'reply', 'edit_text', 'edit_caption', 'send_message', 'send_photo',
    'send_document', 'send_invoice', 'answer_photo', 'reply_photo', 'HTTPException',
    'InlineKeyboardButton', 'KeyboardButton', 'BotCommand',
}
DISPLAY_KWARGS = {'text', 'caption', 'detail', 'description', 'title', 'label', 'message'}


def load_catalog(path):
    duplicates = []

    def pairs_hook(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                duplicates.append(key)
            result[key] = value
        return result

    raw = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs_hook)
    normalized = {}
    collisions = []

    def flatten(data, prefix=''):
        for key, value in data.items():
            key = str(key).strip().replace(' ', '_').upper()
            key = f'{prefix}_{key}' if prefix else key
            if isinstance(value, dict):
                flatten(value, key)
            else:
                if key in normalized:
                    collisions.append(key)
                normalized[key] = value

    flatten(raw)
    return normalized, duplicates, collisions


def literal_text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return ''.join(literal_text(value) for value in node.values if isinstance(value, ast.Constant))
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return literal_text(node.left) + (literal_text(node.right) if isinstance(node.op, ast.Add) else '')
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'format':
        return literal_text(node.func.value)
    return ''


def is_texts(node):
    if isinstance(node, ast.Name):
        return node.id == 'texts' or node.id.endswith('_texts')
    if isinstance(node, ast.Attribute):
        return node.attr == 'texts' or node.attr.endswith('_texts')
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'get_texts'


def scan_file(path, catalog):
    source = path.read_text(encoding='utf-8-sig')
    tree = ast.parse(source)
    findings = []
    references = set()
    parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    excluded_literals = set()
    scopes = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    dict_bindings = set()

    def scope_of(node):
        while id(node) in parents:
            node = parents[id(node)]
            if isinstance(node, scopes):
                return id(node)
        return id(tree)

    for node in ast.walk(tree):
        if isinstance(node, scopes) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                excluded_literals.add(id(first.value))
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Dict, ast.DictComp)):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    dict_bindings.add((scope_of(node), target.id))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                annotation = arg.annotation
                if isinstance(annotation, ast.Subscript):
                    annotation = annotation.value
                if isinstance(annotation, ast.Name) and annotation.id in {'dict', 'Dict', 'Mapping'}:
                    dict_bindings.add((id(node), arg.arg))
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == '_t' and node.args and is_texts(node.args[0]):
                for arg in node.args[1:]:
                    excluded_literals.update(id(child) for child in ast.walk(arg))
            if isinstance(func, ast.Attribute):
                if isinstance(func.value, ast.Name) and func.value.id in {'logger', '_logger', 'structlog'}:
                    excluded_literals.update(id(child) for child in ast.walk(node))
                elif is_texts(func.value) and func.attr in {'t', 'get'}:
                    for arg in node.args:
                        excluded_literals.update(id(child) for child in ast.walk(arg))

    def catalog_receiver(node):
        return is_texts(node) and not (
            isinstance(node, ast.Name) and (scope_of(node), node.id) in dict_bindings
        )

    def add(node, kind, **details):
        findings.append({'path': path.relative_to(ROOT).as_posix(), 'line': node.lineno,
                         'kind': kind, 'status': 'needs_review', **details})

    def check_key(node, key):
        references.add(key)
        if key == 'RULES_TEXT':
            return
        if key not in catalog:
            add(node, 'missing_fa_key', key=key)
        elif not isinstance(catalog[key], str):
            add(node, 'invalid_fa_value', key=key)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', '')
            key_arg = None
            if isinstance(func, ast.Attribute) and catalog_receiver(func.value) and name in {'t', 'get'}:
                key_arg = node.args[0] if node.args else next(
                    (kw.value for kw in node.keywords if kw.arg in {'key', 'item'}), None
                )
            elif name == '_t' and len(node.args) > 1 and is_texts(node.args[0]):
                key_arg = node.args[1]
            if key_arg is not None:
                if isinstance(key_arg, ast.Constant) and isinstance(key_arg.value, str):
                    check_key(node, key_arg.value)
                else:
                    add(node, 'dynamic_key', expression=ast.unparse(key_arg))
            if name in DISPLAY_CALLS:
                values = [kw.value for kw in node.keywords if kw.arg in DISPLAY_KWARGS]
                if node.args and name not in {'send_message', 'send_photo', 'send_document', 'send_invoice'}:
                    values.append(node.args[0])
                elif len(node.args) > 1 and name == 'send_message':
                    values.append(node.args[1])
                for value in values:
                    text = literal_text(value)
                    if PROSE.search(text):
                        add(node, 'direct_foreign_display', text=text)
            if name == 'get_texts' and node.args and isinstance(node.args[0], ast.Constant):
                if node.args[0].value in {'ru', 'en', 'ua', 'zh'}:
                    add(node, 'fixed_non_fa_language', language=node.args[0].value)
        elif isinstance(node, ast.Attribute) and catalog_receiver(node.value) and node.attr.isupper():
            parent = parents.get(id(node))
            if not (isinstance(parent, ast.Call) and parent.func is node):
                check_key(node, node.attr)
        elif isinstance(node, ast.Subscript) and catalog_receiver(node.value):
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                check_key(node, node.slice.value)
            else:
                add(node, 'dynamic_key', expression=ast.unparse(node.slice))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in excluded_literals and re.search(r'[А-Яа-яЁё]', node.value):
                add(node, 'indirect_cyrillic_candidate', text=node.value[:250])
    return findings, references


def audit():
    catalog, duplicates, collisions = load_catalog(ROOT / 'app/localization/locales/fa.json')
    findings, errors, references = [], [], set()
    paths = sorted((ROOT / 'app').rglob('*.py'))
    for path in paths:
        try:
            items, keys = scan_file(path, catalog)
            findings.extend(items)
            references.update(keys)
        except (SyntaxError, UnicodeError) as error:
            errors.append({'path': path.relative_to(ROOT).as_posix(), 'error': str(error)})
    return {
        'scope': (
            'Static Python candidates only; indirect text, templates, stored content '
            'and rendered output require review.'
        ),
        'python_files': len(paths), 'fa_keys': len(catalog), 'referenced_static_keys': len(references),
        'duplicate_keys': duplicates, 'normalized_collisions': collisions, 'parse_errors': errors,
        'counts': dict(sorted(Counter(item['kind'] for item in findings).items())),
        'findings': sorted(findings, key=lambda item: (item['path'], item['line'], item['kind'])),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = audit()
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key != 'findings'}, ensure_ascii=False, indent=2))
    return 1 if result['parse_errors'] or result['duplicate_keys'] or result['normalized_collisions'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
