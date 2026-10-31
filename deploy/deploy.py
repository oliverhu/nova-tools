#!/usr/bin/env python3
"""Deploy the coordinator to the existing private Kubernetes cluster over SSH.

Secrets travel over stdin and are never printed. Objects have ownership checks.
Source ConfigMaps are immutable; the runtime is pinned by digest. State uses a
retained local volume; snapshots use a separate NFS volume.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
NS="nova-coordinator"
MANAGER="nova-coordinator-deploy"
LABELS={"app.kubernetes.io/name":NS,"app.kubernetes.io/managed-by":MANAGER}
PYTHON="python@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704"


class Cluster:
    def __init__(self,host):self.host=host
    def run(self,args,data=None):
        cmd=["ssh","-o","BatchMode=yes","-o","ConnectTimeout=10","-o","StrictHostKeyChecking=yes",self.host,
             shlex.join(["kubectl","--kubeconfig","/home/pi/.kube/config",*args])]
        r=subprocess.run(cmd,input=data,capture_output=True,timeout=180)
        if r.returncode:
            raise RuntimeError("Cluster operation failed; submitted content suppressed" if data else r.stderr.decode()[-2500:])
        return r.stdout
    def existing(self,obj):
        m=obj["metadata"]
        args=(["-n",m["namespace"]] if m.get("namespace") else [])
        raw=self.run([*args,"get",obj["kind"],m["name"],"--ignore-not-found","-o","jsonpath={.metadata.labels.app\\.kubernetes\\.io/managed-by}"])
        if raw and raw.decode()!=MANAGER:raise RuntimeError("Refusing to modify unowned "+obj["kind"]+"/"+m["name"])
        # Empty label and a missing resource must be distinguished.
        exists=self.run([*args,"get",obj["kind"],m["name"],"--ignore-not-found","-o","name"])
        if exists and not raw:raise RuntimeError("Refusing to modify unlabeled resource "+m["name"])
        return bool(exists)
    def apply(self,obj):
        self.existing(obj)
        self.run(["apply","--server-side","--field-manager",MANAGER,"-f","-"],json.dumps(obj).encode())
        print("Applied "+obj["kind"]+"/"+obj["metadata"]["name"],flush=True)


def obj(kind,name,namespace=NS,**fields):
    api={"Deployment":"apps/v1","StorageClass":"storage.k8s.io/v1","NetworkPolicy":"networking.k8s.io/v1",'Issuer':'cert-manager.io/v1','Certificate':'cert-manager.io/v1'}.get(kind,"v1")
    return {"apiVersion":api,"kind":kind,"metadata":{"name":name,"labels":LABELS,**({"namespace":namespace} if namespace else {})},**fields}


def secure_file(path,text):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,"w") as f:f.write(text)


def prepare_tls(cluster,runtime):
    """Generate keys inside Kubernetes; distribute only the public CA."""
    bootstrap=obj('Issuer',NS+'-bootstrap',spec={'selfSigned':{}})
    root=obj('Certificate',NS+'-root',spec={'isCA':True,'commonName':'Nova Internal Coordination CA',
        'subject':{'organizations':['Nova Internal Services']},'secretName':NS+'-ca-keypair',
        'secretTemplate':{'labels':LABELS},'duration':'87600h','renewBefore':'8760h',
        'privateKey':{'algorithm':'RSA','size':3072,'rotationPolicy':'Never'},
        'usages':['cert sign','crl sign'],'issuerRef':{'name':NS+'-bootstrap','kind':'Issuer'}})
    issuer=obj('Issuer',NS+'-internal-ca',spec={'ca':{'secretName':NS+'-ca-keypair'}})
    leaf=obj('Certificate',NS+'-server',spec={'secretName':NS+'-tls-v2','secretTemplate':{'labels':LABELS},
        'commonName':'nova-coordinator.nova-coordinator.svc.cluster.local',
        'dnsNames':['nova-coordinator.nova-coordinator.svc.cluster.local'],
        'ipAddresses':['192.168.2.'+str(n) for n in [2,11,12,13,24]],
        'duration':'2160h','renewBefore':'360h','privateKey':{'algorithm':'RSA','size':3072,'rotationPolicy':'Always'},
        'usages':['digital signature','key encipherment','server auth'],'issuerRef':{'name':NS+'-internal-ca','kind':'Issuer'}})
    for item in [bootstrap,root]:cluster.apply(item)
    cluster.run(['-n',NS,'wait','--for=condition=Ready','certificate/'+NS+'-root','--timeout=60s'])
    for item in [issuer,leaf]:cluster.apply(item)
    cluster.run(['-n',NS,'wait','--for=condition=Ready','certificate/'+NS+'-server','--timeout=60s'])
    root_pem=base64.b64decode(cluster.run(['-n',NS,'get','secret',NS+'-ca-keypair','-o','jsonpath={.data.tls\\.crt}'])).decode()
    legacy=cluster.run(['-n',NS,'get','secret',NS+'-tls','--ignore-not-found','-o','jsonpath={.data.tls\\.crt}'])
    bundle=root_pem+(base64.b64decode(legacy).decode() if legacy else '')
    secure_file(runtime/'internal-root.crt',root_pem)
    secure_file(runtime/'ca-bundle.crt',bundle)
    cluster.apply(obj('ConfigMap',NS+'-trust',data={'root.crt':root_pem,'ca-bundle.crt':bundle,'client-version':'2'}))
    print('Internal CA and automatically renewed server certificate are ready',flush=True)


def manifests():
    files={}
    for folder in ("coordinator","web"):
        for path in sorted((ROOT/folder).iterdir()):
            if path.is_file() and path.suffix in (".py",".html",".css",".js"):
                files[folder+"__"+path.name]=(folder+"/"+path.name,path.read_text())
    digest=hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()
    code_name=NS+"-code-"+digest[:16]
    security={"allowPrivilegeEscalation":False,"readOnlyRootFilesystem":True,"capabilities":{"drop":["ALL"]}}
    deployment=obj("Deployment",NS,spec={"replicas":1,"strategy":{"type":"RollingUpdate","rollingUpdate":{"maxSurge":1,"maxUnavailable":0}},
        "selector":{"matchLabels":{"app.kubernetes.io/name":NS}},"template":{
            "metadata":{"labels":LABELS,"annotations":{"nova.coordinator/source-sha256":digest}},
            "spec":{"automountServiceAccountToken":False,"nodeSelector":{"kubernetes.io/arch":"amd64"},
                "securityContext":{"runAsNonRoot":True,"runAsUser":10001,"runAsGroup":10001,"fsGroup":10001,"seccompProfile":{"type":"RuntimeDefault"}},
                "containers":[{"name":"coordinator","image":PYTHON,"command":["python3","-m","coordinator.server"],"workingDir":"/app",
                    "env":[{"name":k,"value":v} for k,v in {"PYTHONDONTWRITEBYTECODE":"1","PYTHONUNBUFFERED":"1","COORD_CERT":"/run/tls/tls.crt","COORD_KEY":"/run/tls/tls.key","COORD_BACKUPS":"/backups"}.items()],
                    "securityContext":security,"ports":[{"containerPort":8443,"name":"https"}],
                    "resources":{"requests":{"cpu":"50m","memory":"64Mi"},"limits":{"cpu":"500m","memory":"256Mi"}},
                    "readinessProbe":{"httpGet":{"path":"/healthz","port":"https","scheme":"HTTPS"},"periodSeconds":5},
                    "livenessProbe":{"httpGet":{"path":"/healthz","port":"https","scheme":"HTTPS"},"periodSeconds":20},
                    "volumeMounts":[{"name":"source","mountPath":"/app","readOnly":True},{"name":"auth","mountPath":"/run/auth","readOnly":True},
                        {"name":"tls","mountPath":"/run/tls","readOnly":True},{"name":"state","mountPath":"/data"},{"name":"backups","mountPath":"/backups"},{"name":"tmp","mountPath":"/tmp"}]}],
                "volumes":[{"name":"source","configMap":{"name":code_name,"items":[{"key":k,"path":v[0]} for k,v in files.items()]}},
                    {"name":"auth","secret":{"secretName":NS+"-auth","defaultMode":288}},
                    {"name":"tls","secret":{"secretName":NS+"-tls-v2","defaultMode":288}},
                    {"name":"state","persistentVolumeClaim":{"claimName":NS+"-state"}},
                    {"name":"backups","persistentVolumeClaim":{"claimName":NS+"-backups"}},
                    {"name":"tmp","emptyDir":{"sizeLimit":"16Mi"}}]}}})
    return [
        obj("StorageClass",NS+"-local",namespace=None,provisioner="rancher.io/local-path",reclaimPolicy="Retain",volumeBindingMode="WaitForFirstConsumer"),
        obj("PersistentVolumeClaim",NS+"-state",spec={"storageClassName":NS+"-local","accessModes":["ReadWriteOnce"],"resources":{"requests":{"storage":"1Gi"}}}),
        obj("PersistentVolumeClaim",NS+"-backups",spec={"storageClassName":"nfs","accessModes":["ReadWriteOnce"],"resources":{"requests":{"storage":"1Gi"}}}),
        obj("ConfigMap",code_name,immutable=True,data={k:v[1] for k,v in files.items()}),
        deployment,
        obj("Service",NS,spec={"type":"NodePort","selector":{"app.kubernetes.io/name":NS},"ports":[{"name":"https","port":443,"targetPort":8443,"nodePort":31866}]}),
        obj("NetworkPolicy",NS,spec={"podSelector":{"matchLabels":{"app.kubernetes.io/name":NS}},"policyTypes":["Ingress","Egress"],"egress":[],
            # K3s NodePort forwarding can SNAT to a node's pod-CIDR gateway.
            "ingress":[{"from":[{"ipBlock":{"cidr":"192.168.2.0/24"}},{"ipBlock":{"cidr":"192.168.3.0/24"}},
                *[{"ipBlock":{"cidr":f"10.42.{i}.0/32"}} for i in range(8)],{"podSelector":{}}],"ports":[{"protocol":"TCP","port":8443}]}]})
    ],digest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host",default="pi@192.168.2.13")
    parser.add_argument("--apply",action="store_true")
    parser.add_argument('--prepare-tls',action='store_true',help='Prepare certificates and public trust before changing the running service')
    args=parser.parse_args()
    cluster=Cluster(args.host)
    resources,digest=manifests()
    runtime=ROOT/".runtime";runtime.mkdir(mode=0o700,exist_ok=True)
    if not args.apply and not args.prepare_tls:
        print(json.dumps({"namespace":NS,"objects":[x["kind"]+"/"+x["metadata"]["name"] for x in resources],"sha256":digest},indent=2));return
    cluster.apply(obj("Namespace",NS,namespace=None))
    prepare_tls(cluster,runtime)
    if args.prepare_tls:return
    auth_obj=obj("Secret",NS+"-auth",type="Opaque")
    auth_path=runtime/"credentials.json"
    if cluster.existing(auth_obj):
        if not auth_path.exists():
            # Recover only this service's own credentials into restricted local
            # files, without putting values in command arguments or output.
            import base64
            raw=cluster.run(["-n",NS,"get","secret",NS+"-auth","-o","json"])
            auth=json.loads(raw)
            secure_file(auth_path,base64.b64decode(auth["data"]["credentials.json"]).decode())
    else:
        if not auth_path.exists():secure_file(auth_path,json.dumps({name:secrets.token_urlsafe(40) for name in ("admin","oliver-mac","oliver-mini")}))
        cluster.apply({**auth_obj,"stringData":{"credentials.json":auth_path.read_text()}})
    for resource in resources:cluster.apply(resource)
    credentials=json.loads(auth_path.read_text())
    for actor,token in credentials.items():
        secure_file(runtime/(actor+".json"),json.dumps({"url":"https://192.168.2.13:31866","worker":actor,"token":token,
            "ca_file":str(runtime/'ca-bundle.crt'),'root_ca_file':str(runtime/'internal-root.crt')},indent=2))
    secure_file(runtime/"deployment.json",json.dumps({"namespace":NS,"sha256":digest,"url":"https://192.168.2.13:31866"},indent=2))
    subprocess.run([sys.executable,str(ROOT/'scripts/package-client.py'),'--host',args.host],check=True)
    print("Deployment applied. Check rollout before using https://192.168.2.13:31866",flush=True)


if __name__=="__main__":main()
