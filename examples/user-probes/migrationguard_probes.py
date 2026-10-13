"""User-defined behavioural probes (copy to your repository root as migrationguard_probes.py).

Every ``probe_*`` function is executed against the original code (Pydantic v1) and the
migrated code (Pydantic v2) in the sandbox; the return values must be identical. Use probes
for outputs whose *exact form* matters and that tests do not pin down: signatures, hashes,
cache keys, golden files, public API payloads.

This file is protected: agents can never modify it.
"""

import json

from webhooks.signing import parse_event, signing_payload

BODY = json.dumps({"eventId": "evt_probe", "type": "order.paid", "occurredAt": "2024-05-01T08:30:00+00:00",
                   "data": {"amount": 990, "currency": "EUR"}})


def probe_signing_payload():
    # receivers verify HMACs over this exact string
    return signing_payload(parse_event(BODY))
