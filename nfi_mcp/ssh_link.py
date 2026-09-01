"""
SSH transport to the freqtrade server.

Single responsibility: run a bash script on the remote host and return its
output. Scripts are always passed via stdin (never as command-line arguments)
because nested quoting through wsl.exe -> ssh breaks silently.
"""

import subprocess
from dataclasses import dataclass

SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "RequestTTY=no",
    "-o", "RemoteCommand=none",
    "-o", "ClearAllForwardings=yes",
]


class SshError(RuntimeError):
    pass


@dataclass
class SshResult:
    returncode: int
    stdout: str
    stderr: str


class SshRunner:
    """Runs bash scripts on the remote server via wsl.exe -> ssh."""

    def __init__(self, host: str = "freqtrade-ui", wsl_distro: str = "Debian", timeout: int = 90):
        self._host = host
        self._distro = wsl_distro
        self._timeout = timeout

    def run(self, script: str, check: bool = True) -> SshResult:
        cmd = [
            "wsl", "-d", self._distro, "--cd", "/tmp", "--",
            "ssh", *SSH_OPTS, self._host, "bash -s",
        ]
        # Send stdin as raw bytes with explicit \n line endings. subprocess's
        # text mode translates \n -> \r\n on Windows, which breaks bash badly
        # (e.g. "set: -\r: invalid option") — bytes mode bypasses that.
        payload = script.replace("\r\n", "\n").encode("utf-8")
        try:
            proc = subprocess.run(
                cmd,
                input=payload,
                capture_output=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise SshError(f"ssh command timed out after {self._timeout}s") from exc
        result = SshResult(
            proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
        )
        if check and proc.returncode != 0:
            raise SshError(
                f"remote script failed (rc={proc.returncode}): {proc.stderr.strip()[:500]}"
            )
        return result
