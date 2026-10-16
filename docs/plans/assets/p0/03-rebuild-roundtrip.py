"""P0 定向验证（第三轮）：schema 15 → 16 的表重建往返，以及 FK 在 UPDATE 上的实际强度。"""
from __future__ import annotations
import duckdb

def probe(title, fn):
    try:
        r = fn()
        print(f"[ALLOW] {title}" + (f" -> {r}" if r is not None else ""))
    except Exception as exc:  # noqa: BLE001
        print(f"[REJECT] {title} -> {type(exc).__name__}: {str(exc).splitlines()[0][:150]}")

con = duckdb.connect()
U1="11111111-1111-1111-1111-111111111111"; U2="22222222-2222-2222-2222-222222222222"
A1="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

print("=== 步骤 1：构造 schema 15 形状（无 user 归属，全局唯一键）===")
con.execute("""
CREATE TABLE accounts (id UUID PRIMARY KEY, legacy_account_key VARCHAR NOT NULL, UNIQUE(legacy_account_key));
CREATE TABLE transactions (
    id UUID PRIMARY KEY,
    account_id UUID NOT NULL REFERENCES accounts(id),
    amount DECIMAL(18,4) NOT NULL
);
INSERT INTO accounts VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','ACC-1');
INSERT INTO transactions VALUES ('a0000000-0000-0000-0000-000000000001','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 100.5);
""")
print("   旧库 accounts/transactions 就绪")

print("\n=== 步骤 2：建 schema 16 新表（约束必须在 CREATE 时给全）===")
probe("建 users", lambda: con.execute(
    "CREATE TABLE users (id UUID PRIMARY KEY, display_name VARCHAR NOT NULL, status VARCHAR NOT NULL, role VARCHAR NOT NULL)"))
con.execute("INSERT INTO users VALUES (?,'A','active','member')",[U1])
con.execute("INSERT INTO users VALUES (?,'B','active','member')",[U2])
probe("建 accounts_v16（user_id NOT NULL + 用户内唯一 + UNIQUE(user_id,id)）", lambda: con.execute("""
    CREATE TABLE accounts_v16 (
        id UUID PRIMARY KEY,
        user_id UUID NOT NULL REFERENCES users(id),
        legacy_account_key VARCHAR NOT NULL,
        UNIQUE (user_id, legacy_account_key),
        UNIQUE (user_id, id)
    )"""))
probe("建 transactions_v16（复合外键绑定同属一个用户）", lambda: con.execute("""
    CREATE TABLE transactions_v16 (
        id UUID PRIMARY KEY,
        user_id UUID NOT NULL REFERENCES users(id),
        account_id UUID NOT NULL,
        amount DECIMAL(18,4) NOT NULL,
        FOREIGN KEY (user_id, account_id) REFERENCES accounts_v16(user_id, id)
    )"""))

print("\n=== 步骤 3：迁移数据 ===")
probe("拷贝 accounts 并归属 U1", lambda: con.execute(
    "INSERT INTO accounts_v16 SELECT id, ?, legacy_account_key FROM accounts",[U1]))
probe("拷贝 transactions 并归属 U1", lambda: con.execute(
    "INSERT INTO transactions_v16 SELECT id, ?, account_id, amount FROM transactions",[U1]))
probe("Decimal 精度保持", lambda: con.execute("SELECT amount FROM transactions_v16").fetchone())

print("\n=== 步骤 4：删除旧表（必须子先父后）===")
probe("先 DROP accounts（transactions 仍引用，应拒绝）", lambda: con.execute("DROP TABLE accounts"))
probe("先 DROP transactions", lambda: con.execute("DROP TABLE transactions"))
probe("再 DROP accounts", lambda: con.execute("DROP TABLE accounts"))

print("\n=== 步骤 5：重命名为最终名 ===")
probe("accounts_v16 -> accounts", lambda: con.execute("ALTER TABLE accounts_v16 RENAME TO accounts"))
probe("transactions_v16 -> transactions", lambda: con.execute("ALTER TABLE transactions_v16 RENAME TO transactions"))

print("\n=== 步骤 6：重命名后 FK 是否仍然生效 ===")
probe("插入 user 与 account 不匹配的 transaction（必须拒绝）", lambda: con.execute(
    "INSERT INTO transactions VALUES ('a0000000-0000-0000-0000-000000000002', ?, ?, 1.0)",[U2,A1]))
probe("插入 account 不存在的 transaction（必须拒绝）", lambda: con.execute(
    "INSERT INTO transactions VALUES ('a0000000-0000-0000-0000-000000000003', ?, ?, 1.0)",
    [U1,"99999999-9999-9999-9999-999999999999"]))
probe("插入归属正确的 transaction（应允许）", lambda: con.execute(
    "INSERT INTO transactions VALUES ('a0000000-0000-0000-0000-000000000004', ?, ?, 2.0)",[U1,A1]))

print("\n=== 步骤 7：FK 对 UPDATE 的强度（决定是否必须应用层校验）===")
probe("UPDATE transaction 的 account_id 改成不存在（是否被拦）", lambda: con.execute(
    "UPDATE transactions SET account_id='99999999-9999-9999-9999-999999999999' WHERE id='a0000000-0000-0000-0000-000000000004'"))
probe("UPDATE transaction 的 user_id 改成 U2（与 account 归属不符）", lambda: con.execute(
    "UPDATE transactions SET user_id=? WHERE id='a0000000-0000-0000-0000-000000000004'",[U2]))
print("   实际残留状态:", con.execute(
    "SELECT user_id, account_id FROM transactions WHERE id='a0000000-0000-0000-0000-000000000004'").fetchone())

print("\n=== 步骤 8：新增用户与身份表（原 owner 迁移后的自动注册路径）===")
probe("建 user_identities（全局唯一 provider+subject）", lambda: con.execute("""
    CREATE TABLE user_identities (
        id UUID PRIMARY KEY,
        user_id UUID NOT NULL REFERENCES users(id),
        provider VARCHAR NOT NULL,
        subject VARCHAR NOT NULL,
        UNIQUE (provider, subject)
    )"""))
probe("同身份并发注册用 ON CONFLICT DO NOTHING 收敛", lambda: con.execute(
    "INSERT INTO user_identities VALUES ('b0000000-0000-0000-0000-000000000001', ?, 'google','s1') ON CONFLICT DO NOTHING",[U1]))
probe("再次同身份（应不新增）", lambda: con.execute(
    "INSERT INTO user_identities VALUES ('b0000000-0000-0000-0000-000000000002', ?, 'google','s1') ON CONFLICT DO NOTHING",[U2]))
print("   身份行数 =", con.execute("SELECT count(*) FROM user_identities").fetchone()[0],
      "| 归属 =", con.execute("SELECT user_id FROM user_identities").fetchone()[0])

print("\n=== 步骤 9：停用用户（users 保持单表可变）===")
probe("UPDATE users SET status='disabled'（有设备/账户/交易引用）", lambda: con.execute(
    "UPDATE users SET status='disabled' WHERE id=?",[U1]))
print("   status =", con.execute("SELECT status FROM users WHERE id=?",[U1]).fetchone()[0])
probe("停用后是否阻止既有行读取（应不阻止，由应用层拒绝）", lambda: con.execute(
    "SELECT count(*) FROM transactions").fetchone())
