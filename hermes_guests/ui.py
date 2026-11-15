"""Small native Telegram presentation helpers; never truncate a permission review."""

import re


def pages(text, limit=3400):
    if not isinstance(text, str):
        text = str(text)
    result = []
    chunk = []
    size = 0
    for char in text:
        units = len(char.encode("utf-16-le")) // 2
        if size + units > limit:
            result.append("".join(chunk))
            chunk = []
            size = 0
        chunk.append(char)
        size += units
    if chunk or not result:
        result.append("".join(chunk))
    return result


def deadline(value, zone):
    """Use the same local clock for approval, invitation and claim deadlines."""
    from .model import when

    return when(value, zone)


_DAY = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{1,2} [A-Z][a-z]{2} \d{4}$")


def heading_entities(text, *, first_page=True):
    """Native Telegram emphasis keeps scope text unchanged and readable by screen readers."""
    from telegram import MessageEntity

    headings = {
        "What they can do",
        "When it ends",
        "Good to know",
        "Allowed actions",
        "Access details",
    }
    result = []
    offset = 0
    for line in text.splitlines(keepends=True):
        title = line.rstrip("\r\n")
        length = len(title.encode("utf-16-le")) // 2
        if title and (
            (first_page and offset == 0)
            or title in headings
            or title.startswith("Permission group ")
            or _DAY.match(title)
        ):
            result.append(MessageEntity(MessageEntity.BOLD, offset, length))
        offset += len(line.encode("utf-16-le")) // 2
    return result
