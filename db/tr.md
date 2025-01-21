## Modeling Netlists for Rewriting and Saturation: A Relational Approach

### Abstract
This report presents how and why we represent a netlist as an Entity–relationship model (ER model) and how to use this model to perform rewriting and saturation efficiently. The similar idea has been proposed in **[1]** for E-matching. We extend the idea to both rewriting and saturation for circuit representation.

### Insights
Three main problems are targeted:
1. **The grouping problem**: How to group as many components (with certain properties) as possible to reduce the search space?
2. **Flexible rule application**: How to apply a set of rules with flexible conditions and actions?
3. **Feedback loops**: How to express the feedback loops in the circuit without their topological information?

All problems above can be solved by the ER model and thanks to the maturity of the relational database management systems, we can do all operations of rewriting and saturation efficiently in databases.

### Schema
```sql
CREATE TABLE wires (
    id INTEGER PRIMARY KEY,
    width INTEGER NOT NULL,
    alias VARCHAR(255)
);
```
To be clearer, this table represents the output ports of components, which are also the distinct equivalence classes in the circuit.

```sql
CREATE TABLE binary_combinatinal_components (
    type VARCHAR(255),
    in1 INTEGER,
    in2 INTEGER,
    out1 INTEGER NOT NULL,
    alias VARCHAR(255),
    PRIMARY KEY (type, input1, input2),
    FOREIGN KEY (input1) REFERENCES wires(id),
    FOREIGN KEY (input2) REFERENCES wires(id),
    FOREIGN KEY (output) REFERENCES wires(id)
);
-- Add more tables for different kinds of combinational components
```
This table represents the combinational components with two inputs and one output, where the type can be `$_AND_`, `$_OR_`, `$_XOR_`, etc. Note that the gate width is not included in the table, as it is not necessary for the rewriting and saturation and can be inferred from the input wire width. Also, the primary key does not include the output wire. A binary gate can be uniquely identified by its type and two input wires.
```sql
CREATE TABLE dffe_pps (
    clk INTEGER,
    en INTEGER,
    d INTEGER,
    q INTEGER NOT NULL,
    alias VARCHAR(255),
    PRIMARY KEY (clk, en, d),
    FOREIGN KEY (clk) REFERENCES wires(id),
    FOREIGN KEY (en) REFERENCES wires(id),
    FOREIGN KEY (d) REFERENCES wires(id),
    FOREIGN KEY (q) REFERENCES wires(id)
);
```
Similarly, this table represents the D flip-flops. There's nothing special in the table, but in the rewriting and saturation they may be treated differently from the combinational components.
```sql
CREATE TABLE concat (
    in1 INTEGER NOT NULL,
    out1 INTEGER,
    left INTEGER,
    right INTEGER,
    alias VARCHAR(255),
    PRIMARY KEY (out1, left, right),
);
```
This table is the result of converting a many-to-one mapping to a one-to-one relationship. We want the concatenation of wires to be flexible: You can find all inputs of an output by `SELECT in1 FROM concat WHERE out1 = ?`, and append a new wire to the concatenation by `INSERT INTO concat VALUES (?, ?, ?, ?, ?)`. The `left` and `right` columns are used to represent the position of the wire in the concatenation. 
```sql
CREATE TABLE sel (
    in1 INTEGER,
    out1 INTEGER NOT NULL,
    left INTEGER,
    right INTEGER,
    alias VARCHAR(255),
    PRIMARY KEY (in1, left, right),
);
```
This table is similar to the `concat` table, but is used to select a wire from a wider wire.

### Manipulation
#### Rewriting
The operation of rewriting consists of matching and replacing. It's tricky to implement in Datalog since the actions must be atomic (including adding new components and removing old ones) and once an action is triggered, the pattern matching process should be restarted. In the relational database, we can implement every rewriting rule as an atomic transaction: `SELECT`, `DELETE`, `INSERT` and `COMMIT`.

#### Saturation
Saturation utilizes the uniqueness of the primary key in the tables. `INSERT OR IGNORE` can be used to insert a new component. If it says no rows are affected, then the netlist is saturated. Saturation can be done automatically parallelly in the database.

#### Verifier
The verification of the well-formedness of netlist can be done by checking the consistency of the database. For example, constraints can be added to the tables to ensure that the input and output wires of a binary combinational gate are of the same width. All ports should refer to an existing wire. It is worth noting that there's no easy way to check that all wires are connected at least once in the database. To achieve this, we need a clean pass to remove dangling wires at the end.

### Case Study: To MUX-Only
In this case, our purpose is to convert a combinational circuit to an equivalent one with only MUXes and Nots (and as few as possible). Without loss of generality, we assume that the original circuit consists of only AND, OR, and NOT gates. Also, there's no constant input ports of 0 or 1 in the circuit.
#### Step 1: Rewrite to MUX
We can define the following rules to rewrite each type of gate to MUXes:

1. `AND(a, b, y) -> MUX(s=a, d0=a, d1=b, y=y)`
2. `OR(a, b, y) -> MUX(a, b, a, y)`

These rules can be applied parallelly since the new components don't overlap or interfere with each other. They are heuristic and not optimal.

#### Step 2: Saturate MUX
We can define the following rules to saturate the MUXes:

1. **Identity**: `MUX(s, d, d, y) -> y = d`
2. **Negation**: `NOT(s, s'), MUX(s', d0, d1, y) -> MUX(s, d1, d0, y)`
3. **Distribution**: `NOT(d0, d0'), NOT(d1, d1'), MUX(s, d0', d1', y) -> MUX(s, d0, d1, y'), NOT(y', y)`
4. Maybe more...

And Nots:
1. **Idempotence**: `NOT(a, a'), NOT(a', y) -> y = a`

#### Step 3: Clean
Remove all the unused wires.

#### Step 4: Extract (Optional)
Suppose we need the netlist with minimum number of MUXes, we can apply dynamic programming to each wire: assign a cost to each wire and for each wire, check its minimum cost implementation from its equivalence class. In this case, extraction is easy: every gate only has one output.

### References
**[1]** Zhang, Yihong, et al. "Relational E-matching." arXiv preprint arXiv:2108.02290 (2021).