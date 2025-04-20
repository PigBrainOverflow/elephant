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

def find_single_dffe_by_q(netlist, w: int) -> tuple[int, int, int] | None:
    # returns (d, c, e) if found
    cur = netlist.cursor()
    dffe = None
    cur.execute("SELECT d, c, e FROM dffe_xx WHERE q = ? LIMIT 1;", (w,))
    res = cur.fetchone()
    if res:
        return res
    
    cur.execute("SELECT input FROM selector WHERE output = ?;", (w,))
    res = cur.fetchall()
    if not res:
        return None
    for (input,) in res:
        dffe1 = find_single_dffe_by_q(netlist, input)
        if not dffe:
            dffe = dffe1
        elif dffe != dffe1:
            return None
    return dffe


def saturate_readport(netlist) -> list:
    cur = netlist.cursor()
    cur.execute(
        """
        SELECT a, width
        FROM binary_gate JOIN wire ON id = b
        WHERE type = "$_MUX_" AND width >= 4
        GROUP BY a, width
        HAVING COUNT(*) >= 4;
        """
    )
    aws = cur.fetchall()
    if not aws:
        return 0
    ports = []
    for a, w in aws:
        cur.execute(
            "SELECT b, y FROM binary_gate JOIN wire ON id = b WHERE a = ? AND type = \"$_MUX_\" AND width = ?;",
            (a, w)
        )
        bys = cur.fetchall()
        if not bys:
            continue
        # find the selectors
        selectors = None
        ys = []
        for b, y in bys:
            q = find_single_selector_by_output(netlist, b)
            if not q:
                continue
            else:
                ys.append(y)
                if not selectors:
                    selectors = q
                elif selectors != q:
                    break
        if selectors and len(selectors) == w:
            ports.append((a, ys, selectors, w))
    return ports

def find_memory(netlist) -> dict:
    readports = saturate_readport(netlist)
    memories = {}   # qs -> ([(width, rd, ra)], [(wd, wes)])
    for a, y, selectors, w in readports:
        qs = tuple(selectors)
        if qs in memories:
            memories[qs][0].append((w, y, a))
        else:
            memories[qs] = ([(w, y, a)], [])

    # find write ports
    for qs in memories:
        cur = netlist.cursor()
        cur.execute(
            """
            SELECT d
            FROM dffe_xx
            WHERE q = ?
            LIMIT 1;
            """,
            (qs[0],)
        )
        res = cur.fetchone()
        if not res:
            continue
        cur.execute("SELECT input FROM concat WHERE output = ?;", (res[0],))
        res = cur.fetchall()
        if not res:
            continue
        wds = [input for (input,) in res]
        wes = []
        for q in qs:
            cur.execute("SELECT e FROM dffe_xx WHERE q = ?;", (q,))
            res = cur.fetchone()
            if not res:
                continue
            wes.append(res[0])

        memories[qs][1].append((wds, wes))

    return memories


def find_or_create_binary_gate(netlist, a: int, b: int, type: str) -> int:
    cur = netlist.cursor()
    cur.execute(
        "SELECT y FROM binary_gate WHERE a = ? AND b = ? AND type = ?;",
        (a, b, type)
    )
    res = cur.fetchone()
    if res:
        # print(f"Found binary gate {a} {b} {type} -> {res[0]}")
        return res[0]
    y = next(global_id)
    cur.execute(
        "INSERT INTO binary_gate VALUES (?, ?, ?, ?);",
        (a, b, y, type)
    )
    cur.execute(
        "INSERT INTO wire VALUES (?, 1);",
        (y,)
    )
    return y


def find_or_create_unary_gate(netlist, a: int, type: str) -> int:
    cur = netlist.cursor()
    cur.execute(
        "SELECT y FROM unary_gate WHERE a = ? AND type = ?;",
        (a, type)
    )
    res = cur.fetchone()
    if res:
        # print(f"Found unary gate {a} -> {res[0]}")
        return res[0]
    y = next(global_id)
    cur.execute(
        "INSERT INTO unary_gate VALUES (?, ?, ?);",
        (a, y, type)
    )
    cur.execute(
        "INSERT INTO wire VALUES (?, 1);",
        (y,)
    )
    return y


def create_write_port_from_wes(netlist, wes: list[int]) -> tuple[int, list[int]] | None:
    """
    It creates a write port and repairs the connections: use a reversed write port
    """
    wen, waddr, wes_bundle = next(global_id), next(global_id), next(global_id)
    n = len(wes)
    log_map = {2: 1, 4: 2, 8: 3, 16: 4, 32: 5, 64: 6, 128: 7, 256: 8, 512: 9, 1024: 10, 2048: 11, 4096: 12}
    if n not in log_map:
        return False
    logn = log_map[n]
    cur = netlist.cursor()
    cur.executemany(
        "INSERT INTO wire VALUES (?, ?);",
        [(wen, 1), (waddr, logn), (wes_bundle, n)]
    )
    cur.executemany(
        "INSERT INTO selector VALUES (?, ?, ?, ?);",
        [(wes_bundle, wes[i], i, i) for i in range(n)]
    )
    cur.execute(
        "INSERT INTO binary_gate VALUES (?, ?, ?, ?);",
        (wen, waddr, wes_bundle, "$_WRITE_PORT_")
    )

    # create a reversed write port
    # step 1: create wen := wes[0] + wes[1] + ... + wes[n-1]
    # reduction tree
    ys = list(wes)
    for _ in range(1, logn):
        new_ys = []
        j = 0
        while j < len(ys):
            if j + 1 < len(ys):
                y = find_or_create_binary_gate(netlist, ys[j], ys[j + 1], "$_OR_")
                new_ys.append(y)
            j += 2
        ys = new_ys
    # the final or gate
    a, b = ys
    cur.execute(
        "INSERT OR IGNORE INTO binary_gate VALUES (?, ?, ?, ?);",
        (a, b, wen, "$_OR_")
    )
    # step 2: create waddr
    # waddr[0] := wes[0]' wes[2]' wes[4]' ...
    # waddr[1] := wes[0]' wes[1]' wes[4]' ...
    # waddr[2] := wes[0]' wes[1]' wes[2]' ...
    # ...
    # create inverters
    invs = []
    for we in wes:
        invs.append(find_or_create_unary_gate(netlist, we, "$_NOT_"))

    # reduction trees
    waddrs = []
    for i in range(0, logn):    # build waddr[i]
        invs_to_bundle = list(zip(*[iter(invs)] * (2 ** i)))[::2]
        ands = []
        for invs_bundle in invs_to_bundle:
            ands.extend(invs_bundle)
        while len(ands) > 1:
            new_ands = []
            for j in range(0, len(ands), 2):
                a, b = ands[j], ands[j + 1]
                new_ands.append(find_or_create_binary_gate(netlist, a, b, "$_AND_"))
            ands = new_ands
        waddrs.append(ands[0])

    # step 3: bundle waddrs
    # cur.executemany(
    #     "INSERT INTO concat VALUES (?, ?, ?, ?);",
    #     [(waddrs[i], waddr, i, i) for i in range(logn)]
    # )
    # netlist.commit()

    return wen, waddrs


# We can only consider muxes that are connected (directly or indirectly) to dffes

def rewrite_mux_to_qmux(netlist: NetlistDatabase) -> int:
    # qmux is a mux with inputs connected (directly) to dffes
    # we can safely remove the original mux
    cur = netlist.cursor()
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
    qmuxes = [
        (c, json.dumps([a, b]), json.dumps([s]), y, dffe_type)
        for a, b, s, y, c, dffe_type in patterns
    ]
    cur.executemany("INSERT INTO qmux VALUES (?, ?, ?, ?, ?);", qmuxes)
    cur.executemany(
        "DELETE FROM mux WHERE a = ? AND b = ? AND s = ?;",
        [(a, b, s) for a, b, s, _, _, _ in patterns]
    )
    netlist.commit()
    # print(qmuxes)
    return cur.rowcount


def reduce_qmux_once(netlist: NetlistDatabase) -> int:
    # this keeps the original qmuxes
    cur = netlist.cursor()
    cur.execute(
        """
        SELECT qm1.qs, qm2.qs, qm1.ss, mux.s, mux.y, qm1.c, qm1.dffe_type
        FROM qmux AS qm1 JOIN qmux AS qm2 JOIN mux
        ON qm1.c = qm2.c AND qm1.ss = qm2.ss AND qm1.y = mux.a AND qm2.y = mux.b AND qm1.dffe_type = qm2.dffe_type;
        """
    )
    patterns = cur.fetchall()
    if not patterns:
        return 0
    qmuxes = []
    for qs1, qs2, ss, s, y, c, dffe_type in patterns:
        qs1, qs2, ss = json.loads(qs1), json.loads(qs2), json.loads(ss)
        qmuxes.append(
            (c, json.dumps(qs1 + qs2), json.dumps(ss + [s]), y, dffe_type)
        )
    cur.executemany("INSERT OR IGNORE INTO qmux VALUES (?, ?, ?, ?, ?);", qmuxes)
    netlist.commit()
    return cur.rowcount


def contains(a: tuple, b: tuple) -> bool:
    # if a contains b
    return all(x in a for x in b)


def find_readport(netlist: NetlistDatabase) -> dict[tuple[tuple[tuple[int]], tuple[int]], tuple[int]]:
    # (((q)), (ra)) -> (rd)
    # ((1, 2), (3, 4)) means q1, q2 -> rd1 & q3, q4 -> rd2
    readports = {}
    cur = netlist.cursor()
    # width >= 8
    # order by log(height) approximately
    cur.execute("SELECT c, ss, dffe_type FROM qmux GROUP BY c, ss, dffe_type HAVING COUNT(*) >= 8 ORDER BY LENGTH(ss) DESC;")
    groups = cur.fetchall()
    for c, ss, dffe_type in groups:
        # check whether ss is a subset of existing readports
        ss_tuple = tuple(json.loads(ss))
        if any(contains(ra, ss_tuple) for _, ra in readports.keys()):
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
            if contains(mqs, qs):
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


def create_writeport(netlist: NetlistDatabase, memories: dict[tuple[tuple[int]], list[tuple[tuple[int], tuple[int]]]]) -> dict[tuple[tuple[int]], tuple[int, tuple[int], tuple[int]]]:
    writeports = {}
    for qss in memories.keys():
        # find the write enable signals (wes)
        wes = [
            find_d_e_from_q(netlist, q)[1]
            for q in qss[0]
        ]
        wds = tuple(
            find_d_e_from_q(netlist, qs[0])[0]
            for qs in qss
        )
        # create a write port
        wen, waddrs = create_write_port_from_wes(netlist, wes)
        writeports[qss] = (wen, tuple(waddrs), wds)
    return writeports


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


if __name__ == "__main__":

    NETLIST_FILE = "elephant/tests/json/pico.json"

    import time
    import json
    from . import db
    start = time.time()
    with open(NETLIST_FILE, "r") as f:
        blif = json.load(f)
    netlist = db.NetlistDatabase()
    netlist.build_from_blif(blif, "picorv32", True)

    cur = netlist.cursor()
    cur.execute(
        """
        SELECT type, COUNT(*)
        FROM binary_gate
        GROUP BY type;
        """
    )
    print(cur.fetchall())

    # process
    rewrite_dffe_pn_to_pp(netlist)
    group_dffe_pp(netlist)

    updated = True
    while updated:
        updated = False
        updated = True if saturate_comm(netlist, "$_AND_") > 0 else updated
        updated = True if saturate_comm(netlist, "$_OR_") > 0 else updated
        updated = True if saturate_demorgan(netlist, "$_AND_", "$_OR_") else updated
        updated = True if saturate_demorgan(netlist, "$_OR_", "$_AND_") else updated
        updated = True if saturate_idemp(netlist, "$_NOT_") else updated

    # rewrite_andnot_to_and_not(netlist)
    # saturate_comm(netlist, "$_AND_")
    # saturate_comm(netlist, "$_OR_")
    # while saturate_demorgan(netlist, "$_AND_", "$_OR_"):
    #     pass
    # while saturate_demorgan(netlist, "$_OR_", "$_AND_"):
    #     pass
    # while saturate_idemp(netlist):
    #     pass

    saturate_1_2_demux(netlist)
    saturate_2_1_mux(netlist)
    rewrite_2_1_mux_to_binary_gate(netlist)

    cur.execute(
        """
        SELECT type, COUNT(*)
        FROM binary_gate
        GROUP BY type;
        """
    )
    print(cur.fetchall())

    saturate_2_1_mux(netlist)
    rewrite_2_1_mux_to_binary_gate(netlist)
    while reduce_mux_once(netlist):
        pass


    mems = find_memory(netlist)
    for mem, (readports, writeports) in mems.items():
        print(f"Memory {mem}:")
        for i, (w, y, a) in enumerate(readports):
            print(f"Read port {i}:")
            print(f"\twidth: {w}")
            print(f"\tread data wires: {y}")
            print(f"\tread address wire: {a}")
        for i, (wds, wes) in enumerate(writeports):
            print(f"Write port {i}:")
            print(f"\twidth: {len(wds)}")
            print(f"\twrite data wires: {wds}")
            print(f"\twrite enable wires: {wes}")
            create_write_port_from_wes(netlist, wes)
        print("--------------------")

    print("Total time:", time.time() - start)
