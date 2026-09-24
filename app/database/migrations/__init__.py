"""Database migrations package.

Schema evolution lives here; `migrations/0001_initial.py` describes the baseline that
`app.database.models.DDL` creates. Migrations are applied by `JournalRepository.connect()`
for the baseline, and by `scripts/validate_config.py --migrate` for later versions.
"""
