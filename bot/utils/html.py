from __future__ import annotations

import re


# Символы, которые нужно экранировать в HTML
_HTML_ESCAPE = str.maketrans({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
})

# Максимальная длина одного Telegram-сообщения
TELEGRAM_MAX_LENGTH = 4096


def escape_html(text: str) -> str:
    return text.translate(_HTML_ESCAPE)


def split_long_message(text: str, max_len: int = TELEGRAM_MAX_LENGTH) -> list[str]:
    """Разбивает длинный текст на части, не разрывая слова."""
    if len(text) <= max_len:
        return [text]

    parts: list[str] = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        # Ищем последний перенос строки в допустимом диапазоне
        cut = text.rfind("\n", 0, max_len)
        if cut == -1:
            # Нет переноса — режем по пробелу
            cut = text.rfind(" ", 0, max_len)
        if cut == -1:
            # Нет пробела — режем жёстко
            cut = max_len
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts


def markdown_to_html(text: str) -> str:
    """
    Минимальная конвертация Markdown → HTML для Telegram (parse_mode=HTML).
    Обрабатывает: **bold**, *italic*, `code`, ```code block```, [link](url).
    """
    # Сначала экранируем HTML-спецсимволы вне блоков кода
    # Обрабатываем code blocks (```) — сохраняем как есть
    parts = re.split(r"(```[\s\S]*?```)", text)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            # Это блок кода
            code = part[3:-3].strip()
            # Убираем язык из первой строки если есть
            lines = code.split("\n", 1)
            if len(lines) > 1 and re.match(r"^\w+$", lines[0]):
                code = lines[1]
            result.append(f"<pre><code>{escape_html(code)}</code></pre>")
        else:
            p = escape_html(part)
            # inline code
            p = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", p)
            # bold
            p = re.sub(r"\*\*(.+?)\*\*", lambda m: f"<b>{m.group(1)}</b>", p)
            # italic
            p = re.sub(r"\*(.+?)\*", lambda m: f"<i>{m.group(1)}</i>", p)
            # links
            p = re.sub(
                r"\[([^\]]+)\]\((https?://[^\)]+)\)",
                lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
                p,
            )
            result.append(p)
    return "".join(result)
