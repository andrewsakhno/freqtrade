---
name: docker-wsl-access
description: "How to reach docker/servers for this project: mcp-wsl-exec connector never materialized; user approved plain wsl.exe bridging (2026-08-25); use script-over-ssh-stdin to avoid quoting breakage"
metadata: 
  node_type: memory
  type: project
  originSessionId: 093709c5-9ff0-49f3-b914-0fbf6a12d344
  modified: 2026-08-25T19:15:08.292Z
---

Docker is NOT on Windows. Local WSL distro is **Debian** (the only distro). The freqtrade bot itself runs on a **remote server**, not in WSL — see [[freqtrade-server-deployment]].

The `mcp-wsl-exec` MCP connector the user once wanted never appeared (checked 2026-08-24 and 2026-08-25). On 2026-08-25 the user said "пробуй через wsl" — plain `wsl -d Debian -- ...` via PowerShell/Bash is now the accepted access path.

**Why:** the dedicated connector doesn't exist in practice; user explicitly unblocked wsl.exe bridging.

**How to apply:**
- Run remote/server commands as: `wsl -d Debian -- ssh -o BatchMode=yes -o RequestTTY=no -o RemoteCommand=none -o ClearAllForwardings=yes freqtrade-ui '<cmd>'` (ClearAllForwardings is required — the ssh config has a LocalForward that fails with "address already in use" when the tunnel is up).
- Nested quoting through PowerShell/git-bash → wsl → ssh breaks silently (commands partially run locally). For anything non-trivial, write a script to the scratchpad and pipe it: `wsl -d Debian -- sh -c "ssh ... freqtrade-ui 'bash -s' < /mnt/c/<scratchpad>/script.sh"`.
