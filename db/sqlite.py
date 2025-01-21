import sqlite3


MAGIC_NUMBER = 100000

def id_generator():
    i = MAGIC_NUMBER
    while True:
        yield i
        i += 1

global_id = id_generator()


def create_tables(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS wire (
            id INTEGER PRIMARY KEY,
            width INTEGER
        );
        """
    )
    # cur.execute(
    #     """
    #     CREATE TABLE IF NOT EXISTS demux (
    #         a INTEGER,
    #         s INTEGER,
    #         y INTEGER,
    #         PRIMARY KEY (a, s, y)
    #     """
    # )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS dffe_pp (
            d INTEGER,
            c INTEGER,
            e INTEGER,
            q INTEGER,
            PRIMARY KEY (d, c, e)
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS binary_gate (
            a INTEGER,
            b INTEGER,
            y INTEGER,
            type TEXT,
            PRIMARY KEY (a, b, type)
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS selector (
            input INTEGER,
            output INTEGER,
            left INTEGER,
            right INTEGER,
            PRIMARY KEY (input, left, right)
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS concat (
            input INTEGER,
            output INTEGER,
            left INTEGER,
            right INTEGER,
            PRIMARY KEY (output, left, right)
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS unary_gate (
            a INTEGER,
            y INTEGER,
            type TEXT,
            PRIMARY KEY (a, type)
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mux (
            a INTEGER,
            b INTEGER,
            s INTEGER,
            y INTEGER,
            PRIMARY KEY (a, b, s, y)
        );
        """
    )   # TODO: can be deleted
    conn.commit()


def insert_records(conn: sqlite3.Connection, table: str, records: list):
    if not records:
        return
    cur = conn.cursor()
    placeholds = ("?," * len(records[0]))[:-1]
    cur.executemany(
        f"INSERT OR IGNORE INTO {table} VALUES ({placeholds});",
        records
    )
    conn.commit()


def group_dffs_all(conn: sqlite3.Connection) -> bool:
    """
    Based on the fact that all DFF groups are disjoint,
    we can group all of them in one go.
    For now, we only group _DFFE_PP_
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c, e
        FROM dffe_pp
        GROUP BY c, e
        HAVING COUNT(*) > 1;
        """
    )
    ces = cur.fetchall()
    if not ces:
        return False
    for c, e in ces:
        cur.execute(
            """
            SELECT d, q, wire.width as width
            FROM dffe_pp JOIN wire ON d = wire.id
            WHERE c = ? AND e = ?;
            """,
            (c, e)
        )
        dqs: list[tuple[int, int]] = cur.fetchall()

        # remove the dffs
        cur.execute(
            """
            DELETE FROM dffe_pp
            WHERE c = ? AND e = ?;
            """,
            (c, e)
        )

        concat_output, selectors_input = next(global_id), next(global_id)
        wi, concats, selectors = 0, [], []
        for d, q, width in dqs:
            concats.append((d, concat_output, wi, wi + width - 1))
            selectors.append((selectors_input, q, wi, wi + width - 1))
            wi += width
        # construct a concat
        cur.executemany(
            "INSERT INTO concat VALUES (?, ?, ?, ?);",
            concats
        )
        # construct a selector
        cur.executemany(
            "INSERT INTO selector VALUES (?, ?, ?, ?);",
            selectors
        )
        # construct two wires
        cur.executemany(
            "INSERT INTO wire VALUES (?, ?);",
            [(concat_output, wi), (selectors_input, wi)]
        )
        # construct a dff
        cur.execute(
            "INSERT INTO dffe_pp VALUES (?, ?, ?, ?);",
            (concat_output, c, e, selectors_input)
        )
    conn.commit()
    return True


def saturate_demuxes_all(conn: sqlite3.Connection) -> bool:
    """
    It finds all {(a /\ !s), (a /\ s)} patterns
    and saturates each of them with a 1-2 demux and a selector
    NOTE: Only considers 1-bit demuxes
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT bg1.a, bg1.b, bg1.y, bg2.b, bg2.y
        FROM binary_gate AS bg1 JOIN binary_gate AS bg2 JOIN unary_gate AS ug
        ON bg1.a = bg2.a AND bg1.b = ug.y
        WHERE bg1.type = "$_AND_" AND bg2.type = "$_AND_" AND ug.type = "$_NOT_" AND bg2.b = ug.a;
        """
    )
    ab1y1b2y2s = cur.fetchall()
    if not ab1y1b2y2s:
        return False
    updated = False
    for a, b1, y1, b2, y2 in ab1y1b2y2s:
        # construct a 1-2 demux
        demux_output = next(global_id)
        cur.execute(
            "INSERT OR IGNORE INTO binary_gate VALUES (?, ?, ?, ?);",
            (a, b2, demux_output, "$_DEMUX_")
        )
        if cur.rowcount > 0:
            updated = True
        else:
            continue
        # construct a wire
        cur.execute(
            "INSERT INTO wire VALUES (?, ?);",
            (demux_output, 2)
        )
        # construct a selector
        cur.executemany(
            "INSERT INTO selector VALUES (?, ?, ?, ?);",
            (
                (demux_output, y1, 0, 0),
                (demux_output, y2, 1, 1)
            )
        )
    conn.commit()
    return updated


def saturate_commutative_binary_gates_all(conn: sqlite3.Connection, target: str) -> bool:
    """
    It finds all commutative binary gates
    and saturates them
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT a, b, y, type
        FROM binary_gate
        WHERE type = ?;
        """,
        (target,)
    )
    abyts = cur.fetchall()
    if not abyts:
        return False
    bayts = [(b, a, y, t) for a, b, y, t in abyts]
    cur.executemany(
        "INSERT OR IGNORE INTO binary_gate VALUES (?, ?, ?, ?);",
        bayts
    )
    conn.commit()
    return cur.rowcount > 0


# def print_table(cur, table):
#     cur.execute(f"SELECT * FROM {table};")
#     print(f"Table {table}:")
#     for row in cur.fetchall():
#         print(row)
#     print()

def print_dffs(cur):
    cur.execute(
        """
        SELECT d, c, e, q, wire.width as width
        FROM dffe_pp JOIN wire ON d = wire.id;
        """
    )
    print("DFFs:")
    for row in cur.fetchall():
        print(row)
    print()


if __name__ == "__main__":
    # NETLIST_FILE = "dff2w.json"
    # NETLIST_FILE = "2dff2w.json"
    # NETLIST_FILE = "tests/blifjson/alu.json"
    NETLIST_FILE = "tests/blifjson/nerv2.json"

    # with open(NETLIST_FILE) as f:
    #     netlist = json.load(f)

    import json
    import formatter
    import time
    with open(NETLIST_FILE) as f:
        netlist = json.load(f)
    netlist = formatter.blif_to_db(netlist, "nerv", ignore_errors=True) # ignore errors for now
    wire_data = [(w["id"], w["width"]) for w in netlist["wires"]]
    binary_gate_data = [(g["a"], g["b"], g["y"], g["type"]) for g in netlist["binary_gates"]]
    dffe_pp_data = [(d["d"], d["c"], d["e"], d["q"]) for d in netlist["dffe_pps"]]
    unary_gate_data = [(u["a"], u["y"], u["type"]) for u in netlist["unary_gates"]]
    mux_data = [(m["a"], m["b"], m["s"], m["y"]) for m in netlist["muxes"]]

    conn = sqlite3.connect(":memory:")  # Create a new database in memory
    create_tables(conn)

    insert_records(conn, "wire", wire_data)
    insert_records(conn, "binary_gate", binary_gate_data)
    insert_records(conn, "dffe_pp", dffe_pp_data)
    insert_records(conn, "unary_gate", unary_gate_data)
    insert_records(conn, "mux", mux_data)

    # print_dffs(conn.cursor())

    group_dffs_all(conn)
    saturate_commutative_binary_gates_all(conn, "$_AND_")
    saturate_commutative_binary_gates_all(conn, "$_OR_")
    start = time.time()
    saturate_demuxes_all(conn)
    print("Time:", time.time() - start)

    # print_dffs(conn.cursor())
    cur = conn.cursor()
    cur.execute("SELECT * FROM binary_gate WHERE type = \"$_DEMUX_\";")
    print("Demuxes:")
    for row in cur.fetchall():
        print(row)

    conn.close()