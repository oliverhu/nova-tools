#!/usr/bin/env python3
"""Send explicit local attachments over an existing authorized, pinned SSH route.

The private --note-file is the usual exact inbox binding/text plus a local_media
array of {path, media_type}. No route discovery, model, chat or credentials.
"""
import argparse
import json
from pathlib import Path
import re
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from coordinator.cli_media import pack, bounded_read, MAX_PACKET, tool_output


def send(note,host):
    if not re.fullmatch('[A-Za-z][A-Za-z0-9-]{0,79}',host):
        raise ValueError('Existing configured SSH alias required')
    request=dict(note)
    packet=json.dumps(pack(request,request.pop('local_media')),separators=(',',':')).encode()
    if len(packet)>MAX_PACKET:raise ValueError('Media packet limit exceeded')
    # Constant remote command; filenames and content exist only on stdin.
    command=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
             '-o','ControlMaster=no','-o','ControlPath=none','-o','ConnectTimeout=10',host,
             '/usr/bin/python3 .local/share/nova-coordinator/coordinator/cli_media.py']
    try:
        raw=tool_output(command,limit=24000,timeout=90,input_data=packet)
    except (ValueError,OSError) as error:
        raise RuntimeError('Trusted media transport failed; delivery may be unknown. Inspect exact message status before immutable retry.') from error
    return json.loads(raw)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',required=True)
    parser.add_argument('--note-file',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(send(json.loads(bounded_read(args.note_file,24000,True)),args.host)))


if __name__=='__main__':main()
