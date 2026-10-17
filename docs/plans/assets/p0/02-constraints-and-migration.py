"""P0 定向验证（第二轮）：复合外键、迁移期表重建与约束变更。"""
from __future__ import annotations
import duckdb

def probe(title, fn):
    try:
        r = fn()
        print(f"[ALLOW] {title}" + (f" -> {r}" if r is not None else ""))
    except Exception as exc:  # noqa: BLE001
        print(f"[REJECT] {title} -> {type(exc).__name__}: {str(exc).splitlines()[0][:150]}")

print("duckdb", duckdb.__version__)
con = duckdb.connect()

con.execute("""
CREATE TABLE users (id UUID PRIMARY KEY, display_name VARCHAR NOT NULL, status VARCHAR NOT NULL);
CREATE TABLE accounts (
    id UUID PRIMARY KEY,
    legacy_account_key VARCHAR NOT NULL,
    UNIQUE (legacy_account_key)
);
""")
U1="11111111-1111-1111-1111-111111111111"; U2="22222222-2222-2222-2222-222222222222"
A1="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
con.execute("INSERT INTO users VALUES (?,'A','active')",[U1])
con.execute("INSERT INTO users VALUES (?,'B','active')",[U2])
con.execute("INSERT INTO accounts VALUES (?,'ACC-1')",[A1])

print("\n=== M1 迁移：给已有表加 NOT NULL 无默认值的列 ===")
probe("ADD COLUMN user_id UUID NOT NULL（表非空）", lambda: con.execute(
    "ALTER TABLE accounts ADD COLUMN user_id UUID NOT NULL"))
probe("ADD COLUMN user_id UUID（可空，表非空）", lambda: con.execute(
    "ALTER TABLE accounts ADD COLUMN user_id UUID"))
probe("回填 user_id", lambda: con.execute("UPDATE accounts SET user_id=? WHERE id=?",[U1,A1]))
probe("UPDATE 子表 FK 列指向另一个已存在的父（迁移归属改写）", lambda: con.execute(
    "UPDATE accounts SET user_id=? WHERE id=?",[U2,A1]))
probe("UPDATE 子表 FK 列指向不存在的父", lambda: con.execute(
    "UPDATE accounts SET user_id=? WHERE id=?",["99999999-9999-9999-9999-999999999999",A1]))

print("\n=== M2 已有表上增删约束 ===")
probe("ALTER TABLE accounts ADD CONSTRAINT fk_user FOREIGN KEY (user_id) REFERENCES users(id)",
      lambda: con.execute("ALTER TABLE accounts ADD CONSTRAINT fk_user FOREIGN KEY (user_id) REFERENCES users(id)"))
probe("ALTER TABLE accounts DROP CONSTRAINT accounts_legacy_account_key_key（旧全局唯一键）",
      lambda: con.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_legacy_account_key_key"))
probe("列出 accounts 约束", lambda: con.execute(
    "SELECT constraint_type, constraint_text FROM duckdb_constraints() WHERE table_name='accounts'").fetchall())

print("\n=== M3 复合外键：父表需 UNIQUE(user_id, id) ===")
probe("给 accounts 加 UNIQUE(user_id, id)", lambda: con.execute(
    "ALTER TABLE accounts ADD CONSTRAINT uq_accounts_user_id UNIQUE (user_id, id)"))
probe("建 import_files 复合外键指向 accounts(user_id, id)", lambda: con.execute("""
    CREATE TABLE import_files (
        id UUID PRIMARY KEY,
        user_id UUID NOT NULL,
        target_account_id UUID,
        FOREIGN KEY (user_id, target_account_id) REFERENCES accounts(user_id, id)
    )"""))
probe("同属 A 的 import_files（应允许）", lambda: con.execute(
    "INSERT INTO import_files VALUES ('cccccccc-cccc-cccc-cccc-cccccccccccc', ?, ?)",[U2,A1]))
probe("user=A 但 account 属 B（必须拒绝）", lambda: con.execute(
    "INSERT INTO import_files VALUES ('dddddddd-dddd-dddd-dddd-dddddddddddd', ?, ?)",[U1,A1]))

print("\n=== M4 表重建：重命名被引用的父表 ===")
probe("ALTER TABLE accounts RENAME TO accounts_old（有子表引用）", lambda: con.execute(
    "ALTER TABLE accounts RENAME TO accounts_old"))

print("\n=== M5 用户内唯一 legacy key，跨用户可同键 ===")
con2 = duckdb.connect()
con2.execute("""
CREATE TABLE users (id UUID PRIMARY KEY, status VARCHAR NOT NULL);
CREATE TABLE accounts (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    legacy_account_key VARCHAR NOT NULL,
    UNIQUE (user_id, legacy_account_key)
);""")
con2.execute("INSERT INTO users VALUES (?,'active')",[U1])
con2.execute("INSERT INTO users VALUES (?,'active')",[U2])
probe("A 用 ACC-1", lambda: con2.execute("INSERT INTO accounts VALUES (?,?,'ACC-1')",[A1,U1]))
probe("B 用同一个 ACC-1（必须允许）", lambda: con2.execute(
    "INSERT INTO accounts VALUES (?,?,'ACC-1')",["eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",U2]))
probe("A 再用一次 ACC-1（必须拒绝）", lambda: con2.execute(
    "INSERT INTO accounts VALUES (?,?,'ACC-1')",["ffffffff-ffff-ffff-ffff-ffffffffffff",U1]))

print("\n=== M6 有子引用的父表 DROP / CASCADE ===")
probe("DROP TABLE users（accounts 引用它）", lambda: con2.execute("DROP TABLE users"))
probe("DROP TABLE users CASCADE", lambda: con2.execute("DROP TABLE users CASCADE"))
probe("CASCADE 后 accounts 是否还在", lambda: con2.execute(
    "SELECT count(*) FROM duckdb_tables() WHERE table_name='accounts'").fetchone())

print("\n=== M7 单连接内并发事务语义 ===")
con3 = duckdb.connect()
con3.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT)")
con3.execute("BEGIN"); con3.execute("INSERT INTO t VALUES (1,10)")
probe("事务未提交时另一连接读同一内存库", lambda: "见下")
con3.execute("ROLLBACK")
probe("回滚后行数", lambda: con3.execute("SELECT count(*) FROM t").fetchone()[0])
