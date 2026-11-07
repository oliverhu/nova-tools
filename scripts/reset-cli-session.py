#!/usr/bin/env python3
"""Explicit administrator archive/epoch reset; no VM, agent or permission action."""
import argparse,json,os,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from coordinator.client import Client
from coordinator import cli_session_reset as reset


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('plan','prepare','verify','apply','install'))
    parser.add_argument('--admin-config',type=Path)
    parser.add_argument('--worker',required=True)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--request',type=Path);parser.add_argument('--request-sha256')
    parser.add_argument('--archive',type=Path);parser.add_argument('--archive-sha256')
    parser.add_argument('--plan',type=Path);parser.add_argument('--plan-sha256')
    parser.add_argument('--decision',type=Path);parser.add_argument('--decision-sha256');parser.add_argument('--output',type=Path)
    parser.add_argument('--policy',type=Path);parser.add_argument('--policy-sha256')
    parser.add_argument('--agent-stopped',action='store_true',help='Attest actual owned capacity-agent stop; no process stop is performed')
    parser.add_argument('--epoch');parser.add_argument('--client-config',type=Path);parser.add_argument('--dispatch-config',type=Path)
    args=parser.parse_args()
    if args.operation=='prepare':
        if not all((args.plan,args.plan_sha256,args.archive,args.archive_sha256,args.policy,args.policy_sha256,args.decision,args.decision_sha256,args.output)):parser.error('Exact plan/archive/base-policy/decision references and new output required')
        result=reset.prepare({'path':str(args.plan),'sha256':args.plan_sha256},{'path':str(args.archive),'sha256':args.archive_sha256},{'path':str(args.policy),'sha256':args.policy_sha256},{'path':str(args.decision),'sha256':args.decision_sha256},args.root,args.output,agent_stopped=args.agent_stopped)
        print(json.dumps(result));return
    if args.operation=='verify':
        if not all((args.archive,args.archive_sha256,args.policy,args.policy_sha256)):parser.error('Exact archive/policy references required')
        policy=reset.load({'path':str(args.policy),'sha256':args.policy_sha256})
        print(json.dumps(reset.verify({'path':str(args.archive),'sha256':args.archive_sha256},policy,args.root,agent_stopped=args.agent_stopped)))
        return
    if args.operation=='install':
        if not args.client_config:parser.error('Exact existing worker client config required')
        client=Client(args.client_config)
    else:
        if not args.admin_config:parser.error('Use the existing administrator context; never copy its credential to a guest')
        client=Client(args.admin_config)
    if args.operation=='plan':result=client.request('/api/workers/'+args.worker+'/session-reset/plan',{})
    elif args.operation=='apply':
        if not all((args.request,args.request_sha256,args.archive,args.archive_sha256)):parser.error('Exact request/archive references required')
        result=reset.apply(client,{'path':str(args.request),'sha256':args.request_sha256},{'path':str(args.archive),'sha256':args.archive_sha256},args.root)
    else:
        if not all((args.epoch,args.client_config,args.dispatch_config)):parser.error('Exact epoch/client/dispatch paths required')
        result=reset.install(client,args.worker,args.epoch,args.client_config,args.dispatch_config,args.root)
    print(json.dumps(result))

if __name__=='__main__':main()
