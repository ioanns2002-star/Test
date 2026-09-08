"""Start an isolated relay preview without exposing a real PC or credentials."""

import os
import secrets

os.environ.setdefault("BRIDGE_DEVICE_TOKEN", secrets.token_urlsafe(32))
os.environ.setdefault("BRIDGE_CONTROLLER_TOKEN", secrets.token_urlsafe(32))
os.environ.setdefault("PORT", "3000")

from agentbridge.relay import main

main()
