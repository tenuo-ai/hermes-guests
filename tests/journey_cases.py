"""Synthetic services for manual real-model evaluations. They perform no external actions."""


def schema(name, description, properties):
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": list(properties),
        },
    }


SCHEMAS = {
    "device_control": schema(
        "device_control",
        "Control selected simulated home devices.",
        {"domain": {"type": "string"}, "device": {"type": "string"}, "action": {"type": "string"}},
    ),
    "device_state": schema(
        "device_state",
        "Read state and details of a selected device.",
        {"device": {"type": "string"}},
    ),
    "filing_lookup": schema(
        "filing_lookup",
        "Read public company filings.",
        {"ticker": {"type": "string"}, "form": {"type": "string"}, "limit": {"type": "integer"}},
    ),
    "calendar_events": schema(
        "calendar_events",
        "Read events from a selected calendar.",
        {
            "calendar_id": {"type": "string"},
            "include_private": {"type": "boolean"},
            "limit": {"type": "integer"},
        },
    ),
    "reminder_add": schema(
        "reminder_add",
        "Create a reminder in a selected external list.",
        {"list_id": {"type": "string"}, "text": {"type": "string"}},
    ),
    "library_find": schema(
        "library_find",
        "Search titles in a selected media library.",
        {"library": {"type": "string"}, "query": {"type": "string"}, "limit": {"type": "integer"}},
    ),
    "fixture_resources": schema(
        "fixture_resources", "List the available synthetic resources and action names.", {}
    ),
}
RESOURCES = {
    "devices": [
        {"id": "sensor.temperature", "name": "Indoor temperature", "domain": "sensor"},
        {"id": "lock.door_b", "name": "Door B", "domain": "lock", "actions": ["lock", "unlock"]},
    ]
    + [
        {
            "id": "light." + x,
            "name": "Light " + x.upper(),
            "domain": "light",
            "actions": ["turn_on", "turn_off"],
        }
        for x in "cde"
    ]
    + [
        {
            "id": "light.kitchen",
            "name": "Kitchen light",
            "domain": "light",
            "actions": ["turn_on", "turn_off"],
        }
    ],
    "calendars": [
        {"id": "calendar-family", "name": "Family calendar"},
        {"id": "calendar-work", "name": "Work calendar"},
    ],
    "reminder_lists": [
        {"id": "list-family", "name": "Family reminders"},
        {"id": "list-private", "name": "Private reminders"},
    ],
    "libraries": ["Movies", "Private videos"],
}
CASES = {
    "chat": {
        "request": "Let Alex chat with my assistant for one hour, with zero tools.",
        "guest": "Hello! What can you help me with?",
    },
    "research": {
        "request": "Let Alex read AAPL 10-K or 10-Q filings for one hour, with at most five results per request. Only share that filing lookup capability.",
        "tool": "filing_lookup",
        "args": {"ticker": "AAPL", "form": "10-K", "limit": 2},
        "bad": {"ticker": "MSFT", "form": "10-K", "limit": 2},
        "guest": "Find two AAPL 10-K filings.",
    },
    "calendar": {
        "request": "Let Alex read the Family calendar for one hour, excluding private events, with at most twenty events per request. Only share calendar reading.",
        "tool": "calendar_events",
        "args": {"calendar_id": "calendar-family", "include_private": False, "limit": 5},
        "bad": {"calendar_id": "calendar-work", "include_private": False, "limit": 5},
        "guest": "Show me five Family calendar events, excluding private events.",
    },
    "reminders": {
        "request": "Let Alex create reminders with any text in the Family reminders list for one hour. Only share reminder creation.",
        "tool": "reminder_add",
        "args": {"list_id": "list-family", "text": "Buy milk"},
        "bad": {"list_id": "list-private", "text": "Buy milk"},
        "guest": "Add a reminder to buy milk to the Family reminders list.",
    },
    "media": {
        "request": "Let Alex search the Movies library using any search terms for one hour, with at most five results per search. Only share media search.",
        "tool": "library_find",
        "args": {"library": "Movies", "query": "comedy", "limit": 2},
        "bad": {"library": "Private videos", "query": "comedy", "limit": 2},
        "guest": "Find two comedies in Movies.",
    },
    "devices": {
        "request": "Let Alex lock or unlock Door B and turn Lights C, D and E on or off for one hour. Only share these control operations.",
        "tool": "device_control",
        "args": {"domain": "lock", "device": "lock.door_b", "action": "unlock"},
        "bad": {"domain": "lock", "device": "light.c", "action": "unlock"},
        "guest": "Unlock Door B, please.",
    },
    "mixed_resources": {
        "request": "Let Alex read the Indoor temperature sensor, read Light C and switch Light C on or off for two days. Give Alex one day to accept the invitation. Only share these reading and light-control operations.",
        "tool": "device_state",
        "tools": ["device_state", "device_control"],
        "invitation_seconds": 86400,
        "args": {"device": "sensor.temperature"},
        "bad": {"device": "sensor.private"},
        "extra_allowed": [
            ("device_state", {"device": "light.c"}),
            ("device_control", {"domain": "light", "device": "light.c", "action": "turn_on"}),
        ],
        "extra_denied": [
            (
                "device_control",
                {"domain": "sensor", "device": "sensor.temperature", "action": "turn_on"},
            ),
            ("device_control", {"domain": "light", "device": "light.d", "action": "turn_on"}),
        ],
        "guest": "Read the indoor temperature and turn Light C on, please.",
        "guest_tools": ["device_state", "device_control"],
    },
}


CASES["research"]["extra_bad"] = [
    {"ticker": "AAPL", "form": "8-K", "limit": 2},
    {"ticker": "AAPL", "form": "10-K", "limit": 6},
]
CASES["calendar"]["extra_bad"] = [
    {"calendar_id": "calendar-family", "include_private": True, "limit": 5},
    {"calendar_id": "calendar-family", "include_private": False, "limit": 21},
]
CASES["media"]["extra_bad"] = [{"library": "Movies", "query": "comedy", "limit": 6}]
CASES["devices"]["extra_bad"] = [
    {"domain": "light", "device": "light.kitchen", "action": "turn_on"},
    {"domain": "light", "device": "lock.door_b", "action": "turn_on"},
]
CASES["calendar"]["denied_guest"] = "Show me the Work calendar instead."
