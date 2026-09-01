-- Lyra DuckDB migration 8: per-account official import reminder settings.
--
-- A missing settings row means the documented default (enabled every month).
-- The last successful official import is derived from committed import metadata,
-- so Gmail activity cannot accidentally reset the reconciliation reminder.

CREATE TABLE IF NOT EXISTS account_import_reminder_settings (
    account_id       UUID PRIMARY KEY REFERENCES accounts(id),
    enabled          BOOLEAN NOT NULL DEFAULT true,
    interval_months  UTINYINT NOT NULL DEFAULT 1
                     CHECK (interval_months BETWEEN 1 AND 24),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);

CREATE OR REPLACE VIEW v_official_import_reminders AS
SELECT
    accounts.id AS account_id,
    coalesce(settings.enabled, true) AS enabled,
    coalesce(settings.interval_months, 1) AS interval_months,
    max(CASE
        WHEN imports.status = 'committed' THEN CAST(imports.updated_at AS DATE)
        ELSE NULL
    END) AS last_successful_import_date
FROM accounts
LEFT JOIN account_import_reminder_settings AS settings
    ON settings.account_id = accounts.id
LEFT JOIN import_files AS imports
    ON imports.target_account_id = accounts.id
GROUP BY accounts.id, settings.enabled, settings.interval_months;

INSERT INTO schema_migrations (version, description)
VALUES (8, 'per-account official import reminder settings')
ON CONFLICT (version) DO NOTHING;
