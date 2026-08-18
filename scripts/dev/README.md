# Development scripts

One-off diagnostics kept because each one settled a question that shaped the
design. Not needed to run the project.

- `t_plans.py`, `t_index_use.py`, `t_index_depth.py` — found that the vector
  index is only chosen above ~1,000 beliefs per subject
- `t_determinism.py` — found that cosine ties were broken by scan order
- `t_hlc.py` — found that `cluster_logical_timestamp()` inside a write pins the
  commit timestamp and makes the transaction unable to survive a push
- `t_rank.py` — found that a generic poisoned belief ranks ~12th and is never
  retrieved, which is why the attack impersonates the policy instead
- `t_beyond_gc.py` — replay past the MVCC garbage-collection window
- `bedrock_recheck.py`, `quota_check.py`, `list_models.py` — Bedrock access
- `bulk_seed.py`, `replant.py`, `progress.py` — seeding helpers
