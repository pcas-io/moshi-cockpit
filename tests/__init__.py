"""Unit tests. Lockfiles of strict keychain writes go to a temp dir, never into the real HOME."""

import os
import tempfile

os.environ.setdefault("MOSHI_COCKPIT_LOCK_DIR", tempfile.mkdtemp(prefix="moshi-cockpit-locks-"))
