#!/usr/bin/env python3
"""Install only the local client, capacity reporter, dashboard gateway and skill."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def restricted(path,text):
    fd=os.open(path,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
    with os.fdopen(fd,"w") as f:f.write(text)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config",type=Path,required=True)
    p.add_argument("--build-path",type=Path)
    p.add_argument("--no-start",action="store_true")
    args=p.parse_args()
    config=json.loads(args.config.read_text())
    if config.get("worker") not in ("oliver-mini","oliver-mac"):
        p.error("Use a worker config")
    destination=Path.home()/".local/share/nova-coordinator"
    destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    for folder in ("coordinator","web"):
        for source in (ROOT/folder).iterdir():
            if source.is_file() and source.suffix in (".py",".html",".css",".js"):
                target=destination/folder/source.name
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(source,target)
    cfg=Path.home()/".config/nova-coordinator"
    cfg.mkdir(parents=True,exist_ok=True,mode=0o700)
    shutil.copy2(config["ca_file"],cfg/"ca.crt")
    config["ca_file"]=str(cfg/"ca.crt")
    if args.build_path:config["build_path"]=str(args.build_path)
    config.setdefault("disk_reserve_gb",50)
    config.setdefault("capabilities",["macos","development"])
    restricted(cfg/"client.json",json.dumps(config,indent=2))
    binpath=Path.home()/".local/bin";binpath.mkdir(parents=True,exist_ok=True)
    launcher=binpath/"nova-coord"
    launcher.write_text("#!/usr/bin/env python3\nimport sys\nsys.path.insert(0,"+repr(str(destination))+")\nfrom coordinator.client import main\nmain()\n")
    launcher.chmod(0o755)
    skill=Path.home()/".codex/skills/nova-coordination"
    skill.mkdir(parents=True,exist_ok=True)
    shutil.copy2(ROOT/"skill/SKILL.md",skill/"SKILL.md")
    logs=cfg/"logs";logs.mkdir(exist_ok=True,mode=0o700)
    if sys.platform=="darwin" and not args.no_start:
        agents=Path.home()/"Library/LaunchAgents";agents.mkdir(parents=True,exist_ok=True)
        for mode in ("agent","ui"):
            label="surf.nova.coordinator."+mode
            plist=agents/(label+".plist")
            payload={"Label":label,"ProgramArguments":[sys.executable,"-m","coordinator.client",mode],
                "WorkingDirectory":str(destination),"RunAtLoad":True,"KeepAlive":True,"ThrottleInterval":15,
                "EnvironmentVariables":{"PYTHONDONTWRITEBYTECODE":"1","PYTHONUNBUFFERED":"1","PATH":"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/Users/oliverhu/.local/bin"},
                "StandardOutPath":str(logs/(mode+".log")),"StandardErrorPath":str(logs/(mode+".log"))}
            plist.write_bytes(plistlib.dumps(payload));plist.chmod(0o600)
            domain="gui/"+str(os.getuid())
            subprocess.run(["launchctl","bootout",domain+"/"+label],capture_output=True)
            subprocess.run(["launchctl","bootstrap",domain,str(plist)],check=True)
    print("Installed client for "+config["worker"])
    print("Dashboard: http://127.0.0.1:18766")
    print("CLI: "+str(launcher))


if __name__=="__main__":main()
