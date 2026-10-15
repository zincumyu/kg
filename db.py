# -*- coding: utf-8 -*-
"""
MySQL 数据访问层
================

知识图谱模块**不依赖 Django**，直接连 MySQL。

为什么不用 Django ORM：
    本模块要能独立运行（构建数据、跑测试、给外部工具调用），
    绑在 Django app 上就必须先 django.setup()，且必须放在某个 app 目录里。
    直接用 pymysql 后，整个 tests/kg/ 可以拷到任何地方使用。

连接配置优先级：
    1. 本模块目录下的 .env        （tests/kg/.env，**推荐**，模块自带配置）
    2. 环境变量 DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME
    3. LLM_server/.env             （兼容：Django 那边的配置）
    4. 项目默认值（127.0.0.1:3306, root/1111, ai250301）

用法
----
    from db import query, execute, executemany, health

    rows = query("SELECT * FROM kg_node WHERE node_type=%s LIMIT 5", ("symptom",))
    n    = execute("UPDATE kg_node SET name=%s WHERE node_key=%s", ("x", "symptom:x"))
    n    = executemany("INSERT INTO kg_edge (src_key,dst_key,relation) VALUES (%s,%s,%s)", rows)
    print(health())
"""

import contextlib
import os
import queue
import sys
import logging

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 路径与 .env（统一由 paths.py 提供，不要在这里自己数目录层数）
# ----------------------------------------------------------------------
from paths import load_env      # noqa: E402

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "1111",
    "database": "ai250301",
}

_config = None


def _load_env_file():
    """
    把 .env 里的键值读进 os.environ。

    按 _ENV_CANDIDATES 顺序尝试，**第一个存在的**生效（不覆盖已设的环境变量）。
    """
    load_env()


def get_config():
    """解析数据库连接配置"""
    global _config
    if _config is not None:
        return _config

    _load_env_file()
    _config = {
        "host": os.getenv("DB_HOST") or DEFAULTS["host"],
        "port": int(os.getenv("DB_PORT") or DEFAULTS["port"]),
        "user": os.getenv("DB_USER") or DEFAULTS["user"],
        "password": os.getenv("DB_PASSWORD") or DEFAULTS["password"],
        "database": os.getenv("DB_NAME") or DEFAULTS["database"],
    }
    return _config


# ----------------------------------------------------------------------
# 连接管理（连接池）
# ----------------------------------------------------------------------
# ⚠️ 为什么用池而不是"全局单连接"
#
# 这里是给 Django 用的：`runserver` 默认多线程（每个请求一个线程），
# Celery 是 `-P eventlet --concurrency=8`。
# 曾经用「全局单连接 + 取连接时加锁」，但锁只保护了"取"这一步，
# 出了锁多个线程会共用同一个 pymysql 连接 —— 游标互相踩，
# 实测 8 线程 48 次查询只成功 6 次，报 Protocol error / Lost connection。
#
# 池的做法：借出连接 → 用完归还。同一时刻一个连接只属于一个调用者。
# 池满了就新建，归还时池满则直接关掉，所以连接数有上限、不会泄漏。

POOL_SIZE = 8                   # 池里最多缓存的空闲连接数
_POOL = queue.LifoQueue(maxsize=POOL_SIZE)   # LIFO：优先复用最近用过的热连接


def _new_connection():
    import pymysql
    import pymysql.cursors
    cfg = get_config()
    return pymysql.connect(
        host=cfg["host"], port=cfg["port"],
        user=cfg["user"], password=cfg["password"],
        database=cfg["database"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def _discard(conn):
    """关闭并从池里彻底丢掉一个连接（出过错的不再复用）"""
    try:
        conn.close()
    except Exception:
        pass


@contextlib.contextmanager
def _borrow():
    """借一个连接，用完自动归还。出异常则丢弃，不还池。"""
    try:
        conn = _POOL.get_nowait()
    except queue.Empty:
        conn = _new_connection()

    # 空闲久了可能已被 MySQL 断掉，ping 一下自动重连
    try:
        conn.ping(reconnect=True)
    except Exception:
        _discard(conn)
        conn = _new_connection()

    try:
        yield conn
    except Exception:
        _discard(conn)          # 出错说明连接状态可疑，别还回池里
        raise
    else:
        try:
            _POOL.put_nowait(conn)
        except queue.Full:
            _discard(conn)      # 池满了就关掉，避免连接数无限增长


def get_connection():
    """兼容老写法：返回一个可用连接。**用完请自己 close()，或改用 _borrow()**。"""
    return _new_connection()


def close():
    """关闭池里所有空闲连接（程序退出前可调；被借出的连接不受影响）"""
    while True:
        try:
            conn = _POOL.get_nowait()
        except queue.Empty:
            return
        _discard(conn)


# ----------------------------------------------------------------------
# 查询接口
# ----------------------------------------------------------------------
def query(sql, args=None, limit=None):
    """
    执行 SELECT，返回 list[dict]。

    limit 只用于**提示**（本函数不会自动加 LIMIT，因为 SQL 里可能已经有），
    实际限制请在 SQL 里写。
    """
    with _borrow() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            return list(cur.fetchall())


def query_one(sql, args=None):
    """执行 SELECT，返回第一行或 None"""
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql, args=None):
    """执行 INSERT/UPDATE/DELETE，返回受影响行数"""
    with _borrow() as conn:
        with conn.cursor() as cur:
            return cur.execute(sql, args or ())


def executemany(sql, rows):
    """批量执行，返回受影响行数"""
    rows = list(rows)
    if not rows:
        return 0
    with _borrow() as conn:
        with conn.cursor() as cur:
            return cur.executemany(sql, rows)


def insert_many(table, columns, rows, on_duplicate_ignore=True):
    """
    批量插入的语法糖。

    insert_many("kg_node", ["node_key","node_type","name"],
                [("symptom:腰痛","symptom","腰痛"), ...])
    """
    rows = list(rows)
    if not rows:
        return 0
    cols = ",".join("`%s`" % c for c in columns)
    ph = ",".join(["%s"] * len(columns))
    sql = "INSERT %s INTO `%s` (%s) VALUES (%s)" % (
        "IGNORE" if on_duplicate_ignore else "", table, cols, ph)
    return executemany(sql, rows)


# ----------------------------------------------------------------------
# 诊断
# ----------------------------------------------------------------------
def health():
    """返回 (是否可用, 描述)，用于自检与排错"""
    cfg = get_config()
    desc = "%(user)s@%(host)s:%(port)s/%(database)s" % cfg
    try:
        r = query_one("SELECT VERSION() AS v")
        return True, "%s  MySQL %s" % (desc, r["v"] if r else "?")
    except Exception as e:
        return False, "%s  连接失败: %s" % (desc, str(e)[:150])


def table_counts(tables=("kg_node", "kg_edge", "kg_alias")):
    """各表行数，用于快速确认数据是否导入"""
    out = {}
    for t in tables:
        try:
            r = query_one("SELECT COUNT(*) AS c FROM `%s`" % t)
            out[t] = r["c"] if r else 0
        except Exception as e:
            out[t] = "错误: %s" % str(e)[:60]
    return out


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ok, desc = health()
    print(("✅ " if ok else "❌ ") + desc)
    print()
    print("表行数:")
    for t, c in table_counts().items():
        print("    %-12s %s" % (t, c))
