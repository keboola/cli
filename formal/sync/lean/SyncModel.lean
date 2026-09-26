/-
Formal-verification pilot for the kbagent sync engine (keboola/cli#792).

Pure decision functions of `sync diff` / `sync push` / `sync pull`, modelled
per config key from the Python sources, plus safety theorems about them.
Core Lean only (no Mathlib). Refuted properties are kept as `Prop`
definitions and their NEGATION is proved with a concrete witness -- see
the `-- FINDING` comments and the pilot report.
-/
import SyncModel.Diff
import SyncModel.Pull
