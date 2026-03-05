import os
import sys

import pymysql
from dotenv import load_dotenv


def main():
    load_dotenv()

    host = os.getenv("MYSQL_HOST", "127.0.0.1")
    port = int(os.getenv("MYSQL_PORT", "3306"))
    user = os.getenv("MYSQL_USER", "root")
    password = os.getenv("MYSQL_PASSWORD", "")
    db = os.getenv("MYSQL_DB", "")

    print("=== 当前读取到的配置 ===")
    print(f"MYSQL_HOST={host}")
    print(f"MYSQL_PORT={port}")
    print(f"MYSQL_USER={user}")
    print(f"MYSQL_DB={db}")
    print("")

    try:
        conn = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            charset="utf8mb4",
            connect_timeout=10,
        )
    except Exception as exc:
        print(f"[失败] 连接 MySQL 失败: {exc}")
        sys.exit(1)

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT @@hostname, @@port")
            server = cur.fetchone()
            print(f"server={server}")

            # 注意：LIKE 中下划线是通配符，不能用于库名精确判断
            cur.execute(
                "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = %s",
                (db,),
            )
            rows = cur.fetchall()
            print(f"db_exact={rows}")

            if rows:
                print(f"[成功] 数据库 {db} 可见")
            else:
                print(f"[失败] 数据库 {db} 不可见（不存在或无权限）")
                cur.execute("SHOW DATABASES")
                all_dbs = [r[0] for r in cur.fetchall()]
                similar = [name for name in all_dbs if db.replace("_", "-") in name or db.replace("-", "_") in name]
                if similar:
                    print(f"[提示] 相近库名: {similar}")
                sys.exit(2)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
