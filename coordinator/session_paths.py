"""Fresh active namespace; old archived files never become current implicitly."""
import json
from pathlib import Path
import re

BASE=Path.home()/'.local/state/nova-cli-dispatch'

def root(config_path=None):
    path=config_path or Path.home()/'.config/nova-coordinator/client.json'
    if not path.exists():return BASE
    epoch=json.loads(path.read_text()).get('session_epoch')
    if epoch is None:return BASE
    if not isinstance(epoch,str) or not re.fullmatch('[0-9a-f]{32}',epoch):raise ValueError('Invalid session epoch namespace')
    return BASE/'epochs'/epoch
