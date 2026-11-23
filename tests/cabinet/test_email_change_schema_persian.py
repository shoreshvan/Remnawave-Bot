"""Render real schema definitions without importing unrelated cabinet routes."""

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from pydantic import BaseModel, EmailStr, Field, ValidationError

from app.localization.texts import get_texts
from tests.fixtures.persian import persian  # noqa: F401


@pytest.fixture
def schemas(persian):
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/schemas/auth.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    names = {'EmailChangeRequest', 'EmailChangeVerifyRequest', 'EmailMergeVerifyRequest', 'EmailChangeResponse'}
    nodes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in names]
    context = dict(BaseModel=BaseModel, EmailStr=EmailStr, Field=Field, get_texts=get_texts)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), context)
    return context


@pytest.mark.parametrize('name', ['EmailChangeRequest', 'EmailChangeVerifyRequest', 'EmailMergeVerifyRequest', 'EmailChangeResponse'])
def test_schema_prose(schemas, name):
    schema = schemas[name].model_json_schema()
    for entry in [schema, *schema['properties'].values()]:
        for field in ['title', 'description']:
            assert any('\u0600' <= char <= '\u06ff' for char in entry[field])
            assert not any('a' <= char.lower() <= 'z' for char in entry[field])


@pytest.mark.parametrize('name', ['EmailChangeVerifyRequest', 'EmailMergeVerifyRequest'])
def test_code_contract(schemas, name):
    model = schemas[name]
    assert model(code='012345').code == '012345'
    for value in ['12345', '1234567', 'abcdef', 123456]:
        with pytest.raises(ValidationError):
            model(code=value)
    prop = model.model_json_schema()['properties']['code']
    assert prop['minLength'] == prop['maxLength'] == 6
    assert prop['pattern'] == r'^\d{6}$'


def test_email_contract(schemas):
    model = schemas['EmailChangeRequest']
    assert model(new_email='owner@example.com').new_email == 'owner@example.com'
    with pytest.raises(ValidationError):
        model(new_email='not-an-email')
    response = schemas['EmailChangeResponse'](message='ارسال نشد', new_email='owner@example.com', expires_in_minutes=0)
    assert response.model_dump() == {'message': 'ارسال نشد', 'new_email': 'owner@example.com', 'expires_in_minutes': 0}


def test_openapi_operation_metadata(schemas):
    path = Path(__file__).resolve().parents[2] / 'app/cabinet/routes/auth.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    app = FastAPI()
    count = 0
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not decorator.args:
                continue
            route = decorator.args[0]
            if not isinstance(route, ast.Constant) or not str(route.value).startswith('/email/change'):
                continue
            metadata = {kw.arg: ast.literal_eval(kw.value) for kw in decorator.keywords
                        if kw.arg in {'summary', 'description'}}
            async def endpoint():
                return {}
            app.add_api_route(route.value, endpoint, methods=[decorator.func.attr.upper()], **metadata)
            count += 1
    assert count == 4
    for operations in app.openapi()['paths'].values():
        for operation in operations.values():
            assert any('\u0600' <= char <= '\u06ff' for char in operation['summary'])
            assert any('\u0600' <= char <= '\u06ff' for char in operation['description'])
