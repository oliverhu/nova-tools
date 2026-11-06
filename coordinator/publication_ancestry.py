"""Read-only Git ancestry attestation; no fetch, checkout, ref or index writes."""
import os
import re
import subprocess
import time


def attestation(repository, head, snapshot, timeout=15):
    if not snapshot or snapshot.get('capabilities', {}).get('publication_policy') != 1:
        return None
    repo = next((r for r in snapshot.get('repositories', []) if r['name'] == repository['name']), None)
    if not repo:
        return None
    candidates = {repo['published_head']}
    # This compact all-open list is not truncated with the dashboard's recent
    # 500-task window, so an older pending target cannot silently lose proof.
    for target in snapshot.get('publication_candidates', []):
        if target.get('repository') == repository['name']:
            candidates.add(target.get('commit'))
    for task in snapshot.get('tasks', []):
        target = task.get('result', {}).get('release_target', {})
        if task.get('task_type') == 'publish' and task['status'] not in ('completed', 'cancelled') and target.get('repository') == repository['name']:
            candidates.add(target.get('commit'))
    if len(candidates) > 512 or not re.fullmatch('[0-9a-f]{40}', head):
        return None
    ancestors = []; deadline = time.monotonic() + timeout
    for commit in sorted(c for c in candidates if isinstance(c, str) and re.fullmatch('[0-9a-f]{40}', c)):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            result = subprocess.run(['git', '-C', repository['path'], 'merge-base', '--is-ancestor', commit, head],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=remaining,
                env={**os.environ, 'GIT_OPTIONAL_LOCKS':'0', 'GIT_TERMINAL_PROMPT':'0'})
        except (OSError, subprocess.TimeoutExpired):
            break
        if result.returncode == 0:
            ancestors.append(commit)
    return {'head':head, 'published_head':repo['published_head'], 'ancestors':ancestors}
