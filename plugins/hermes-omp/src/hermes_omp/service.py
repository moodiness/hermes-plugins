from __future__ import annotations

import dataclasses
import os
import plistlib
import shlex
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional
from xml.sax.saxutils import escape

from .core import atomic_write, slug

LABEL_PREFIX = "ai.hermes.omp."

@dataclasses.dataclass(frozen=True)
class ServiceSnapshot:
    path: Path
    data: Optional[bytes]
    registered: bool



class ServiceBackend(ABC):
    def __init__(self, root: Path, runner=subprocess.run): self.root, self.runner = root, runner
    @abstractmethod
    def definition(self, name: str, command: list[str], cwd: str, restart_policy: str) -> Any: ...
    @abstractmethod
    def definition_path(self, name: str) -> Path: ...
    @abstractmethod
    def snapshot(self, name: str) -> ServiceSnapshot: ...
    @abstractmethod
    def restore(self, name: str, snapshot: ServiceSnapshot) -> None: ...
    @abstractmethod
    def install(self, name: str, command: list[str], cwd: str, restart_policy: str, activate: bool = True) -> Path: ...
    @abstractmethod
    def start(self, name: str) -> None: ...
    @abstractmethod
    def stop(self, name: str) -> None: ...
    @abstractmethod
    def remove(self, name: str) -> None: ...


class LaunchdBackend(ServiceBackend):
    def definition(self, name, command, cwd, restart_policy):
        keep: Any = False if restart_policy == "never" else True if restart_policy == "always" else {"SuccessfulExit": False}
        return {"Label": LABEL_PREFIX + slug(name), "ProgramArguments": command, "WorkingDirectory": cwd, "EnvironmentVariables": {"HERMES_HOME": os.environ.get("HERMES_HOME", str(Path.home()/".hermes")), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}, "RunAtLoad": False, "KeepAlive": keep, "ProcessType": "Background", "ThrottleInterval": 10, "StandardOutPath": str(self.root / "logs" / f"{slug(name)}.service.log"), "StandardErrorPath": str(self.root / "logs" / f"{slug(name)}.service.log")}
    def definition_path(self, name): return Path.home()/"Library"/"LaunchAgents"/f"{LABEL_PREFIX}{slug(name)}.plist"
    def snapshot(self, name):
        path=self.definition_path(name); result=self.runner(["launchctl","print",f"gui/{os.getuid()}/{LABEL_PREFIX}{slug(name)}"],check=False)
        return ServiceSnapshot(path,path.read_bytes() if path.exists() else None,getattr(result,"returncode",1)==0)
    def restore(self,name,snapshot):
        self.runner(["launchctl","bootout",f"gui/{os.getuid()}",str(snapshot.path)],check=False)
        if snapshot.data is None: snapshot.path.unlink(missing_ok=True); return
        atomic_write(snapshot.path,snapshot.data,0o600)
        if snapshot.registered: self.runner(["launchctl","bootstrap",f"gui/{os.getuid()}",str(snapshot.path)],check=True)
    def install(self, name, command, cwd, restart_policy, activate=True):
        path=self.definition_path(name); path.parent.mkdir(parents=True, exist_ok=True); atomic_write(path, plistlib.dumps(self.definition(name,command,cwd,restart_policy)).decode(), 0o600)
        if activate:
            self.runner(["launchctl","bootstrap",f"gui/{os.getuid()}",str(path)],check=True)
        return path
    def start(self,name): self.runner(["launchctl","kickstart","-k",f"gui/{os.getuid()}/{LABEL_PREFIX}{slug(name)}"],check=True)
    def stop(self,name): self.runner(["launchctl","kill","SIGTERM",f"gui/{os.getuid()}/{LABEL_PREFIX}{slug(name)}"],check=False)
    def remove(self,name):
        path=self.definition_path(name); self.runner(["launchctl","bootout",f"gui/{os.getuid()}",str(path)],check=False)
        if path.exists(): path.unlink()


class SystemdBackend(ServiceBackend):
    def definition(self,name,command,cwd,restart_policy):
        restart={"never":"no","on-failure":"on-failure","always":"always"}[restart_policy]
        return f"[Unit]\nDescription=Hermes OMP session {slug(name)}\n\n[Service]\nType=simple\nWorkingDirectory={cwd}\nExecStart={shlex.join(command)}\nRestart={restart}\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n"
    def definition_path(self,name): return Path.home()/".config"/"systemd"/"user"/f"hermes-omp-{slug(name)}.service"
    def snapshot(self,name):
        path=self.definition_path(name); result=self.runner(["systemctl","--user","is-enabled",f"hermes-omp-{slug(name)}.service"],check=False)
        return ServiceSnapshot(path,path.read_bytes() if path.exists() else None,getattr(result,"returncode",1)==0)
    def restore(self,name,snapshot):
        unit=f"hermes-omp-{slug(name)}.service"; self.runner(["systemctl","--user","disable","--now",unit],check=False)
        if snapshot.data is None: snapshot.path.unlink(missing_ok=True)
        else: atomic_write(snapshot.path,snapshot.data,0o600)
        self.runner(["systemctl","--user","daemon-reload"],check=False)
        if snapshot.registered: self.runner(["systemctl","--user","enable",unit],check=True)
    def install(self,name,command,cwd,restart_policy,activate=True):
        path=self.definition_path(name); atomic_write(path,self.definition(name,command,cwd,restart_policy),0o600)
        if activate: self.runner(["systemctl","--user","daemon-reload"],check=True)
        return path
    def start(self,name): self.runner(["systemctl","--user","start",f"hermes-omp-{slug(name)}.service"],check=True)
    def stop(self,name): self.runner(["systemctl","--user","stop",f"hermes-omp-{slug(name)}.service"],check=False)
    def remove(self,name):
        self.stop(name); path=self.definition_path(name)
        if path.exists(): path.unlink()
        self.runner(["systemctl","--user","daemon-reload"],check=False)


class WindowsTaskBackend(ServiceBackend):
    def definition(self,name,command,cwd,restart_policy):
        args=" ".join(shlex.quote(x) for x in command[1:])
        restart="" if restart_policy=="never" else f"<RestartOnFailure><Interval>PT1M</Interval><Count>{'3' if restart_policy=='on-failure' else '999'}</Count></RestartOnFailure>"
        return f'''<?xml version="1.0" encoding="UTF-16"?><Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers><Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>{restart}</Settings><Actions Context="Author"><Exec><Command>{escape(command[0])}</Command><Arguments>{escape(args)}</Arguments><WorkingDirectory>{escape(cwd)}</WorkingDirectory></Exec></Actions></Task>'''
    def definition_path(self,name): return self.root/"services"/f"hermes-omp-{slug(name)}.xml"
    def snapshot(self,name):
        path=self.definition_path(name); result=self.runner(["schtasks","/Query","/TN",f"HermesOMP-{slug(name)}"],check=False)
        return ServiceSnapshot(path,path.read_bytes() if path.exists() else None,getattr(result,"returncode",1)==0)
    def restore(self,name,snapshot):
        task=f"HermesOMP-{slug(name)}"; self.runner(["schtasks","/Delete","/TN",task,"/F"],check=False)
        if snapshot.data is None: snapshot.path.unlink(missing_ok=True); return
        atomic_write(snapshot.path,snapshot.data,0o600)
        if snapshot.registered: self.runner(["schtasks","/Create","/TN",task,"/XML",str(snapshot.path),"/F"],check=True)
    def install(self,name,command,cwd,restart_policy,activate=True):
        path=self.definition_path(name); atomic_write(path,self.definition(name,command,cwd,restart_policy),0o600)
        if activate: self.runner(["schtasks","/Create","/TN",f"HermesOMP-{slug(name)}","/XML",str(path),"/F"],check=True)
        return path
    def start(self,name): self.runner(["schtasks","/Run","/TN",f"HermesOMP-{slug(name)}"],check=True)
    def stop(self,name): self.runner(["schtasks","/End","/TN",f"HermesOMP-{slug(name)}"],check=False)
    def remove(self,name): self.runner(["schtasks","/Delete","/TN",f"HermesOMP-{slug(name)}","/F"],check=False)


def backend_for(platform: str | None = None, root: Path | None = None) -> ServiceBackend:
    value=(platform or sys.platform).lower(); target=root or Path(os.environ.get("HERMES_HOME",Path.home()/".hermes"))/"omp"
    if value.startswith("darwin"): return LaunchdBackend(target)
    if value.startswith("linux"): return SystemdBackend(target)
    if value.startswith("win"): return WindowsTaskBackend(target)
    raise ValueError(f"unsupported service platform: {value}")
