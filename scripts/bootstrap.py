#!/usr/bin/env python3
"""One-time client setup from a cluster-hosted bundle; no Mac-to-Mac transport."""
import argparse
import base64
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--worker",choices=["oliver-mac","oliver-mini"],required=True)
    p.add_argument("--cluster-host",default="pi@192.168.2.13")
    p.add_argument("--build-path")
    p.add_argument("--repository-path")
    p.add_argument('--trust-internal-ca',action='store_true')
    a=p.parse_args()
    def remote(command):
        return subprocess.check_output(["ssh","-o","BatchMode=yes","-o","ConnectTimeout=10","-o","StrictHostKeyChecking=yes",a.cluster_host,command])
    bundle=remote("cat /home/pi/.local/share/nova-coordinator/client.tar.gz")
    auth=json.loads(remote("kubectl --kubeconfig /home/pi/.kube/config -n nova-coordinator get secret nova-coordinator-auth -o json"))
    credentials=json.loads(base64.b64decode(auth["data"]["credentials.json"]))
    trust=json.loads(remote('kubectl --kubeconfig /home/pi/.kube/config -n nova-coordinator get configmap nova-coordinator-trust -o json'))['data']
    cert=trust['ca-bundle.crt'].encode()
    with tempfile.TemporaryDirectory(prefix="nova-coordinator-install-") as temp:
        root=Path(temp)
        with tarfile.open(fileobj=io.BytesIO(bundle),mode="r:gz") as archive:
            for member in archive.getmembers():
                path=Path(member.name)
                if path.is_absolute() or ".." in path.parts or not member.isfile():
                    raise RuntimeError("Unsafe bundle entry")
                target=root/path;target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
        (root/"ca.crt").write_bytes(cert)
        (root/'internal-root.crt').write_text(trust['root.crt'])
        config=root/"client.json"
        fd=os.open(config,os.O_CREAT|os.O_WRONLY,0o600)
        existing=Path.home()/".config/nova-coordinator/client.json"
        previous=json.loads(existing.read_text()) if existing.exists() else {}
        if previous and previous.get("worker") != a.worker:
            raise RuntimeError("Existing client belongs to another worker")
        with os.fdopen(fd,"w") as f:json.dump({**previous,"worker":a.worker,"url":"https://192.168.2.13:31866","token":credentials[a.worker],"ca_file":str(root/"ca.crt"),'root_ca_file':str(root/'internal-root.crt')},f)
        import sys
        subprocess.run([sys.executable,str(root/"scripts/install-client.py"),"--config",str(config),*(["--build-path",a.build_path] if a.build_path else []),*(["--repository-path",a.repository_path] if a.repository_path else []),*(['--trust-internal-ca'] if a.trust_internal_ca else [])],check=True)


if __name__=="__main__":main()
