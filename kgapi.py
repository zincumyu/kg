# -*- coding: utf-8 -*-
"""
图谱查询辅助（给测试用）
========================

测试里原先写的 `KgNode.objects.filter(...)` 是 Django ORM 语法。
知识图谱模块改成独立包后没有 ORM，这里提供等价的简单查询函数，
让测试代码保持可读。

**这不是生产代码** —— 生产查询在 store.py / recommend.py 里。
本模块只服务测试，提供最常用的读操作。
"""

import db


# ----------------------------------------------------------------------
# 节点
# ----------------------------------------------------------------------
def node_count(node_type=None):
    """节点总数，可按类型过滤"""
    if node_type:
        r = db.query_one("SELECT COUNT(*) AS c FROM kg_node WHERE node_type=%s", (node_type,))
    else:
        r = db.query_one("SELECT COUNT(*) AS c FROM kg_node")
    return r["c"] if r else 0


def edge_count(relation=None):
    if relation:
        r = db.query_one("SELECT COUNT(*) AS c FROM kg_edge WHERE relation=%s", (relation,))
    else:
        r = db.query_one("SELECT COUNT(*) AS c FROM kg_edge")
    return r["c"] if r else 0


def alias_count():
    r = db.query_one("SELECT COUNT(*) AS c FROM kg_alias")
    return r["c"] if r else 0


def get_node(node_key):
    """按 node_key 取一行，没有则 None"""
    return db.query_one("SELECT * FROM kg_node WHERE node_key=%s", (node_key,))


def node_exists(node_key):
    return get_node(node_key) is not None


def get_nodes_by_name(name, node_type=None):
    sql = "SELECT * FROM kg_node WHERE name=%s"
    args = [name]
    if node_type:
        sql += " AND node_type=%s"
        args.append(node_type)
    return db.query(sql, tuple(args))


def all_nodes(node_type=None, limit=None):
    sql = "SELECT * FROM kg_node"
    args = ()
    if node_type:
        sql += " WHERE node_type=%s"
        args = (node_type,)
    if limit:
        sql += " LIMIT %s"
        args = args + (int(limit),)
    return db.query(sql, args)


def node_types():
    """返回 {node_type: count}"""
    rows = db.query("SELECT node_type, COUNT(*) AS c FROM kg_node GROUP BY node_type")
    return {r["node_type"]: r["c"] for r in rows}


# ----------------------------------------------------------------------
# 别名
# ----------------------------------------------------------------------
def alias_targets(alias):
    """某个口语表达指向的 node_key 列表"""
    rows = db.query("SELECT node_key FROM kg_alias WHERE alias=%s", (alias,))
    return [r["node_key"] for r in rows]


def alias_names(alias):
    """某个口语表达指向的节点名列表（便于断言）"""
    rows = db.query(
        "SELECT n.name FROM kg_alias a JOIN kg_node n ON n.node_key=a.node_key "
        "WHERE a.alias=%s", (alias,))
    return [r["name"] for r in rows]


def alias_target_counts():
    """{alias: 指向的节点数}"""
    rows = db.query("SELECT alias, COUNT(*) AS c FROM kg_alias GROUP BY alias")
    return {r["alias"]: r["c"] for r in rows}


def all_aliases_row():
    """全部 (alias, node_key) 行，用于批量校验"""
    return db.query("SELECT alias, node_key FROM kg_alias")


# ----------------------------------------------------------------------
# 边
# ----------------------------------------------------------------------
def out_edges(node_key, relation=None, limit=None):
    sql = "SELECT * FROM kg_edge WHERE src_key=%s"
    args = [node_key]
    if relation:
        sql += " AND relation=%s"
        args.append(relation)
    if limit:
        sql += " LIMIT %s"
        args.append(int(limit))
    return db.query(sql, tuple(args))


def in_edges(node_key, relation=None, limit=None):
    sql = "SELECT * FROM kg_edge WHERE dst_key=%s"
    args = [node_key]
    if relation:
        sql += " AND relation=%s"
        args.append(relation)
    if limit:
        sql += " LIMIT %s"
        args.append(int(limit))
    return db.query(sql, tuple(args))


def department_names():
    """所有科室节点名"""
    return [r["name"] for r in db.query(
        "SELECT name FROM kg_node WHERE node_type='department'")]


def pediatric_departments():
    """按名字粗筛儿科类科室（测试用，生产逻辑在 recommend.py）"""
    from recommend import is_pediatric_department
    return [n for n in department_names() if is_pediatric_department(n)]


# ----------------------------------------------------------------------
def counts():
    """三张表行数，等同 db.table_counts()"""
    return db.table_counts()
