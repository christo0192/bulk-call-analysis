"""The one Claude Code version the app runs, and the built-in plugins that version ships.

The turn guard (stream.check_init) accepts only this version, because the checks it makes were
verified against it. Moving to a new version means checking its init report again (a signed-out run
shows it, using no allowance) and updating both values here.
"""

from __future__ import annotations

PINNED_CLAUDE_VERSION = "2.1.332"

# Loaded by default in 2.1.332 (its init report lists them); the app's settings switch each one off,
# so the guard's "no plugins" check still holds.
BUILTIN_PLUGINS = (
    "cc-plugin-agents-md@builtin",
    "cc-plugin-privacy@builtin",
    "cc-plugin-plugin-authoring@builtin",
)
