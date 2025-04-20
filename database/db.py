import sqlite3
import pyrtl
import time
from . import formatter
from . import rewriter


# A netlist database can only hold one module/netlist.
class NetlistDatabase(sqlite3.Connection):
    def _create_tables(self):
        cur = self.cursor()
        # NOTE: Wire's id starts from 2.
        # 0 and 1 are reserved for constant 0 and 1.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wire (
                id INTEGER PRIMARY KEY,
                width INTEGER
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS dffe_xx (
                d INTEGER,
                c INTEGER,
                e INTEGER,
                q INTEGER,
                type VARCHAR(255),
                PRIMARY KEY (d, c, e, type)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS qmux (
                c INTEGER,
                qs JSON,
                ss JSON,
                y INTEGER,
                dffe_type VARCHAR(255),
                PRIMARY KEY (qs, ss, dffe_type)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS binary_gate (
                a INTEGER,
                b INTEGER,
                y INTEGER,
                type VARCHAR(255),
                PRIMARY KEY (a, b, type)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS unary_gate (
                a INTEGER,
                y INTEGER,
                type VARCHAR(255),
                PRIMARY KEY (a, type)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mux (
                a INTEGER,
                b INTEGER,
                s INTEGER,
                y INTEGER,
                PRIMARY KEY (a, b, s)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS quasi_qmux (
                c INTEGER,
                qs JSON,
                ss JSON,
                y INTEGER,
                dffe_type VARCHAR(255),
                PRIMARY KEY (qs, ss, dffe_type)
            );
        """)
        self.commit()

    def __init__(self, db_path: str = ":memory:"):
        super().__init__(db_path)
        self._create_tables()

    def build_from_json(self, netlist: dict, target_module: str, ignore_errors: bool = False):
        module_data = formatter.json_to_db(netlist, target_module, ignore_errors=ignore_errors)
        wire_data = [(w["id"], w["width"]) for w in module_data["wire"]]
        binary_gate_data = [(g["a"], g["b"], g["y"], g["type"]) for g in module_data["binary_gate"]]
        dffe_xx_data = [(d["d"], d["c"], d["e"], d["q"], d["type"]) for d in module_data["dffe_xx"]]
        unary_gate_data = [(u["a"], u["y"], u["type"]) for u in module_data["unary_gate"]]
        mux_data = [(m["a"], m["b"], m["s"], m["y"]) for m in module_data["mux"]]

        cur = self.cursor()
        cur.executemany("INSERT INTO wire (id, width) VALUES (?, ?)", wire_data)
        cur.executemany("INSERT INTO binary_gate (a, b, y, type) VALUES (?, ?, ?, ?)", binary_gate_data)
        cur.executemany("INSERT INTO dffe_xx (d, c, e, q, type) VALUES (?, ?, ?, ?, ?)", dffe_xx_data)
        cur.executemany("INSERT INTO unary_gate (a, y, type) VALUES (?, ?, ?)", unary_gate_data)
        cur.executemany("INSERT INTO mux (a, b, s, y) VALUES (?, ?, ?, ?)", mux_data)
        self.commit()

    def get_next_id(self) -> int:
        cur = self.cursor()
        cur.execute("SELECT MAX(id) FROM wire")
        max_id = cur.fetchone()[0]
        return 2 if max_id is None else max_id + 1

    def extract_mems(self):
        times = []
        # saturate
        time_start = time.time()

        # group registers
        rewriter.rewrite_dffe_pn_to_pp(self)
        rewriter.group_dffe_pp(self)

        # basic boolean rules
        # updated = True
        # while updated:
        #     updated = False
        #     updated = True if rewriter.saturate_comm(self, "$_AND_") > 0 else updated
        #     updated = True if rewriter.saturate_comm(self, "$_OR_") > 0 else updated
        #     updated = True if rewriter.saturate_demorgan(self, "$_AND_", "$_OR_") else updated
        #     updated = True if rewriter.saturate_demorgan(self, "$_OR_", "$_AND_") else updated
        #     updated = True if rewriter.saturate_idemp(self, "$_NOT_") else updated

        # reduce muxes
        rewriter.rewrite_2_1_mux_to_binary_gate(self)
        while rewriter.reduce_mux_once(self):
            pass

        cursor = self.cursor()
        cursor.execute("SELECT wire.width, COUNT(*) FROM binary_gate JOIN wire ON binary_gate.b = wire.id WHERE type = '$_MUX_' GROUP BY wire.width")
        res = cursor.fetchall()
        for width, count in res:
            print(f"Number of {width}-1 Muxes: {count}")

        times.append(time.time() - time_start) # saturation time
        time_start = time.time()

        # extract memories
        mems = rewriter.find_memory(self)
        print("-" * 20)
        for i, (regs, (readports, writeports)) in enumerate(mems.items()):
            print(f"Memory {i}:")
            print(f"- Registers: {regs}")
            print(f"- Data Width: {len(readports[0][1])}")
            print(f"- Number of Entries: {len(regs)}")
            print(f"- Read Ports:")
            for j, (_, y, a) in enumerate(readports):
                print(f"-- Read port {j}:")
                print(f"--- Read Data Wires: {y}")
                print(f"--- Read Address wire: {a}")
            print(f"- Write Ports:")
            for i, (wds, wes) in enumerate(writeports):
                print(f"-- Write port {i}:")
                print(f"--- Write Data Wires: {wds}")
                print(f"--- Write Raw Enable Wires: {wes}")
                wen, waddr = rewriter.create_write_port_from_wes(self, wes)
                print(f"--- Write Enable Wire: {wen}")
                print(f"--- Write Address Wire: {waddr}")
            print("-" * 20)

        times.append(time.time() - time_start) # memory extraction time
        print(f"Saturation time: {times[0]}")
        print(f"Memory extraction time: {times[1]}")
        print(f"Total time: {sum(times)}")
