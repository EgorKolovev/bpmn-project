import os
import pytest

# Override database URL to use SQLite for tests
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test.db"
os.environ["ML_SERVICE_URL"] = "http://localhost:8001"
