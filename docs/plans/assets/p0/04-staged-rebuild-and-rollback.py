"""P0 定向验证（第四轮）：确定 schema 15 -> 16 唯一可行的重建序列。

已确认的硬约束：
  F1 ADD COLUMN ... NOT NULL 在非空表上不支持
  F2 ALTER TABLE ADD/DROP CONSTRAINT 完全未实现
  F3 复合外键要求父表在 CREATE 时就有 UNIQUE(user_id, id)
  F4 被外键引用的父表不能 RENAME（DependencyException）
=> 新表必须以最终名字从零创建，旧表必须先整体拆除。
"""
from __future__ import annotations
import duckdb

def probe(title, fn):
    try:
        r = fn()
        print(f"[ALLOW] {title}" + (f" -> {r}" if r is not None else ""))
    except Exception as exc:  # noqa: BLE001
        print(f"[REJECT] {title} -> {type(exc).__name__}: {str(exc).splitlines()[0][:140]}")

U1="11111111-1111-1111-1111-111111111111"
A1="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
A2="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaab"
T1="a0000000-0000-0000-0000-000000000001"

def build_v15(con):
    """构造与 schema 15 同形的旧库：无用户归属、全局唯一键、带视图。"""
    con.execute("""
    CREATE TABLE accounts (id UUID PRIMARY KEY, legacy_account_key VARCHAR NOT NULL, UNIQUE(legacy_account_key));
    CREATE TABLE transactions (id UUID PRIMARY KEY, account_id UUID NOT NULL REFERENCES accounts(id),
                               amount DECIMAL(18,4) NOT NULL, trade_date DATE NOT NULL);
    CREATE VIEW cashflow AS SELECT t.id, t.amount, a.legacy_account_key FROM transactions t JOIN accounts a ON a.id=t.account_id;
    INSERT INTO accounts VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','ACC-1');
    INSERT INTO accounts VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaab','ACC-2');
    INSERT INTO transactions VALUES ('a0000000-0000-0000-0000-000000000001','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 100.5000, DATE '2026-01-02');
    INSERT INTO transactions VALUES ('a0000000-0000-0000-0000-000000000002','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaab', -25.2500, DATE '2026-01-03');
    """)

print("duckdb", duckdb.__version__)

print("\n=== A. CTAS 是否保真（UUID / DECIMAL / DATE）===")
con = duckdb.connect(); build_v15(con)
con.execute("CREATE TABLE _stage_transactions AS SELECT * FROM transactions")
probe("CTAS 类型保真", lambda: con.execute("DESCRIBE _stage_transactions").fetchall())
probe("CTAS 数值保真", lambda: con.execute("SELECT amount, trade_date FROM _stage_transactions ORDER BY amount").fetchall())

print("\n=== B. 视图是否阻止表重建 ===")
probe("DROP TABLE accounts（视图 cashflow 依赖）", lambda: con.execute("DROP TABLE accounts"))
probe("DROP VIEW cashflow", lambda: con.execute("DROP VIEW cashflow"))

print("\n=== C. 完整重建序列（在单个事务内）===")
con2 = duckdb.connect(); build_v15(con2)

def migrate(con):
    # 阶段 1：把全部业务数据搬到无外键的暂存表
    con.execute("CREATE TABLE _stage_accounts AS SELECT * FROM accounts")
    con.execute("CREATE TABLE _stage_transactions AS SELECT * FROM transactions")
    # 阶段 2：整体拆除旧图（先视图、再子表、后父表）
    con.execute("DROP VIEW cashflow")
    con.execute("DROP TABLE transactions")
    con.execute("DROP TABLE accounts")
    # 阶段 3：以最终名字建 schema 16（约束必须在 CREATE 时给全）
    con.execute("""
    CREATE TABLE users (id UUID PRIMARY KEY, display_name VARCHAR NOT NULL,
                        status VARCHAR NOT NULL, role VARCHAR NOT NULL);
    CREATE TABLE accounts (
        id UUID PRIMARY KEY,
        user_id UUID NOT NULL REFERENCES users(id),
        legacy_account_key VARCHAR NOT NULL,
        UNIQUE (user_id, legacy_account_key),
        UNIQUE (user_id, id)
    );
    CREATE TABLE transactions (
        id UUID PRIMARY KEY,
        user_id UUID NOT NULL REFERENCES users(id),
        account_id UUID NOT NULL,
        amount DECIMAL(18,4) NOT NULL,
        trade_date DATE NOT NULL,
        FOREIGN KEY (user_id, account_id) REFERENCES accounts(user_id, id)
    );
    CREATE VIEW cashflow AS SELECT t.id, t.amount, a.legacy_account_key
        FROM transactions t JOIN accounts a ON a.id = t.account_id;
    """)
    con.execute("INSERT INTO users VALUES (?, 'A', 'active', 'admin')", [U1])
    # 阶段 4：回填归属并装载
    con.execute("INSERT INTO accounts SELECT id, ?, legacy_account_key FROM _stage_accounts", [U1])
    con.execute("INSERT INTO transactions SELECT id, ?, account_id, amount, trade_date FROM _stage_transactions", [U1])
    con.execute("DROP TABLE _stage_accounts")
    con.execute("DROP TABLE _stage_transactions")

probe("事务内执行完整重建", lambda: (con2.execute("BEGIN"), migrate(con2), con2.execute("COMMIT")) and None)
probe("重建后 accounts", lambda: con2.execute("SELECT id, user_id, legacy_account_key FROM accounts ORDER BY legacy_account_key").fetchall())
probe("重建后 transactions（Decimal 保真）", lambda: con2.execute("SELECT amount, trade_date FROM transactions ORDER BY amount").fetchall())
probe("重建后视图可用", lambda: con2.execute("SELECT * FROM cashflow ORDER BY amount").fetchall())
probe("复合外键生效：user 与 account 不匹配", lambda: con2.execute(
    "INSERT INTO transactions VALUES ('a0000000-0000-0000-0000-000000000009', '22222222-2222-2222-2222-222222222222', ?, 1.0, DATE '2026-02-01')", [A1]))

print("\n=== D. 失败注入后能否回滚到旧库可用 ===")
con3 = duckdb.connect(); build_v15(con3)
def migrate_and_fail(con):
    con.execute("CREATE TABLE _stage_accounts AS SELECT * FROM accounts")
    con.execute("DROP VIEW cashflow")
    con.execute("DROP TABLE transactions")
    con.execute("DROP TABLE accounts")
    con.execute("""
    CREATE TABLE users (id UUID PRIMARY KEY, display_name VARCHAR NOT NULL, status VARCHAR NOT NULL, role VARCHAR NOT NULL);
    CREATE TABLE accounts (id UUID PRIMARY KEY, user_id UUID NOT NULL REFERENCES users(id),
                           legacy_account_key VARCHAR NOT NULL, UNIQUE(user_id, legacy_account_key), UNIQUE(user_id, id));
    """)
    raise RuntimeError("模拟迁移中途失败")

probe("故障注入迁移", lambda: (con3.execute("BEGIN"), migrate_and_fail(con3)))
con3.execute("ROLLBACK")
probe("回滚后旧 accounts 行数", lambda: con3.execute("SELECT count(*) FROM accounts").fetchone()[0])
probe("回滚后旧 transactions 行数", lambda: con3.execute("SELECT count(*) FROM transactions").fetchone()[0])
probe("回滚后旧视图可用", lambda: con3.execute("SELECT count(*) FROM cashflow").fetchone()[0])
probe("回滚后暂存表是否残留", lambda: con3.execute(
    "SELECT count(*) FROM duckdb_tables() WHERE table_name LIKE '_stage%'").fetchone()[0])
