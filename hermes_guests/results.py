"""Known native presentation cleanup; generic service data is otherwise unchanged."""

from __future__ import annotations

import copy
import re

FOOTER = re.compile(r"\n─{8} \[TRUNCATED\] ─{8}.*?─{29}", re.S)


def guest_result(tool, result):
    if tool != "web_extract" or not isinstance(result, dict):
        return result
    result = copy.deepcopy(result)
    for page in result.get("results", []):
        if not isinstance(page, dict):
            continue
        for key in ("content", "raw_content"):
            text = page.get(key)
            if isinstance(text, str):
                page[key] = FOOTER.sub(
                    "\n[This is a page excerpt. Ask a narrower question or use another public page; file and browser tools are not available.]",
                    text,
                ).replace(
                    "[... middle omitted — see footer ...]",
                    "[... middle omitted from this excerpt ...]",
                )
        error = page.get("error")
        if isinstance(error, str) and "Download it with the terminal" in error:
            page["error"] = (
                "This URL is a binary download rather than a readable page. Use a public HTML or supported document URL."
            )
    return result
