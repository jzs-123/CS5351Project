# R3 state and retention

R3 holds no evolving account book. The caller owns the explicit position_context and its factual accuracy; a weight_source label does not verify an account. A per-run prepared snapshot is a detached copy of flag/context/policy. Checkpoint signatures include normalized content; same-file edits affect later runs. End-of-run lifecycle clears prepared state.

Historical inputs include21 closes, last observation, calculation settings, acquisition/source/time and content digest. Replay is validated/recomputed and marked snapshot_replay. Provider caches follow existing file/cache behavior; SDK cookies/metadata are not deliverables. No cross-symbol/date mutable result cache is added.

final_state and explicit JSON logs carry position_signal; reports are replaceable derived artifacts. Reusing a report directory reconciles only owned files and preserves user notes. This version adds no automatic retention/deletion schedule: operators must retain course evidence through assessment, then follow their own approved retention policy. Do not claim hashes authenticate the provider or that deletion guarantees recovery.
