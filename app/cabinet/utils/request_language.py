"""Resolve the display language before an account can be authenticated."""

from app.config import settings


def get_request_language(request) -> str:
    supported = {'fa', 'ru', 'en', 'ua', 'zh'}
    candidates = []
    for position, item in enumerate(request.headers.get('Accept-Language', '').split(',')):
        tag, *parameters = item.strip().split(';')
        language = tag.lower().replace('_', '-').split('-')[0]
        language = 'ua' if language == 'uk' else language
        quality = 1.0
        try:
            for parameter in parameters:
                name, _, value = parameter.strip().partition('=')
                if name == 'q':
                    quality = float(value)
        except ValueError:
            continue
        if language in supported and 0 < quality <= 1:
            candidates.append((quality, -position, language))
    return max(candidates)[2] if candidates else settings.DEFAULT_LANGUAGE
