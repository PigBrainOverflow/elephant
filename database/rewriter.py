from __future__ import annotations
import sqlite3
import json

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .db import NetlistDatabase


def rewrite_dffe_pn_to_pp(netlist: NetlistDatabase) -> bool:
    cur = netlist.cursor()
    cur.execute("""
        SELECT d, c, e, q
        FROM dffe_xx
        WHERE type = "$_DFFE_PN_";
    """)
    res = cur.fetchall()
    if not res:
        return False
    i = netlist.get_next_id()
    for d, c, e, q in res:
        # check whether !e exists
        cur.execute("SELECT y FROM unary_gate WHERE a = ? AND type = \"$_NOT_\";", (e,))
        yt = cur.fetchone()
        if yt:
            ne = yt[0]
        else:   # !e does not exist, create it
            ne = i
            i += 1
            cur.execute("INSERT INTO unary_gate VALUES (?, ?, ?);", (e, ne, "$_NOT_"))
            cur.execute("INSERT INTO wire VALUES (?, 1);", (ne,))
        # update the dffe_pn to dffe_pp
        cur.execute("""
            UPDATE dffe_xx
            SET type = "$_DFFE_PP_", e = ?
            WHERE type = "$_DFFE_PN_" AND d = ? AND c = ? AND e = ? AND q = ?;
            """, (ne, d, c, e, q)
        )
    netlist.commit()
    return True

def saturate_comm(netlist: NetlistDatabase, target_type: str) -> int:
    # It finds all commutative binary gates and saturates them.
    cur = netlist.cursor()
    cur.execute("""
        SELECT b, a, y, type
        FROM binary_gate
        WHERE type = ?;
        """, (target_type,)
    )
    res = cur.fetchall()
    cur.executemany("INSERT OR IGNORE INTO binary_gate VALUES (?, ?, ?, ?);", res)
    netlist.commit()
    return cur.rowcount

def saturate_demorgan(netlist: NetlistDatabase, target_type: str, to_type: str) -> bool:
    # It finds the pattern: !(a & b) -> !a | !b. One at a time.
    cur = netlist.cursor()
    cur.execute("""
        SELECT bg.a, bg.b, bg.y
        FROM binary_gate AS bg JOIN unary_gate AS ug
        ON bg.y = ug.a
        WHERE bg.type = ? AND ug.type = ?
        LIMIT 1;
        """, (target_type, "$_NOT_")
    )
    res = cur.fetchone()
    if not res:
        return False
    i = netlist.get_next_id()
    a, b, y = res
    # check whether !a already exists
    cur.execute("SELECT y FROM unary_gate WHERE a = ? AND type = ?;", (a, "$_NOT_"))
    yt = cur.fetchone()
    if yt:
        na = yt[0]
    else:
        na = i
        i += 1
        cur.execute("INSERT INTO unary_gate VALUES (?, ?, ?);", (a, na, "$_NOT_"))
        cur.execute("INSERT INTO wire VALUES (?, 1);", (na,))
    # check whether !b already exists
    cur.execute("SELECT y FROM unary_gate WHERE a = ? AND type = ?;", (b, "$_NOT_"))
    yt = cur.fetchone()
    if yt:
        nb = yt[0]
    else:
        nb = i
        cur.execute("INSERT INTO unary_gate VALUES (?, ?, ?);", (b, nb, "$_NOT_"))
        cur.execute("INSERT INTO wire VALUES (?, 1);", (nb,))
    # check whether the output already exists
    cur.execute("SELECT y FROM binary_gate WHERE a = ? AND b = ? AND type = ?;", (na, nb, to_type))
    yt = cur.fetchone()
    if yt:
        return False
    else:
        # construct a new or
        cur.execute("INSERT INTO binary_gate VALUES (?, ?, ?, ?);", (na, nb, y, to_type))
    netlist.commit()
    return True

def saturate_2_1_mux(netlist: NetlistDatabase) -> int:
    # It finds the pattern: s'a + sb.
    cur = netlist.cursor()
    cur.execute("""
        SELECT and1.b, and2.b, and2.a, or1.y
        FROM binary_gate AS and1 JOIN binary_gate AS and2 JOIN binary_gate AS or1 JOIN unary_gate AS not1
        ON and1.a = not1.y AND and2.a = not1.a AND or1.a = and1.y AND or1.b = and2.y
        WHERE and1.type = "$_AND_" AND and2.type = "$_AND_" AND or1.type = "$_OR_" AND not1.type = "$_NOT_"
    """)
    res = cur.fetchall()
    cur.executemany("INSERT OR IGNORE INTO mux VALUES (?, ?, ?, ?);", res)
    netlist.commit()
    return cur.rowcount

# We can only consider muxes that are connected (directly or indirectly) to dffes.
# NOTE: Write port is now extracted heuristically instead of repairing.
def rewrite_mux_to_qmux(netlist: NetlistDatabase) -> int:
    # Qmux is a mux with inputs connected (directly) to dffes.
    # We can safely remove the original mux.
    cur = netlist.cursor()
    cur.execute("""
        SELECT mux.a, mux.b, mux.s, mux.y, d1.c, d1.type
        FROM mux JOIN dffe_xx AS d1 JOIN dffe_xx AS d2
        ON a = d1.q AND b = d2.q AND d1.c = d2.c AND d1.type = d2.type;
    """)
    res = cur.fetchall()
    if not res:
        return 0
    qmuxes = [
        (c, json.dumps([a, b]), json.dumps([s]), y, dffe_type)
        for a, b, s, y, c, dffe_type in res
    ]
    cur.executemany("INSERT INTO qmux VALUES (?, ?, ?, ?, ?);", qmuxes)
    cur.executemany(
        "DELETE FROM mux WHERE a = ? AND b = ? AND s = ?;",
        [(a, b, s) for a, b, s, _, _, _ in res]
    )
    netlist.commit()
    return cur.rowcount

def reduce_qmux_once(netlist: NetlistDatabase) -> int:
    # NOTE: This function keeps the original qmuxes.
    cur = netlist.cursor()
    cur.execute("""
        SELECT qm1.qs, qm2.qs, qm1.ss, mux.s, mux.y, qm1.c, qm1.dffe_type
        FROM qmux AS qm1 JOIN qmux AS qm2 JOIN mux
        ON qm1.c = qm2.c AND qm1.ss = qm2.ss AND qm1.y = mux.a AND qm2.y = mux.b AND qm1.dffe_type = qm2.dffe_type;
    """)
    res = cur.fetchall()
    if not res:
        return 0
    qmuxes = []
    for qs1, qs2, ss, s, y, c, dffe_type in res:
        qs1, qs2, ss = json.loads(qs1), json.loads(qs2), json.loads(ss)
        qmuxes.append((c, json.dumps(qs1 + qs2), json.dumps(ss + [s]), y, dffe_type))
    cur.executemany("INSERT OR IGNORE INTO qmux VALUES (?, ?, ?, ?, ?);", qmuxes)
    netlist.commit()
    return cur.rowcount

def subset(a: tuple, b: tuple) -> bool:
    # if b is a subset of a
    return all(x in a for x in b)

def find_readport(netlist: NetlistDatabase) -> dict[tuple[tuple[tuple[int]], tuple[int]], tuple[int]]:
    # (((q)), (ra)) -> (rd)
    # ((1, 2), (3, 4)) means q1, q2 -> rd1 & q3, q4 -> rd2
    readports = {}
    cur = netlist.cursor()
    # width >= 4
    # order by log(height) approximately
    cur.execute("SELECT c, ss, dffe_type FROM qmux GROUP BY c, ss, dffe_type HAVING COUNT(*) >= 4 ORDER BY LENGTH(ss) DESC;")
    groups = cur.fetchall()
    for c, ss, dffe_type in groups:
        # check whether ss is a subset of existing readports
        ss_tuple = tuple(json.loads(ss))
        if any(subset(ra, ss_tuple) for _, ra in readports.keys()):
            continue
        cur.execute("SELECT qs, y FROM qmux WHERE c = ? AND ss = ? AND dffe_type = ?;", (c, ss, dffe_type))
        patterns = cur.fetchall()
        qss = tuple(tuple(json.loads(qs)) for qs, _ in patterns)
        rd = tuple(y for _, y in patterns)
        readports[(qss, ss_tuple)] = rd
    return readports


def find_memory(readports: dict[tuple[tuple[tuple[int]], tuple[int]], tuple[int]]) -> dict[tuple[tuple[int]], list[tuple[tuple[tuple[int]], tuple[int], tuple[int]]]]:
    memories = {}
    for (qs, ra), rd in readports.items():
        found = False
        for mqs in memories.keys():
            if subset(mqs, qs):
                memories[mqs].append((qs, rd, ra))
                found = True
                break
        if not found:
            memories[qs] = [(qs, rd, ra)]
    return memories


def find_d_e_from_q(netlist: NetlistDatabase, q: int) -> tuple[int, int] | None:
    cur = netlist.cursor()
    cur.execute("SELECT d, e FROM dffe_xx WHERE q = ? LIMIT 1;", (q,))
    res = cur.fetchone()
    return res if res else None

# to support unbalanced muxes

def rewrite_mux_to_quasi_qmux(netlist: NetlistDatabase) -> int:
    # quasi_qmux is a mux with inputs connected to dffes or quasi_qmuxes or 0
    # we can safely remove the original mux
    cur = netlist.cursor()
    # add a const 0 dffe
    cur.execute(
        "INSERT OR IGNORE INTO dffe_xx VALUES (?, ?, ?, ?, ?);",
        (0, 2, 0, 0, "$_DFFE_PP_")
    )
    cur.execute(
        """
        SELECT mux.a, mux.b, mux.s, mux.y, d1.c, d1.type
        FROM mux JOIN dffe_xx AS d1 JOIN dffe_xx AS d2
        ON a = d1.q AND b = d2.q AND d1.c = d2.c AND d1.type = d2.type;
        """
    )
    patterns = cur.fetchall()
    if not patterns:
        return 0
    quasi_qmuxes = [
        (c, json.dumps([a, b]), json.dumps([s, None, None]), y, dffe_type)
        for a, b, s, y, c, dffe_type in patterns
    ]
    cur.executemany("INSERT INTO quasi_qmux VALUES (?, ?, ?, ?, ?);", quasi_qmuxes)
    cur.executemany(
        "DELETE FROM mux WHERE a = ? AND b = ? AND s = ?;",
        [(a, b, s) for a, b, s, _, _, _ in patterns]
    )
    netlist.commit()
    # print(quasi_qmuxes)
    return cur.rowcount


def reduce_quasi_qmux_once(netlist: NetlistDatabase) -> int:
    # this keeps the original quasi_qmuxes
    cur = netlist.cursor()
    cur.execute(
        """
        SELECT dff.q, dff.c, dff.type, qm.qs, qm.ss, m.s, m.y
        FROM dffe_xx AS dff JOIN quasi_qmux AS qm JOIN mux AS m
        ON dff.q = m.b AND dff.c = qm.c AND qm.y = m.a AND dff.type = qm.dffe_type;
        """
    )
    patterns = cur.fetchall()
    if not patterns:
        return 0
    quasi_qmuxes = []
    for q, c, dffe_type, qs, ss, s, y in patterns:
        qs = json.loads(qs)
        ss = json.loads(ss)
        new_qs = qs + [q]
        new_ss = [s] + ss + [None]
        # new_qs = [qs, q]    # left: qs, right: q
        # new_ss = [s, ss, None]  # left: s, right: ss
        quasi_qmuxes.append((c, json.dumps(new_qs), json.dumps(new_ss), y, dffe_type))
    cur.executemany("INSERT OR IGNORE INTO quasi_qmux VALUES (?, ?, ?, ?, ?);", quasi_qmuxes)
    # delete the original quasi_qmuxes
    cur.executemany(
        "DELETE FROM quasi_qmux WHERE c = ? AND ss = ? AND dffe_type = ?;",
        [(c, ss, dffe_type) for _, c, dffe_type, _, ss, _, _ in patterns]
    )
    netlist.commit()
    return cur.rowcount


def find_quasi_memory(netlist: NetlistDatabase) -> list:
    cur = netlist.cursor()
    memories = []
    cur.execute("SELECT c, ss, dffe_type FROM quasi_qmux GROUP BY c, ss, dffe_type HAVING COUNT(*) >= 8 ORDER BY LENGTH(ss) DESC;")
    groups = cur.fetchall()
    for c, ss, dffe_type in groups:
        if len(json.loads(ss)) < 10:
            continue
        cur.execute("SELECT qs, y FROM quasi_qmux WHERE c = ? AND ss = ? AND dffe_type = ?;", (c, ss, dffe_type))
        patterns = cur.fetchall()
        qss = [json.loads(qs) for qs, _ in patterns]
        rd = [y for _, y in patterns]
        ss = json.loads(ss)
        if len(ss) >= 256:
            memories.append((qss, ss, rd))
    return memories
