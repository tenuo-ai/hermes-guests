"""Render synthetic recording-bot messages for layout review, without contacting Telegram."""

from __future__ import annotations

import argparse
import html
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from telegram import Chat, Message  # noqa: E402

from hermes_guests.core import token_hash  # noqa: E402
from hermes_guests.ui import heading_entities  # noqa: E402
from tests.test_grouped_rules import SCHEMA  # noqa: E402
from tests.test_presentation import device_selection  # noqa: E402
from tests.test_telegram_journeys import TelegramJourneyTests  # noqa: E402


class PreviewJourney(TelegramJourneyTests):
    async def capture(self):
        original = self.service.tool_resolver
        self.service.tool_resolver = lambda name: (
            SCHEMA if name == SCHEMA["name"] else original(name)
        )
        await self.message(100, "hello")
        self.cards = [("greeting", self.bot.sent[-1])]
        proposal = self.service.prepare(
            "100",
            "Alex",
            {
                "tools": device_selection(),
                "duration_seconds": 3600,
            },
        )
        await self.plugin.proposal_card(proposal)
        self.cards.append(("review", self.bot.sent[-1]))
        await self.click(100, "hs:p:" + proposal["proposal_id"])
        token = self.bot.sent[-1][1].split("?start=guests_", 1)[1]
        await self.message(200, "/start guests_" + token)
        self.cards += [("pending", self.bot.sent[-2]), ("claim", self.bot.sent[-1])]
        await self.click(100, "hs:a:" + token_hash(token)[:24])
        self.cards.append(("welcome", self.bot.sent[-1]))
        await self.message(200, "help")
        self.cards.append(("permissions", self.bot.sent[-1]))
        long_name = "Alexandra 李 👩🏽\u200d💻 " + "Guest-With-A-Long-Display-Name-" * 2
        proposal = self.service.prepare(
            "100", long_name[:80], {"chat_only": True, "duration_seconds": 3600}
        )
        await self.plugin.proposal_card(proposal)
        self.cards.append(("long-name", self.bot.sent[-1]))
        await self.plugin.data_card("100")
        self.cards.append(("data-retention", self.bot.sent[-1]))
        await self.click(100, self.press("Keep 7 days"))
        self.cards.append(("retention-review", self.bot.sent[-1]))
        sid = self.service.guest("200")["id"]
        await self.click(100, "hs:v:" + sid)
        self.cards.append(("manage-data", self.bot.sent[-1]))
        await self.click(100, "hs:z:" + sid)
        self.cards.append(("delete-review", self.bot.sent[-1]))
        self.assertFalse(self.errors)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "output", nargs="?", type=Path, default=Path("/tmp/guests-telegram-preview.html")
    )
    parser.add_argument(
        "--reports", type=Path, help="Optional directory of synthetic real-model journey reports."
    )
    args = parser.parse_args()
    case = PreviewJourney("capture")
    result = unittest.TestResult()
    case.run(result)
    if not result.wasSuccessful():
        raise SystemExit(str(result.errors + result.failures))
    if args.reports:
        approval = next(card[2] for name, card in case.cards if name == "review")
        for path in sorted(args.reports.glob("final-*.json")):
            report = json.loads(path.read_text())
            if report.get("status") != "passed":
                raise SystemExit("Only completed synthetic model journeys can be previewed.")
            case.cards.append(("model-" + report["case"], (100, report["review"], approval)))
    cards = []
    for name, (_who, text, markup) in case.cards:
        rows = ""
        if markup:
            for row in markup.inline_keyboard:
                rows += (
                    '<div class="buttons">'
                    + "".join(
                        '<button type="button" disabled>' + html.escape(button.text) + "</button>"
                        for button in row
                    )
                    + "</div>"
                )
        message = Message(
            1,
            datetime.now(timezone.utc),
            Chat(100, "private"),
            text=text,
            entities=heading_entities(text),
        )
        cards.append(
            '<article id="'
            + name
            + '"><div class="message">'
            + message.text_html
            + "</div>"
            + rows
            + "</article>"
        )
    output = args.output
    options = "".join("<option>" + name + "</option>" for name, _ in case.cards)
    output.write_text(
        """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes Guests message layout preview</title>
<style>
* { box-sizing: border-box; }
body { margin: 0; font: 16px/1.4 system-ui, sans-serif; background: #dfe8e4; color: #17201d; }
header { background: #fff; padding: 12px; font-size: 13px; }
label { display: block; margin-top: 8px; }
select { max-width: 100%; font: inherit; padding: 8px; min-height: 44px; }
main { padding: 12px; max-width: 430px; margin: auto; }
article { display: none; min-width: 0; }
article.selected { display: block; }
.message { white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px; background: #fff;
  border-radius: 12px 12px 12px 2px; box-shadow: 0 1px 2px #0002; }
.buttons { display: flex; gap: 4px; margin-top: 4px; }
button { flex: 1; min-width: 0; min-height: 48px; padding: 10px 6px; border: 0;
  border-radius: 6px; background: #305b4d; color: #fff; font: inherit; opacity: 1;
  overflow-wrap: anywhere; }
</style><header>Layout approximation using recorded messages. Actual Telegram clients vary.
<label>Message <select aria-label="Preview message">"""
        + options
        + "</select></label></header><main>"
        + "".join(cards)
        + """</main><script>
const select = document.querySelector('select');
const params = new URLSearchParams(location.search);
select.value = params.get('card') || 'review';
document.documentElement.style.fontSize = (16 * Number(params.get('scale') || 1)) + 'px';
document.body.style.fontSize = '1rem';
function show() {
  document.querySelectorAll('article').forEach(card => card.classList.toggle('selected', card.id === select.value));
}
select.addEventListener('change', show);
show();
</script></html>""",
        encoding="utf-8",
    )
    print(output)


if __name__ == "__main__":
    main()
