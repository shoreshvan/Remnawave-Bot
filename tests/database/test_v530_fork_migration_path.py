from alembic.config import Config
from alembic.script import ScriptDirectory


def test_upgrade_from_deployed_iranian_head_includes_all_upstream_steps():
    script = ScriptDirectory.from_config(Config('alembic.ini'))
    assert script.get_heads() == ['0144_iranian_v530']
    steps = script._upgrade_revs('heads', '0141_iranian_merge')
    assert [step.revision.revision for step in steps] == [
        '0141', '0142', '0143', '0144_iranian_v530',
    ]
