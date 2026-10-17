"""P0 定向验证：DuckDB 父子外键在「被引用父行更新」时的实际行为。

结论直接决定 users 的物理结构：是普通单表，还是项目既有的
「不可变身份表 + 可变状态表」模式。
"""
from __future__ import annotations
import duckdb

def probe(title: str, fn) -> None:
    try:
        fn()
        print(f"[ALLOW] {title}")
    except Exception as exc:  # noqa: BLE001 - 探针要报告真实行为
        print(f"[REJECT] {title} -> {type(exc).__name__}: {str(exc).splitlines()[0][:160]}")

print("duckdb", duckdb.__version__)
con = duckdb.connect()  # 内存库

# --- 基线：可变父表（状态直接放 users 上）-------------------------------
con.execute("""
CREATE TABLE users (
    id UUID PRIMARY KEY,
    display_name VARCHAR NOT NULL,
    status VARCHAR NOT NULL
);
CREATE TABLE paired_devices (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    fingerprint VARCHAR NOT NULL
);
CREATE TABLE ledger_versions (
    version BIGINT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id)
);
""")
U1 = "11111111-1111-1111-1111-111111111111"
con.execute("INSERT INTO users VALUES (?, 'A', 'active')", [U1])
con.execute("INSERT INTO paired_devices VALUES (?, ?, 'fp-1')", ["22222222-2222-2222-2222-222222222222", U1])
con.execute("INSERT INTO ledger_versions VALUES (1, ?)", [U1])

print("\n=== Q1 子表插入不存在的父 ===")
probe("INSERT 子行指向不存在的 user", lambda: con.execute(
    "INSERT INTO paired_devices VALUES (?, ?, 'fp-x')",
    ["33333333-3333-3333-3333-333333333333", "99999999-9999-9999-9999-999999999999"]))

print("\n=== Q2 父表 PK 更新（有子引用）===")
probe("UPDATE users SET id = <新值>（有设备与版本引用）", lambda: con.execute(
    "UPDATE users SET id = ? WHERE id = ?",
    ["44444444-4444-4444-4444-444444444444", U1]))

print("\n=== Q3 父表非键列更新（有子引用）===")
probe("UPDATE users SET status='disabled'（这是停用用户的核心操作）", lambda: con.execute(
    "UPDATE users SET status = 'disabled' WHERE id = ?", [U1]))
print("   status 现为:", con.execute("SELECT status FROM users WHERE id=?", [U1]).fetchone()[0])
con.execute("UPDATE users SET status = 'active' WHERE id = ?", [U1])

print("\n=== Q4 删除被引用的父行 ===")
probe("DELETE 有子引用的 user", lambda: con.execute("DELETE FROM users WHERE id = ?", [U1]))

print("\n=== Q5 事务回滚后 FK 与数据是否复原 ===")
con.execute("BEGIN")
con.execute("UPDATE users SET status='disabled' WHERE id=?", [U1])
con.execute("ROLLBACK")
print("   回滚后 status:", con.execute("SELECT status FROM users WHERE id=?", [U1]).fetchone()[0])

print("\n=== Q6 被引用列的列类型/约束能否 ALTER ===")
probe("ALTER TABLE users ALTER status TYPE VARCHAR", lambda: con.execute(
    "ALTER TABLE users ALTER status TYPE VARCHAR"))
probe("ALTER TABLE users ADD COLUMN role VARCHAR DEFAULT 'member'", lambda: con.execute(
    "ALTER TABLE users ADD COLUMN role VARCHAR DEFAULT 'member'"))
print("   加列后:", con.execute("SELECT id, status, role FROM users").fetchall())

print("\n=== Q7 唯一约束：(provider, subject) 全局唯一 + 并发收敛 ===")
con.execute("""
CREATE TABLE user_identities (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    provider VARCHAR NOT NULL,
    subject VARCHAR NOT NULL,
    UNIQUE (provider, subject)
);
""")
con.execute("INSERT INTO user_identities VALUES (?, ?, 'google', 'sub-1')", ["55555555-5555-5555-5555-555555555555", U1])
probe("同 (provider, subject) 再插一行", lambda: con.execute(
    "INSERT INTO user_identities VALUES (?, ?, 'google', 'sub-1')",
    ["66666666-6666-6666-6666-666666666666", U1]))
print("   ON CONFLICT DO NOTHING:", end=" ")
con.execute("INSERT INTO user_identities VALUES (?, ?, 'google', 'sub-1') ON CONFLICT DO NOTHING",
            ["77777777-7777-7777-7777-777777777777", U1])
print("身份行数 =", con.execute("SELECT count(*) FROM user_identities").fetchone()[0])

print("\n=== Q8 复合外键：子表须与父表同属一个用户 ===")
con.execute("""
CREATE TABLE accounts (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    legacy_account_key VARCHAR NOT NULL,
    UNIQUE (user_id, legacy_account_key)
);
CREATE TABLE import_files (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    target_account_id UUID,
    FOREIGN KEY (user_id, target_account_id) REFERENCES accounts(user_id, id)
);
""")
A1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
con.execute("INSERT INTO accounts VALUES (?, ?, 'ACC-1')", [A1, U1])
U2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
con.execute("INSERT INTO users VALUES (?, 'B', 'active')", [U2])
probe("import_files 的 user 与 target_account 同属（应允许）", lambda: con.execute(
    "INSERT INTO import_files VALUES (?, ?, ?)",
    ["cccccccc-cccc-cccc-cccc-cccccccccccc", U1, A1]))
probe("import_files user=B 但 target_account 属 A（必须拒绝）", lambda: con.execute(
    "INSERT INTO import_files VALUES (?, ?, ?)",
    ["dddddddd-dddd-dddd-dddd-dddddddddddd", U2, A1]))

print("\n=== Q9 用户内唯一 legacy_account_key，跨用户可同键 ===")
probe("B 用与 A 相同的 legacy_account_key（必须允许）", lambda: con.execute(
    "INSERT INTO accounts VALUES (?, ?, 'ACC-1')",
    ["eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", U2]))

print("\n=== Q10 有子引用的父表能否 DROP/重建（迁移路径）===")
probe("DROP TABLE users（有 FK 依赖）", lambda: con.execute("DROP TABLE users"))
probe("DROP TABLE users CASCADE", lambda: con.execute("DROP TABLE users CASCADE"))
