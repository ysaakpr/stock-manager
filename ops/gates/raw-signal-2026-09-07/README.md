# The raw-signal editions of the three sweeps (2026-09-07)

The reports one directory up now carry the **L2 back-adjusted** signal — the default since
`0e53f4a` made the signal source a parameter, and the signal a live run should use. These are the
**raw-signal** editions of the same three sweeps, run on the same lake state (server, `ad0746f`,
2026-09-07 08:30 UTC) so the two sets differ in the signal source and nothing else.

They are kept because the raw-vs-adjusted difference is a finding in its own right, read in
`../algo-reevaluation-2026-09-07.md`: over the decade the raw signal *beats* the adjusted one,
because misreading a split as a ~-50 % twelve-month return acts as an accidental "sell what just
split" filter. Regenerate either edition with `--raw` or without it; both name their source in the
report's own data-reality section, so a file is never ambiguous about which it is.
