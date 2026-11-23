import ast
from pathlib import Path


def test_migrations_have_unique_revisions_and_one_connected_head():
    directory = Path(__file__).resolve().parents[2] / 'migrations' / 'alembic' / 'versions'
    revisions = {}
    for path in directory.glob('*.py'):
        values = {}
        for node in ast.parse(path.read_text(encoding='utf-8')).body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            target = node.target if isinstance(node, ast.AnnAssign) else node.targets[0]
            if isinstance(target, ast.Name) and target.id in {'revision', 'down_revision'}:
                values[target.id] = ast.literal_eval(node.value)
        if 'revision' not in values:
            continue
        revision = values['revision']
        assert revision not in revisions, f'Duplicate revision {revision}: {path}'
        parents = values.get('down_revision')
        revisions[revision] = (parents,) if isinstance(parents, str) else tuple(parents or ())

    all_parents = {parent for parents in revisions.values() for parent in parents}
    assert all_parents <= revisions.keys()
    heads = revisions.keys() - all_parents
    assert len(heads) == 1, heads
    visited = set()
    active = set()

    def visit(revision):
        assert revision not in active, f'Migration cycle at {revision}'
        if revision in visited:
            return
        active.add(revision)
        for parent in revisions[revision]:
            visit(parent)
        active.remove(revision)
        visited.add(revision)

    visit(next(iter(heads)))
    assert visited == revisions.keys()
