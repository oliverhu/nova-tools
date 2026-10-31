#!/usr/bin/env python3
"""Publish the client installer on the cluster host, excluding all secrets."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host',default='pi@192.168.2.13')
    args=p.parse_args()
    root=Path(__file__).resolve().parents[1]
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w:gz') as archive:
        for folder in ['coordinator','web','scripts','skill']:
            for path in sorted((root/folder).rglob('*')):
                if path.is_file() and not path.is_symlink() and path.suffix in ['.py','.html','.css','.js','.md']:
                    archive.add(path,arcname=str(path.relative_to(root)),recursive=False)
    bundle=stream.getvalue()
    ssh=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',args.host]
    subprocess.run(ssh+['umask 077; mkdir -p /home/pi/.local/share/nova-coordinator; cat > /home/pi/.local/share/nova-coordinator/client.tar.gz.tmp; mv /home/pi/.local/share/nova-coordinator/client.tar.gz.tmp /home/pi/.local/share/nova-coordinator/client.tar.gz'],input=bundle,check=True)
    subprocess.run(ssh+['umask 077; cat > /home/pi/.local/share/nova-coordinator/bootstrap.py.tmp; mv /home/pi/.local/share/nova-coordinator/bootstrap.py.tmp /home/pi/.local/share/nova-coordinator/bootstrap.py'],input=(root/'scripts/bootstrap.py').read_bytes(),check=True)
    print(json.dumps({'client_bundle_bytes':len(bundle),'sha256':hashlib.sha256(bundle).hexdigest(),'host':args.host}))


if __name__=='__main__':main()
