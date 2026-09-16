from __future__ import annotations

import os


# Production code requires an explicit environment.  Pytest establishes its
# own isolated runtime boundary before importing application modules.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("AIRMA_WORKER_ENVIRONMENT", "test")
