BINARY_GATES = {"$_NAND_", "$_ORNOT_", "$_AND_", "$_XNOR_", "$_ANDNOT_", "$_XOR_", "$_OR_", "$_NOR_"}
UNARY_GATES = {"$_NOT_"}
DFFE_GATES = {"$_DFFE_PP_", "$_DFFE_PN_"}


def json_to_db(netlist: dict, target_module: str, ignore_errors: bool = False) -> dict[str, dict]:
    # When set ignore_errors to True, the function will not raise an error when it encounters an unknown cell type.
    db = {}
    module: dict[str, dict] = netlist["modules"][target_module]
    wires: set[int] = set()

    # attributes
    db["attributes"] = module["attributes"]

    # gates
    db["binary_gate"] = []
    db["unary_gate"] = []
    db["dffe_xx"] = []
    db["mux"] = []
    for cell in module["cells"].values():
        cell_type = cell["type"]
        if cell_type in DFFE_GATES:
            d, c, e, q = cell["connections"]["D"][0], cell["connections"]["C"][0], cell["connections"]["E"][0], cell["connections"]["Q"][0]
            db["dffe_xx"].append({
                "d": d, "c": c, "e": e, "q": q,
                "type": cell_type
            })
            [wires.add(w) for w in [d, c, e, q]]
        elif cell_type == "$_MUX_":
            a, b, s, y = cell["connections"]["A"][0], cell["connections"]["B"][0], cell["connections"]["S"][0], cell["connections"]["Y"][0]
            db["mux"].append({
                "a": a, "b": b, "s": s, "y": y
            })
            [wires.add(w) for w in [a, b, s, y]]
        elif cell_type in BINARY_GATES:
            a, b, y = cell["connections"]["A"][0], cell["connections"]["B"][0], cell["connections"]["Y"][0]
            db["binary_gate"].append({
                "a": a, "b": b, "y": y,
                "type": cell_type
            })
            [wires.add(w) for w in [a, b, y]]
        elif cell_type in UNARY_GATES:
            a, y = cell["connections"]["A"][0], cell["connections"]["Y"][0]
            db["unary_gate"].append({
                "a": a, "y": y,
                "type": cell_type
            })
            [wires.add(w) for w in [a, y]]
        elif not ignore_errors:
            raise ValueError(f"Unknown cell type: {cell_type}")

    # wires
    db["wire"] = [{"id": w, "width": 1} for w in wires]
    db["inputs"] = []
    db["outputs"] = []
    ports = module["ports"]
    for port in ports.values():
        direction, bits = port["direction"], port["bits"]
        if direction == "input":
            db["inputs"].extend(bits)
        elif direction == "output":
            db["outputs"].extend(bits)
        elif not ignore_errors:
            raise ValueError(f"Unknown port direction: {direction}")

    return db