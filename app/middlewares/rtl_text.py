"""RTL-нормализация исходящего текста: каждая строка выравнивается вправо.

Telegram не позволяет боту задавать направление текста (нет dir=rtl): направление
строки определяет первый сильный символ, поэтому строка, начинающаяся с латиницы,
цифры, эмодзи или символа-рамки, уходит влево. В начало каждой строки добавляем
RLM (U+200F), а уголки псевдографики зеркалим под RTL. Правим исходящий запрос,
а не каждый хендлер: меняем только text и caption, остальное уходит как прежде.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.methods import TelegramMethod


RLM = '‏'  # RIGHT-TO-LEFT MARK
_MIRROR = str.maketrans({'└': '┘', '┌': '┐', '├': '┤'})
_TEXT_FIELDS = ('text', 'caption')


def _to_rtl(value: str) -> str:
    lines = []
    for line in value.split('\n'):
        line = line.translate(_MIRROR)
        if line and not line.startswith(RLM):
            line = RLM + line
        lines.append(line)
    return '\n'.join(lines)


class RtlTextMiddleware(BaseRequestMiddleware):
    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[Any],
        bot: Bot,
        method: TelegramMethod[Any],
    ) -> Any:
        for field in _TEXT_FIELDS:
            value = getattr(method, field, None)
            if isinstance(value, str) and value:
                setattr(method, field, _to_rtl(value))
        return await make_request(bot, method)
