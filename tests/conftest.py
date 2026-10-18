"""Safe import-time defaults for agent modules in tests."""
import os

for _name in ("MARGIN_SEED", "ACADEMIC_SEED", "INBOX_SEED", "PLANNER_SEED", "LIFE_SEED"):
    os.environ.setdefault(_name, "test-placeholder-seed-not-for-production")
