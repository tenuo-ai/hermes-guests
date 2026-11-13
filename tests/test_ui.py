"""Telegram emphasis preserves all scope text and uses Telegram's Unicode offsets."""

import unittest

from hermes_guests.ui import heading_entities


class PresentationTests(unittest.TestCase):
    def test_headers_use_utf16_offsets_with_unicode_names(self):
        text = 'Access for Alex 👩🏽‍💻 李\n\nConnected tools\nPermission group 1\n• Device — "B"\nEnds tomorrow\n'
        entities = heading_entities(text)
        encoded = text.encode("utf-16-le")
        self.assertEqual(
            [
                encoded[e.offset * 2 : (e.offset + e.length) * 2].decode("utf-16-le")
                for e in entities
            ],
            ["Access for Alex 👩🏽‍💻 李", "Connected tools", "Permission group 1", "Ends tomorrow"],
        )
        self.assertEqual(text.count('"B"'), 1)

    def test_continuation_fragment_is_not_treated_as_a_heading(self):
        text = "continuation of a long value\n\nAccess details\n• Scope is limited."
        entities = heading_entities(text, first_page=False)
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0].offset, len("continuation of a long value\n\n"))
