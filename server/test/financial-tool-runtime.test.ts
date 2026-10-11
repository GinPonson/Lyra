import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import {
  ApplicationError,
  buildImportCommitPlan,
  createFinancialToolRegistry,
  createImportPreview,
  type AgentAccountSummaryResult,
  type AgentCurrencyGroupedResult,
} from "../src/application/index.js";
import {
  FINANCIAL_TOOL_NAMES,
  type AgentToolContext,
} from "../src/domain/agent/index.js";
import type {
  AllocationResult,
  AttributionResult,
  DrawdownResult,
  HoldingPeriodResult,
  LedgerAuditResult,
  MonthlyPerformanceResult,
  OptionRiskResult,
  OptionStressResult,
  StreaksResult,
} from "../src/domain/analytics/index.js";
import type {
  PublicCashflowQueryResult,
  PublicExpiringPositionsResult,
  PublicPosition,
  PublicRealizedQueryResult,
  PublicTradeQueryResult,
} from "../src/application/index.js";
import type { RawTransactionFields } from "../src/domain/ledger/index.js";
import { openLyraDatabase, type LyraDatabase } from "../src/infrastructure/db/database.js";
import { persistImportCommit } from "../src/infrastructure/db/import-commit-repository.js";
import { MarketDataService } from "../src/application/market-data-service.js";
import type { MarketPositionView } from "../src/application/market-position-view.js";
import { marketInstrumentKey } from "../src/domain/market-data/types.js";

const USD_ACCOUNT_ID = "61000000-0000-4000-8000-000000000001";
const EUR_ACCOUNT_ID = "61000000-0000-4000-8000-000000000002";
const IMPORT_ID = "62000000-0000-4000-8000-000000000001";
const PARSER_VERSION = "schwab-csv-v1";

/** 建立两个币种账户，并通过正式导入仓储写入 USD 账户的真实账本和 FIFO。 */
async function seedFinancialLedger(database: LyraDatabase): Promise<void> {
  await database.connection.run(`
    INSERT INTO accounts (
      id, display_name, legacy_account_key, masked_suffix, base_currency,
      created_at, updated_at
    ) VALUES
      (
        CAST($usd_account_id AS UUID), 'Primary USD', 'SCHWAB-USD', '1001', 'USD',
        CAST('2026-08-31T08:00:00Z' AS TIMESTAMPTZ),
        CAST('2026-08-31T08:00:00Z' AS TIMESTAMPTZ)
      ),
      (
        CAST($eur_account_id AS UUID), 'Secondary EUR', 'SCHWAB-EUR', '2002', 'EUR',
        CAST('2026-08-31T08:01:00Z' AS TIMESTAMPTZ),
        CAST('2026-08-31T08:01:00Z' AS TIMESTAMPTZ)
      )
  `, { usd_account_id: USD_ACCOUNT_ID, eur_account_id: EUR_ACCOUNT_ID });

  const rows: readonly RawTransactionFields[] = [
    {
      Date: "08/01/2026",
      Action: "Buy to Open",
      Symbol: "DDD 09/15/2026 50.00 C",
      Description: "CALL DDD",
      Quantity: "1",
      Price: "$2.00",
      "Fees & Comm": "$0.65",
      Amount: "-$200.65",
    },
    {
      Date: "03/02/2026",
      Action: "Sell",
      Symbol: "AAA",
      Description: "AAA CORP",
      Quantity: "1",
      Price: "$12.00",
      "Fees & Comm": "",
      Amount: "$12.00",
    },
    {
      Date: "03/01/2026",
      Action: "Buy",
      Symbol: "AAA",
      Description: "AAA CORP",
      Quantity: "1",
      Price: "$10.00",
      "Fees & Comm": "",
      Amount: "-$10.00",
    },
    {
      Date: "01/01/2026",
      Action: "MoneyLink Transfer",
      Symbol: "",
      Description: "Deposit",
      Quantity: "",
      Price: "",
      "Fees & Comm": "",
      Amount: "$1000.00",
    },
  ];
  const preview = createImportPreview({
    batch: {
      fileName: "Individual_SCHWAB-USD_Transactions.csv",
      account: "SCHWAB-USD",
      sourceHash: "a".repeat(64),
      sourceType: "csv",
      sourceReference: IMPORT_ID,
      rows,
    },
    parserVersion: PARSER_VERSION,
    baseLedgerVersion: 0,
    existingTransactionHashes: new Set(),
  });
  const plan = buildImportCommitPlan(preview, []);
  await database.connection.run(`
    INSERT INTO import_files (
      id, file_hash, original_file_name, file_type, file_size_bytes, parser_version,
      status, target_account_id, preview_token_hash, preview_fingerprint,
      preview_expires_at, total_rows, created_at, updated_at
    ) VALUES (
      CAST($import_id AS UUID), repeat('a', 64), 'ledger.csv', 'csv', 512,
      $parser_version, 'preview_ready', CAST($account_id AS UUID), 'token-hash',
      $preview_fingerprint, CAST('2026-08-31T09:00:00Z' AS TIMESTAMPTZ), 4,
      CAST('2026-08-31T08:30:00Z' AS TIMESTAMPTZ),
      CAST('2026-08-31T08:30:00Z' AS TIMESTAMPTZ)
    )
  `, {
    import_id: IMPORT_ID,
    parser_version: PARSER_VERSION,
    account_id: USD_ACCOUNT_ID,
    preview_fingerprint: preview.previewFingerprint,
  });
  await persistImportCommit(database, {
    plan,
    importId: IMPORT_ID,
    accountId: USD_ACCOUNT_ID,
    parserVersion: PARSER_VERSION,
    previewFingerprint: preview.previewFingerprint,
    expectedLedgerVersion: 0,
    committedAt: new Date("2026-08-31T08:35:00Z"),
  });
}

/** 构造绑定首个账本版本的 Agent 工具上下文。 */
function context(accountScope: AgentToolContext["accountScope"]): AgentToolContext {
  return {
    accountScope,
    ledgerVersion: 1,
    asOf: "2026-08-31T12:00:00.000Z",
  };
}

describe("real DuckDB financial tool runtime", () => {
  it("从同一账本快照公开金融工具，外部行情采用独立注入", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-financial-tools-"));
    const database = await openLyraDatabase(join(directory, "ledger.duckdb"));
    try {
      await seedFinancialLedger(database);
      const registry = createFinancialToolRegistry(database);
      assert.deepEqual(registry.availableTools(), FINANCIAL_TOOL_NAMES);

      const accounts = await registry.invoke("accounts", {}, context({ kind: "all" })) as {
        rows: readonly { id: string; baseCurrency: string }[];
        count: number;
      };
      assert.equal(accounts.count, 2);
      assert.deepEqual(
        accounts.rows.map((account) => [account.id, account.baseCurrency]),
        [[USD_ACCOUNT_ID, "USD"], [EUR_ACCOUNT_ID, "EUR"]],
      );
      assert.doesNotMatch(JSON.stringify(accounts), /SCHWAB-(?:USD|EUR)/u);

      const summary = (await registry.invoke(
        "summary",
        {},
        context({ kind: "all" }),
      )) as AgentAccountSummaryResult;
      assert.equal(summary.aggregateCurrency, null);
      assert.deepEqual(summary.groups.map((group) => group.currency), ["EUR", "USD"]);
      assert.equal(summary.groups[0]?.data.transactionCount, 0);
      assert.equal(summary.groups[1]?.data.transactionCount, 4);
      assert.equal(summary.groups[1]?.data.netDeposits, "1000.0000");
      assert.equal(summary.groups[1]?.data.realizedPnlStock, "2.0000");
      assert.equal(summary.groups[1]?.data.openPositions, 1);

      const positions = await registry.invoke(
        "positions",
        { underlying: "ddd" },
        context({ kind: "all" }),
      ) as { rows: readonly PublicPosition[]; count: number; basis: string };
      assert.equal(positions.count, 1);
      assert.equal(positions.rows[0]?.accountId, USD_ACCOUNT_ID);
      assert.equal(positions.rows[0]?.cost, "2");
      assert.equal(positions.basis, "historical_cost_not_market_value");

      const expiring = await registry.invoke(
        "expiring",
        { days: 30 },
        context({ kind: "all" }),
      ) as PublicExpiringPositionsResult & { basis: string };
      assert.equal(expiring.count, 1);
      assert.equal(expiring.rows[0]?.daysRemaining, 15);
      assert.equal(expiring.rows[0]?.premiumDollars, "200");
      assert.equal(expiring.basis, "historical_premium_not_market_value");

      const trades = await registry.invoke(
        "trades",
        { symbol: "aaa", sourceType: "csv", limit: 10 },
        context({ kind: "all" }),
      ) as PublicTradeQueryResult;
      assert.equal(trades.count, 2);
      assert.deepEqual(trades.rows.map((row) => row.action), ["Sell", "Buy"]);
      assert.doesNotMatch(
        JSON.stringify(trades),
        /transactionHash|sourceReference|SCHWAB-USD/u,
      );

      const realized = await registry.invoke(
        "realized",
        { symbol: "AAA", fromDate: "2026-03-01", toDate: "2026-03-31" },
        context({ kind: "all" }),
      ) as PublicRealizedQueryResult;
      assert.equal(realized.totalCount, 1);
      assert.equal(realized.totalPnl, "2.0000");
      assert.equal(realized.rows[0]?.accountId, USD_ACCOUNT_ID);
      assert.doesNotMatch(JSON.stringify(realized), /Transaction|transaction_hash/u);

      const cashflow = await registry.invoke(
        "cashflow",
        { sourceType: "csv" },
        context({ kind: "all" }),
      ) as PublicCashflowQueryResult;
      assert.equal(cashflow.totalCount, 1);
      assert.equal(cashflow.total, "1000.0000");
      assert.deepEqual(cashflow.byAction, { "MoneyLink Transfer": "1000.0000" });

      const monthly = await registry.invoke(
        "monthly",
        { fromDate: "2026-03-01", toDate: "2026-03-31" },
        context({ kind: "all" }),
      ) as AgentCurrencyGroupedResult<MonthlyPerformanceResult>;
      assert.equal(monthly.groups[1]?.data.totalPnl, "2.0000");

      const allocation = await registry.invoke(
        "allocation",
        { groupBy: "asset-type" },
        context({ kind: "all" }),
      ) as AgentCurrencyGroupedResult<AllocationResult>;
      assert.equal(allocation.groups[1]?.data.totalCostBasis, "200.00");

      const attribution = await registry.invoke(
        "attribution",
        { groupBy: "underlying" },
        context({ kind: "all" }),
      ) as AgentCurrencyGroupedResult<AttributionResult>;
      assert.equal(attribution.groups[1]?.data.totalPnl, "2.0000");

      const drawdown = await registry.invoke(
        "drawdown",
        {},
        context({ kind: "all" }),
      ) as AgentCurrencyGroupedResult<DrawdownResult>;
      assert.equal(drawdown.groups[1]?.data.endingCumulativePnl, "2.0000");

      const risk = await registry.invoke(
        "risk",
        {},
        context({ kind: "all" }),
      ) as AgentCurrencyGroupedResult<OptionRiskResult>;
      assert.equal(risk.groups[1]?.data.totalLongPremiumPaid, "200.00");

      const holding = await registry.invoke(
        "holding-period",
        {},
        context({ kind: "all" }),
      ) as AgentCurrencyGroupedResult<HoldingPeriodResult>;
      assert.equal(holding.groups[1]?.data.rows[0]?.netPnl, "2.0000");

      const extremes = await registry.invoke(
        "extremes",
        { limit: 1 },
        context({ kind: "all" }),
      ) as AgentCurrencyGroupedResult<{
        rows: readonly { trade: { accountId: string; pnl: string } }[];
      }>;
      assert.equal(extremes.groups[1]?.data.rows[0]?.trade.accountId, USD_ACCOUNT_ID);
      assert.equal(extremes.groups[1]?.data.rows[0]?.trade.pnl, "2");
      assert.doesNotMatch(JSON.stringify(extremes), /closeTransaction|SCHWAB-USD/u);

      const streaks = await registry.invoke(
        "streaks",
        {},
        context({ kind: "all" }),
      ) as AgentCurrencyGroupedResult<StreaksResult>;
      assert.equal(streaks.groups[1]?.data.rows[0]?.kind, "win");

      const stress = await registry.invoke(
        "stress",
        {},
        context({ kind: "all" }),
      ) as AgentCurrencyGroupedResult<OptionStressResult>;
      assert.equal(stress.groups[1]?.data.allLongOptionsLossIfZero, "-200.00");

      const audit = await registry.invoke(
        "audit",
        {},
        context({ kind: "all" }),
      ) as LedgerAuditResult;
      assert.equal(audit.ok, true);
      assert.equal(audit.transactionCount, 4);
    } finally {
      database.close();
      await rm(directory, { recursive: true, force: true });
    }
  });

  it("账本推进后拒绝用旧 Agent 基线读取新快照", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-financial-stale-"));
    const database = await openLyraDatabase(join(directory, "ledger.duckdb"));
    try {
      await seedFinancialLedger(database);
      const registry = createFinancialToolRegistry(database);
      await assert.rejects(
        () => registry.invoke("positions", {}, {
          accountScope: { kind: "account", account: USD_ACCOUNT_ID },
          ledgerVersion: 0,
          asOf: "2026-08-31T12:00:00.000Z",
        }),
        (error: unknown) => {
          assert.ok(error instanceof ApplicationError);
          assert.equal(error.code, "LEDGER_VERSION_CONFLICT");
          assert.deepEqual(error.details, {
            expected_ledger_version: 0,
            actual_ledger_version: 1,
          });
          return true;
        },
      );
    } finally {
      database.close();
      await rm(directory, { recursive: true, force: true });
    }
  });

  it("App/Agent共享行情估值、先检查账本权限且刷新行情不改变历史结果", async () => {
    const directory = await mkdtemp(join(tmpdir(), "lyra-market-runtime-"));
    const database = await openLyraDatabase(join(directory, "ledger.duckdb"));
    try {
      await seedFinancialLedger(database);
      let calls = 0;
      const marketData = new MarketDataService({
        providers: [{
          id: "fixture", assetTypes: ["stock", "option"],
          fetchQuote: async (instrument) => {
            calls += 1;
            return { status: "available", quote: {
              instrumentKey: marketInstrumentKey(instrument), providerId: "fixture", feed: "delayed fixture",
              currency: "USD", bid: { value: "3", asOf: "2026-08-31T11:30:00.000Z" },
              ask: { value: "5", asOf: "2026-08-31T11:30:00.000Z" }, last: null, close: null,
              timing: { kind: "delayed", delayMinutes: 15 }, fetchedAt: "2026-08-31T12:00:00.000Z",
            } };
          },
        }],
        routes: { stock: ["fixture"], option: ["fixture"] },
        policies: { fixture: { cacheTtlMs: 60_000, maxRequestsPerMinute: 20, maxRequestsPerDay: null } },
        now: () => new Date("2026-08-31T12:00:00.000Z"),
      });
      const registry = createFinancialToolRegistry(database, marketData);
      const before = await registry.invoke("summary", {}, context({ kind: "all" }));
      for (const accountScope of [{ kind: "all" as const }, { kind: "account" as const, account: USD_ACCOUNT_ID }]) {
        const result = await registry.invoke("market-positions", {}, context(accountScope)) as MarketPositionView;
        assert.equal(result.rows[0]?.valuation.marketValue, "400");
        assert.equal(result.rows[0]?.valuation.unrealizedPnl, "200");
        assert.equal(result.rows[0]?.cost, "2");
        assert.equal(result.rows[0]?.quote.referencePrice?.asOf, "2026-08-31T11:30:00.000Z");
        assert.equal(result.rows[0]?.accountId, USD_ACCOUNT_ID);
        assert.doesNotMatch(JSON.stringify(result), /SCHWAB-USD|transactionHash|sourceReference/);
      }
      assert.equal(calls, 1);
      assert.deepEqual(await registry.invoke("summary", {}, context({ kind: "all" })), before);
      await assert.rejects(() => registry.invoke("market-positions", {}, { ...context({ kind: "all" }), ledgerVersion: 0 }), {
        code: "LEDGER_VERSION_CONFLICT",
      });
      await assert.rejects(() => registry.invoke("market-positions", {}, context({ kind: "account", account: "not-authorized" })));
      assert.equal(calls, 1);
      const eur = await registry.invoke("market-positions", {}, context({ kind: "account", account: EUR_ACCOUNT_ID })) as MarketPositionView;
      assert.equal(eur.count, 0);
      assert.equal(calls, 1);
    } finally {
      database.close();
      await rm(directory, { recursive: true, force: true });
    }
  });
});
