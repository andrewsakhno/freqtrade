---
name: nfi-project-rules-focus
description: "Standing rule: the NFI/freqtrade project is never pushed to a remote repo; current focus (2026-08-25) is pair statistics and reversal-trading signals on the NFI X7 dry-run bot"
metadata: 
  node_type: memory
  type: project
  originSessionId: 23953f58-335e-48ab-8308-34773423da4a
  modified: 2026-08-25T19:38:41.223Z
---

**Standing rule (2026-08-25):** the freqtrade/NFI project (local repo and the server deployment in `/opt/nfi`) must NOT be pushed to any remote repository. Never suggest or perform `git push`/publishing for it.

**Current focus:** (1) accumulating per-pair dry-run statistics to decide which pairs/signals to keep; (2) implementing/tuning "reversal trading" — the disabled reversal-family entry conditions in NostalgiaForInfinityX7 (#10, #11, #170 crash-bounce hunter, #192, #193), toggled via `nfi_parameters.long_entry_signal_params` in `/opt/nfi/user_data/config.json` (that file also carries these notes as comments; backup at `config.json.bak`). Server details: [[freqtrade-server-deployment]].

The strategy has no dedicated "dead cat bounce" entry pattern — dead-cat logic exists only as protective filters; #170 is the closest tradable variant.
