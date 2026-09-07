# The delta decomposition runs (2026-09-07)

Reading L2 changes two things at once — the **price basis** (closes back-adjusted into one share
basis) and the **identity coverage** (L2 is stitched, so it answers for a reissued name under its
surviving ISIN on sessions where raw L1 carries only the retired one). A raw-vs-adjusted delta
measured with both moving is a sum, not a cause.

These are the arms that separate them, all on the server at `4bab8e0` over one lake state. The
middle arm of each is the adjusted signal held to the ISINs L1 printed each session
(`--signal-l1-isins-only`), so its candidate set is the raw run's exactly.

- `../M9-adjusted-backtest-report.md` — naive momentum, full universe, 2016-09 → 2026-08. Three
  arms and the split.
- `M9-delta-2019-2026.md` — the same three arms over 2019-07 → 2026-08, the window the fundamentals
  report uses.
- `M10-fundamentals-fixed-universe.md` — the six-arm M10.6 report with the momentum ranks on the
  fixed candidate set, so its momentum rows decompose against the adjusted edition one directory up.

The finding they produced is in `../algo-reevaluation-2026-09-07.md`: the price basis is neutral to
positive everywhere, and the whole of the adjusted arms' shortfall is the identity coverage — the
names within twelve months of a face-value split that only the stitched series makes rankable.
