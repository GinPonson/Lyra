-- Lyra DuckDB migration 9: expose transaction provenance in cashflow reads.
--
-- The canonical transactions table already stores source_type/source_reference.
-- Replacing the view is enough for existing databases and preserves all facts.

CREATE OR REPLACE VIEW v_cashflows AS
SELECT
    account_id,
    transaction_date,
    action,
    description,
    amount,
    source_type,
    source_reference,
    transaction_hash,
    ledger_version
FROM transactions
WHERE asset_type = 'cash';

INSERT INTO schema_migrations (version, description)
VALUES (9, 'cashflow transaction provenance view')
ON CONFLICT (version) DO NOTHING;
