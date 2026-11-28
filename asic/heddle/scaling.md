# From a shuttle tile to a 16-billion-parameter model

Heddle proves the mechanism on a shuttle: trained weights become logic, and the
logic is checked bit for bit against the model. It holds 18,432 weights. The
target discussed for this project is a chat-capable model of about 16 billion
parameters, a factor of 870,000 larger. This note sizes that gap from the
tile's measurements and from public figures.

Each external figure is marked **[read]** when the primary document was opened
and **[cited]** when it comes from a search excerpt or secondary report and
still needs a click-through before anyone relies on it. Figures derived here
are marked **[derived]**.

## What the tile measures

| Quantity | Value | Basis |
|---|---:|---|
| Weights | 18,432 ternary, 13,267 non-zero | `model.json` |
| Standard-cell area after synthesis | 139,873 µm² | Yosys and ABC, IHP SG13G2 typical library |
| Area per weight, whole tile | 7.6 µm² | [derived] includes datapath, registers and both host ports; 9.3 µm² after timing repair |
| Clocks per token | 176 | one per network row, verified in RTL and gates |
| Token rate at the 25 MHz shuttle clock | about 142,000 characters per second | [derived] not measured on silicon |
| Held-out quality | 2.96 bits per character | letter frequencies give 4.38, a three-character table 2.88 |

At 2.96 bits per character the tile emits word-like fragments. It does not
answer questions.

## Why 130 nm cannot hold 16 billion parameters

| Storage style at 130 nm | Area per bit | 16B parameters at 4 bits | Basis |
|---|---:|---:|---|
| Heddle's logic, 2 bits per ternary weight | 3.8 µm² | 2,400 cm² | [derived] from the tile |
| Dense mask ROM cell | 0.25 µm² | 160 cm² | Infineon C11N brochure, 2007 [cited] |
| 6T SRAM cell | 1.9 to 2.5 µm² | 1,200 to 1,600 cm² | LSI Logic 2001, Intel IEDM 2000 [cited] |

A 200 mm wafer has 314 cm² and a single exposure field is limited to
26 mm × 33 mm, or 8.58 cm² (ASML scanner specifications [cited]). Even the
densest 130 nm ROM cell would need about nineteen full fields for the weights
alone, before any arithmetic. No tile size on this shuttle changes that: the
largest slot is 1.2 mm².

Storing ternary weights instead of 4-bit ones (about 1.58 bits each, as in
BitNet b1.58, arXiv 2402.17764 [cited]) cuts the weight bits from 64 Gbit to
about 25 Gbit. That is a 2.5-fold saving against a five-order gap.

## What the one shipping example shows

Taalas announced the HC1 in February 2026. Public reports describe it as
follows; all are [cited] and vendor power figures were not found.

| Item | Reported figure |
|---|---|
| Model | Llama 3.1 8B, hardwired |
| Process and die | TSMC N6, 815 mm², about 53 billion transistors |
| Weight precision | mixed 3-bit and 6-bit |
| Throughput | about 17,000 tokens per second, vendor-measured |
| Card power | 200 W in some reports, 250 W in others |
| Development cost | about 30 million US dollars |

Scaling that data point linearly [derived]:

- 16 billion parameters need about 1,630 mm² on the same process. That is two
  dies at the reticle limit, or one die on a denser node. A successor with
  about 20 billion parameters per chip has been announced but not shipped
  [cited].
- Power would be several hundred watts if it scales with parameter count.

An academic paper design (arXiv 2508.16151 [cited]) estimates 59 to 124
million US dollars of non-recurring cost at 5 nm for a hardwired language
model. Industry estimates of a complete 7 nm chip design run to about
250 million US dollars (IBS, via Arm's 2023 filing [cited]), with reports of
startups completing 7 nm chips for 50 to 75 million.

## What this means for a PCIe card

- **The link is not the constraint.** Text tokens are a few bytes each. Even
  20,000 tokens per second is far below one megabyte per second, and a single
  first-generation lane carries about 250 megabytes per second. One lane is
  enough at any model size.
- **Power is the constraint.** A low-profile x1 card may draw 10 W from the
  slot. An x16 slot supplies 75 W, an 8-pin auxiliary connector 150 W, and a
  12V-2x6 connector up to 600 W (PCI Express CEM, via secondary summaries
  [cited]). A 16-billion-parameter hardwired chip therefore needs a
  full-height card with auxiliary power and a large heatsink, whatever lane
  count it uses. It is a graphics-card-class product, not a low-profile x1
  card.
- **The host protocol carries over.** Push a token, read a token, poll a busy
  flag. The Heddle card exercises exactly that contract through a commodity
  bridge.

## Steps between the tile and the target

| Step | Size | Access | What it would prove |
|---|---|---|---|
| Heddle tile | 18 thousand weights | Tiny Tapeout IHP shuttle | Weights-to-logic flow, bit-exact verification, host protocol, measured area and energy per weight on silicon |
| Own 130 nm die | about 1 million weights in 8 to 10 mm² as synthesized logic [derived], less as an array | IHP SG13G2 multi-project wafer, listed at €7,300 per mm² for 2026 [cited] | A regular weight array instead of synthesized logic, pad ring, package, on-card power |
| Emulated architecture | full 16B model, weights in memory | FPGA or commodity accelerator on a card | Datapath, numerics and software at target scale, without a mask set |
| Advanced-node chip | 16B parameters hardwired | N6-class foundry, tens of millions of US dollars | The product |

The second step matters more than it looks. Heddle expresses its weights as
synthesized standard cells, which place-and-route tools treat as random logic.
The layout attempts recorded in [silicon/README.md](silicon/README.md) show the
cost: the densest netlist could not be routed in any slot tried, and the
routable one spends 37% more cells to keep its wires short. A scaled design needs a
regular, array-style weight structure with its own layout generator. That is
the main architectural lesson the tile gives for a larger chip.

## Limits of this note

- The linear scaling from one shipped chip ignores architecture, precision and
  node differences. Treat the results as order-of-magnitude.
- Conflicts between sources remain open: HC1 card power (200 W or 250 W) and
  the minimum area and open-source price of IHP multi-project runs.
- No current Tiny Tapeout price for IHP tiles was found. The last published
  figure located was €50 per tile for a 2025 shuttle [cited].
- Tile token rate, power and energy are simulation-derived until a fabricated
  sample is measured.
