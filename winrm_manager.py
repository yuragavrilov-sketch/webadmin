import json
import winrm
from typing import Optional


class WinRMError(Exception):
    pass


_STATUS_MAP = {
    1: "Stopped", 2: "StartPending", 3: "StopPending",
    4: "Running", 5: "ContinuePending", 6: "PausePending", 7: "Paused",
}
_START_TYPE_MAP = {0: "Boot", 1: "System", 2: "Automatic", 3: "Manual", 4: "Disabled"}


def _parse_svc(raw: dict) -> dict:
    status_val = raw.get("Status", 0)
    status_str = _STATUS_MAP.get(status_val, str(status_val)) if isinstance(status_val, int) else str(status_val)
    start_val = raw.get("StartType", 3)
    start_str = _START_TYPE_MAP.get(start_val, str(start_val)) if isinstance(start_val, int) else str(start_val)
    return {
        "name": raw.get("Name", ""),
        "display_name": raw.get("DisplayName", ""),
        "status": status_str,
        "start_type": start_str,
        "error": None,
    }


class WinRMManager:
    def __init__(self, hostname: str, port: int, username: str, password: str,
                 use_ssl: bool = False, timeout: int = 30):
        self.hostname = hostname
        self.port = port
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.timeout = timeout
        self._session: Optional[winrm.Session] = None

    def _get_session(self) -> winrm.Session:
        if self._session is None:
            transport = "ssl" if self.use_ssl else "ntlm"
            protocol = "https" if self.use_ssl else "http"
            endpoint = f"{protocol}://{self.hostname}:{self.port}/wsman"
            self._session = winrm.Session(
                endpoint,
                auth=(self.username, self.password),
                transport=transport,
                server_cert_validation="ignore" if self.use_ssl else "ignore",
                operation_timeout_sec=self.timeout,
                read_timeout_sec=self.timeout + 10,
            )
        return self._session

    def _run_ps(self, script: str) -> tuple[str, str, int]:
        session = self._get_session()
        result = session.run_ps(script)
        stdout = result.std_out.decode("utf-8", errors="replace").strip()
        stderr = result.std_err.decode("utf-8", errors="replace").strip()
        return stdout, stderr, result.status_code

    def list_services(self) -> list[dict]:
        """Return all services on the remote host (used for browsing when adding configs)."""
        script = "Get-Service | Select-Object Name, DisplayName, Status, StartType | ConvertTo-Json -Compress"
        stdout, stderr, code = self._run_ps(script)
        if code != 0:
            raise WinRMError(f"Failed to list services: {stderr}")
        raw = json.loads(stdout)
        if isinstance(raw, dict):
            raw = [raw]
        return sorted([_parse_svc(s) for s in raw], key=lambda s: s["display_name"].lower())

    def get_services_by_names(self, names: list[str]) -> list[dict]:
        """Fetch status for a specific list of service names in a single PS call.

        Returns one dict per name in the same order. If a service is not found on
        the remote host the dict will have ``error`` set to an error message and
        ``status`` set to ``"Unknown"``.
        """
        if not names:
            return []
        # Build a safe PS array literal — service names must not contain single quotes
        safe_names = [n.replace("'", "") for n in names]
        ps_array = "@(" + ", ".join(f"'{n}'" for n in safe_names) + ")"
        script = f"""
$names = {ps_array}
$out = @()
foreach ($n in $names) {{
    try {{
        $s = Get-Service -Name $n -ErrorAction Stop
        $out += [PSCustomObject]@{{
            Name        = $s.Name
            DisplayName = $s.DisplayName
            Status      = $s.Status.value__
            StartType   = $s.StartType.value__
            Error       = $null
        }}
    }} catch {{
        $out += [PSCustomObject]@{{
            Name        = $n
            DisplayName = $n
            Status      = -1
            StartType   = -1
            Error       = $_.Exception.Message
        }}
    }}
}}
$out | ConvertTo-Json -Compress
"""
        stdout, stderr, code = self._run_ps(script)
        if code != 0:
            raise WinRMError(f"Failed to query services: {stderr}")
        raw = json.loads(stdout)
        if isinstance(raw, dict):
            raw = [raw]
        results = []
        for item in raw:
            parsed = _parse_svc(item)
            if item.get("Error"):
                parsed["status"] = "Unknown"
                parsed["error"] = item["Error"]
            results.append(parsed)
        return results

    def get_service(self, service_name: str) -> dict:
        """Fetch rich details for a single service via CIM (path, description, account, PID)."""
        safe = service_name.replace("'", "")
        script = f"""
$s = Get-CimInstance Win32_Service -Filter "Name='{safe}'" -ErrorAction SilentlyContinue
if (-not $s) {{ Write-Error "Service not found: {safe}"; exit 1 }}
[PSCustomObject]@{{
    Name        = $s.Name
    DisplayName = $s.DisplayName
    State       = $s.State
    StartMode   = $s.StartMode
    PathName    = $s.PathName
    Description = $s.Description
    StartName   = $s.StartName
    ProcessId   = $s.ProcessId
}} | ConvertTo-Json -Compress
"""
        stdout, stderr, code = self._run_ps(script)
        if code != 0:
            return {
                "name": service_name, "display_name": service_name,
                "status": "Unknown", "start_type": "",
                "path": "", "description": "", "account": "", "pid": 0,
                "error": stderr or "Service not found",
            }
        raw = json.loads(stdout)
        state_map = {
            "Running": "Running", "Stopped": "Stopped", "Paused": "Paused",
            "Start Pending": "StartPending", "Stop Pending": "StopPending",
            "Continue Pending": "ContinuePending", "Pause Pending": "PausePending",
        }
        start_map = {"Auto": "Automatic", "Manual": "Manual", "Disabled": "Disabled",
                     "Boot": "Boot", "System": "System"}
        state = raw.get("State") or ""
        return {
            "name":         raw.get("Name") or service_name,
            "display_name": raw.get("DisplayName") or service_name,
            "status":       state_map.get(state, state) or "Unknown",
            "start_type":   start_map.get(raw.get("StartMode") or "", raw.get("StartMode") or ""),
            "path":         raw.get("PathName") or "",
            "description":  raw.get("Description") or "",
            "account":      raw.get("StartName") or "",
            "pid":          int(raw.get("ProcessId") or 0),
            "error":        None,
        }

    def start_service(self, service_name: str) -> str:
        script = f"Start-Service -Name '{service_name}' -ErrorAction Stop; 'OK'"
        stdout, stderr, code = self._run_ps(script)
        if code != 0:
            raise WinRMError(stderr or "Failed to start service")
        return stdout

    def stop_service(self, service_name: str) -> str:
        script = f"Stop-Service -Name '{service_name}' -Force -ErrorAction Stop; 'OK'"
        stdout, stderr, code = self._run_ps(script)
        if code != 0:
            raise WinRMError(stderr or "Failed to stop service")
        return stdout

    def restart_service(self, service_name: str) -> str:
        script = f"Restart-Service -Name '{service_name}' -Force -ErrorAction Stop; 'OK'"
        stdout, stderr, code = self._run_ps(script)
        if code != 0:
            raise WinRMError(stderr or "Failed to restart service")
        return stdout

    # Text-based extensions collected during config snapshots
    _TEXT_EXTENSIONS = (
        ".json", ".xml", ".config", ".ini", ".yaml", ".yml",
        ".txt", ".properties", ".toml", ".cfg", ".conf", ".env",
    )

    def get_service_config_dir(self, service_name: str) -> str:
        """Derive the Config/ path from the service exe via CIM. Returns '' if not found."""
        safe = service_name.replace("'", "")
        script = f"""
$s = Get-CimInstance Win32_Service -Filter "Name='{safe}'" -ErrorAction SilentlyContinue
if (-not $s) {{ Write-Output ''; exit 0 }}
$exe = ($s.PathName -replace '"', '' -split ' ')[0]
$dir = Split-Path $exe -Parent
$cfg = Join-Path $dir 'Config'
if (Test-Path $cfg) {{ $cfg }} else {{ '' }}
"""
        stdout, stderr, code = self._run_ps(script)
        if code != 0:
            raise WinRMError(f"Cannot detect Config dir for '{service_name}': {stderr}")
        return stdout.strip()

    def list_config_files(self, config_dir: str) -> list[str]:
        """Return relative paths of text config files inside *config_dir* (recursive)."""
        safe = config_dir.replace("'", "").replace('"', "")
        includes = " ".join(f"'*{ext}'" for ext in self._TEXT_EXTENSIONS)
        script = f"""
$base = '{safe}'
if (-not (Test-Path $base)) {{ Write-Output '[]'; exit 0 }}
$files = Get-ChildItem -Path $base -Recurse -File -Include {includes} |
    ForEach-Object {{ $_.FullName.Substring($base.Length).TrimStart('\\') }}
if ($files) {{ $files | ConvertTo-Json -Compress }} else {{ Write-Output '[]' }}
"""
        stdout, stderr, code = self._run_ps(script)
        if code != 0:
            raise WinRMError(f"Cannot list config files in '{config_dir}': {stderr}")
        stdout = stdout.strip()
        if not stdout or stdout == "null":
            return []
        raw = json.loads(stdout)
        return [raw] if isinstance(raw, str) else list(raw)

    def read_config_file(self, file_path: str) -> str:
        """Read a remote text file as UTF-8 string."""
        safe = file_path.replace("'", "").replace('"', "")
        script = f"Get-Content -Path '{safe}' -Raw -Encoding UTF8 -ErrorAction Stop"
        stdout, stderr, code = self._run_ps(script)
        if code != 0:
            raise WinRMError(f"Cannot read '{file_path}': {stderr}")
        return stdout

    def test_connection(self) -> tuple[bool, str]:
        try:
            stdout, stderr, code = self._run_ps("$env:COMPUTERNAME")
            if code == 0:
                return True, f"Connected to {stdout}"
            return False, stderr
        except Exception as e:
            return False, str(e)
