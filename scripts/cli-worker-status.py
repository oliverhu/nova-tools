#!/usr/bin/env python3
"""Read the current status of explicitly configured SSH CLI workers."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess


def read(host):
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                             host, 'nova-cli-worker status'], capture_output=True, text=True, timeout=25)
    if result.returncode:
        return host, None
    return host, json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', action='append', required=True)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    # SSH host values are passed as arguments, never shell text or task input.
    if any(not h or h.startswith('-') or any(c.isspace() for c in h) for h in args.host):
        parser.error('Use configured SSH host aliases')
    with ThreadPoolExecutor(max_workers=min(8, len(args.host))) as pool:
        for host, value in pool.map(read, args.host):
            if args.json:
                print(json.dumps({'host': host, 'status': value}))
                continue
            if value is None:
                print(host+': connection unavailable; execution state unknown\n')
                continue
            latest = value.get('latest') or {}
            task = value.get('task') or {}
            print(host+': '+latest.get('status', 'idle')+' | '+latest.get('model', 'configured model')+' '+latest.get('reasoning', ''))
            if task:
                print('  '+task['id']+' — '+task['title']+' ['+task['status']+']')
                if task.get('progress'):
                    print('  '+task['progress'])
            if value.get('latest_message'):
                print('  Latest: '+value['latest_message'].replace('\n', ' ')[:900])
            if value.get('api_unavailable'):
                print('  Queue unavailable; shown run state is local evidence only.')
            print()


if __name__ == '__main__':
    main()
