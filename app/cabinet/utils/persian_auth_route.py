"""Localize validation failures on reviewed cabinet authentication routes."""

from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.routing import APIRoute

from app.cabinet.utils.request_language import get_request_language
from app.localization.texts import get_texts


class PersianAuthRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        account_link = '/auth/account/link/' in self.path or '/auth/account/unlink/' in self.path
        if not account_link and not self.path.endswith(('/auth/email/change', '/auth/email/change/verify',
                                   '/auth/email/change/cancel', '/auth/email/change/status', '/auth/deeplink/poll')):
            return handler

        async def localized(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                if get_request_language(request) != 'fa':
                    raise
                texts = get_texts('fa')
                errors = []
                for original in exc.errors():
                    error = dict(original)
                    kind = error['type']
                    field = error.get('loc', ())[-1:]
                    if kind == 'missing':
                        key = 'CABINET_INPUT_REQUIRED'
                    elif kind == 'json_invalid':
                        key = 'CABINET_INPUT_JSON_INVALID'
                    elif account_link:
                        reason = str(error.get('ctx', {}).get('error', ''))
                        if reason.startswith('Provide exactly one of:'):
                            key = 'CABINET_LINK_INPUT_MULTIPLE_METHODS'
                        elif reason.startswith('Provide one of:'):
                            key = 'CABINET_LINK_TELEGRAM_PROVIDE_ONE'
                        elif reason == 'Login Widget mode requires id, auth_date, and hash fields':
                            key = 'CABINET_LINK_INPUT_WIDGET_INCOMPLETE'
                        elif field == ('provider',):
                            key = 'CABINET_LINK_INPUT_PROVIDER_INVALID'
                        elif field == ('code',):
                            key = 'CABINET_LINK_INPUT_CODE_INVALID'
                        elif field == ('state',):
                            key = 'CABINET_LINK_INPUT_STATE_INVALID'
                        elif kind in {'int_parsing', 'int_type', 'int_from_float'}:
                            key = 'CABINET_LINK_INPUT_INTEGER_REQUIRED'
                        elif kind == 'string_too_long':
                            key = 'CABINET_LINK_INPUT_TOO_LONG'
                        elif field == ('hash',):
                            key = 'CABINET_LINK_INPUT_HASH_INVALID'
                        elif kind == 'string_type':
                            key = 'CABINET_LINK_INPUT_TEXT_REQUIRED'
                        else:
                            key = 'CABINET_INPUT_BODY_INVALID'
                    elif field == ('new_email',):
                        key = 'CABINET_INPUT_EMAIL_INVALID'
                    elif field == ('code',):
                        key = 'CABINET_INPUT_CODE_INVALID'
                    elif field == ('token',):
                        key = 'CABINET_INPUT_DEEPLINK_TOKEN_INVALID'
                    elif field == ('campaign_slug',):
                        key = 'CABINET_INPUT_CAMPAIGN_INVALID'
                    else:
                        key = 'CABINET_INPUT_BODY_INVALID'
                    error['msg'] = texts.t(key, 'Invalid request input')
                    if key == 'CABINET_LINK_INPUT_TOO_LONG':
                        error['msg'] = error['msg'].format(limit=error['ctx']['max_length'])
                    if 'ctx' in error:
                        error['ctx'] = dict(error['ctx'])
                        for name in ('reason', 'error'):
                            if name in error['ctx']:
                                error['ctx'][name] = error['msg']
                    errors.append(error)
                return await request_validation_exception_handler(
                    request, RequestValidationError(errors, body=exc.body)
                )

        return localized
