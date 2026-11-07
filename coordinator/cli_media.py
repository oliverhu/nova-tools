"""Bounded local media for the existing authenticated, ordered CLI inbox.

No URL readers, credentials, model calls, schedules or consent operations.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import tempfile

MAX_COUNT = 4
MAX_FILE = 16 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
MAX_RUN = 128 * 1024 * 1024
RESERVE = 256 * 1024 * 1024
MAX_PACKET = 45 * 1024 * 1024
MAX_DIMENSION = 8192
MAX_PIXELS = 32 * 1024 * 1024
MAX_FRAMES = 5
MAX_DURATION = 120
MAX_FRAME_BYTES = 4 * 1024 * 1024
TYPES = {'image/png': '.png', 'image/jpeg': '.jpg', 'video/mp4': '.mp4', 'video/quicktime': '.mov',
         'image/gif':'.gif','image/webp':'.webp','video/webm':'.webm'}
INSPECTABLE = {'image/png','image/jpeg','video/mp4','video/quicktime'}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(path, private=False):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Canonical nonsymlink local media path required')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or (private and info.st_mode & 0o077):
        raise ValueError('Owned regular media file required')
    return path


def bounded_read(path, limit=MAX_FILE, private=False):
    path = canonical(path, private)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > limit:
            raise ValueError('Media byte limit exceeded or empty file')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if any(getattr(before,k) != getattr(after,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')) or len(raw) != before.st_size:
        raise ValueError('Media changed during bounded read')
    return raw


def dimensions(raw, media_type):
    if media_type == 'image/png':
        if len(raw) < 33 or raw[:8] != b'\x89PNG\r\n\x1a\n' or raw[12:16] != b'IHDR':
            raise ValueError('Invalid PNG signature')
        w, h = struct.unpack('>II', raw[16:24])
    elif media_type == 'image/jpeg':
        if not raw.startswith(b'\xff\xd8'):
            raise ValueError('Invalid JPEG signature')
        pos = 2; w = h = 0
        while pos + 4 <= len(raw):
            if raw[pos] != 255: break
            marker = raw[pos+1]; pos += 2
            if marker == 255: pos -= 1; continue
            if marker in (0xd8, 0xd9, 0xda): break
            size = int.from_bytes(raw[pos:pos+2], 'big')
            if size < 2 or pos + size > len(raw): break
            if marker in (0xc0, 0xc1, 0xc2):
                if size < 7: break
                h, w = struct.unpack('>HH', raw[pos+3:pos+7]); break
            pos += size
    elif media_type not in INSPECTABLE:
        return None
    else:
        if len(raw) < 12 or raw[4:8] != b'ftyp':
            raise ValueError('Unsupported or corrupt video container')
        return None
    if not w or not h or max(w, h) > MAX_DIMENSION or w*h > MAX_PIXELS:
        raise ValueError('Image dimension limit exceeded or unsupported encoding')
    return {'width': w, 'height': h}


def validate_items(items):
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_COUNT:
        raise ValueError('One to four explicit media attachments required')
    total = 0
    for item in items:
        if not isinstance(item, dict) or set(item) != {'name','media_type','size','sha256','data_base64'}:
            raise ValueError('Exact versioned wire media fields required')
        if (not isinstance(item['name'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._ -]{0,119}', item['name'])
                or '..' in item['name'] or item['media_type'] not in TYPES
                or type(item['size']) is not int or not 0 < item['size'] <= MAX_FILE
                or not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}',item['sha256'])
                or not isinstance(item['data_base64'], str) or len(item['data_base64']) > 4*((MAX_FILE+2)//3)):
            raise ValueError('Unsafe name, unsupported type or media limit')
        total += item['size']
    if total > MAX_TOTAL: raise ValueError('Total media byte limit exceeded')


def pack(request, attachments):
    """Studio uses explicitly selected local files only; bytes travel on SSH stdin."""
    if not isinstance(attachments, list) or not 1 <= len(attachments) <= MAX_COUNT:
        raise ValueError('Explicit bounded local attachment list required')
    items = []; total = 0
    for entry in attachments:
        if not isinstance(entry,dict) or set(entry) != {'path','media_type'}: raise ValueError('Explicit local path/type required')
        if entry['media_type'] not in TYPES: raise ValueError('Unsupported media type')
        path = canonical(entry['path'])
        if path.stat().st_size + total > MAX_TOTAL: raise ValueError('Total media limit exceeded')
        raw = bounded_read(path); total += len(raw)
        dimensions(raw, entry['media_type'])
        items.append({'name':path.name,'media_type':entry['media_type'],'size':len(raw),
                      'sha256':sha(raw),'data_base64':base64.b64encode(raw).decode('ascii')})
    validate_items(items)
    return {**request, 'media': items}


def stage(run_dir, request, sequence):
    """Called under the message ledger lock after fresh account/task binding."""
    from .cli_messages import private, digest
    items = request['media']; validate_items(items)
    root = run_dir/'media'
    if not root.exists(): root.mkdir(mode=0o700)
    private(root, True)
    # Only bounded per-run attachment storage, including retained/held originals.
    used = 0
    for path in root.rglob('*'):
        if path.is_symlink(): raise ValueError('Media storage contains a symlink')
        if path.is_file(): used += path.stat().st_size
    wanted = sum(i['size'] for i in items)
    final = root/request['message_id']
    # Publication-to-ledger crash recovery still needs temporary verification space.
    addition = 0 if final.exists() else wanted
    if used + addition > MAX_RUN or shutil.disk_usage(root).free < RESERVE + wanted:
        raise ValueError('Media storage limit or free reserve reached')
    temp = Path(tempfile.mkdtemp(prefix='.stage-', dir=root))
    try:
        entries = []
        for i, item in enumerate(items):
            try: raw = base64.b64decode(item['data_base64'], validate=True)
            except (ValueError, TypeError) as error: raise ValueError('Corrupt media transport') from error
            if len(raw) != item['size'] or sha(raw) != item['sha256']:
                raise ValueError('Media content size or SHA256 mismatch')
            size = dimensions(raw, item['media_type'])
            name = str(i)+TYPES[item['media_type']]
            with (temp/name).open('xb') as stream:
                os.chmod(temp/name, 0o600); stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            entries.append({k:item[k] for k in ('name','media_type','size','sha256')} | {
                'index':i, 'guest_path':str(final/name), 'dimensions':size})
        target = {k:v for k,v in request.items() if k not in ('text','media')}
        manifest = {'schema':1,'binding':{**target,'sequence':sequence,'text_sha256':sha(request['text'].encode())},
                    'attachments':entries}
        from .cli_dispatch import save
        save(temp/'manifest.json',manifest)
        fd = os.open(temp, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
        if final.exists():
            # Crash between directory publication and ledger commit: exact retry only.
            private(final, True)
            old = json.loads(bounded_read(final/'manifest.json', 24000, True))
            if old != manifest: raise ValueError('Conflicting retained media; not overwritten')
            verify(run_dir, old, digest(old))
        else:
            os.rename(temp,final)
            fd = os.open(root, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
        return manifest, digest(manifest)
    finally:
        if temp.exists(): shutil.rmtree(temp)  # Only this invocation's private unpublished temp.


def verify(run_dir, manifest, expected):
    from .cli_messages import private, digest
    if digest(manifest) != expected or manifest.get('schema') != 1:
        raise ValueError('Media manifest hash/schema mismatch')
    target = manifest['binding']; root = run_dir/'media'/target['message_id']
    private(run_dir/'media', True); private(root,True)
    stored = json.loads(bounded_read(root/'manifest.json',24000,True))
    if stored != manifest: raise ValueError('Published media manifest changed')
    for i, item in enumerate(manifest['attachments']):
        path = root/(str(i)+TYPES[item['media_type']])
        if item['guest_path'] != str(path) or item['index'] != i:
            raise ValueError('Manifest media path or order mismatch')
        raw = bounded_read(path, private=True)
        if len(raw) != item['size'] or sha(raw) != item['sha256']:
            raise ValueError('Published media corrupted')
        if dimensions(raw,item['media_type']) != item['dimensions']:
            raise ValueError('Published media dimensions changed')
    return manifest


def tool_output(argv, limit=32768, timeout=15, input_data=None):
    """No shell. Kill and reap on wall/byte bound; never pipe unbounded output."""
    import selectors
    import time
    if input_data is not None and (not isinstance(input_data,bytes) or len(input_data)>MAX_PACKET):
        raise ValueError('Tool stdin limit exceeded')
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    output = bytearray(); deadline = time.monotonic()+timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout,selectors.EVENT_READ)
            written=0
            if input_data is not None:
                os.set_blocking(proc.stdin.fileno(),False)
                selector.register(proc.stdin,selectors.EVENT_WRITE)
            while selector.get_map():
                left = deadline-time.monotonic()
                if left <= 0: raise ValueError('Media tool time limit')
                ready=selector.select(min(left,0.2))
                for key, _ in ready:
                    if key.fileobj is proc.stdin:
                        written+=os.write(proc.stdin.fileno(),input_data[written:written+65536])
                        if written==len(input_data):
                            selector.unregister(proc.stdin);proc.stdin.close()
                    else:
                        chunk = os.read(proc.stdout.fileno(),min(65536,limit+1-len(output)))
                        if not chunk:
                            selector.unregister(proc.stdout);continue
                        output.extend(chunk)
                        if len(output)>limit: raise ValueError('Media tool output limit')
        if proc.wait(timeout=max(0.01,deadline-time.monotonic())): raise ValueError('Media tool failed')
        return bytes(output)
    finally:
        import signal
        try: os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError: pass
        proc.wait(); proc.stdout.close()
        if proc.stdin is not None and not proc.stdin.closed:proc.stdin.close()


def pinned_tool(config, name):
    selected = config.get('media_tools',{}).get(name)
    if not isinstance(selected,dict) or set(selected) != {'path','sha256'}:
        raise ValueError('Supported pinned '+name+' unavailable')
    path = canonical(selected['path'])
    if sha(bounded_read(path,128*1024*1024)) != selected['sha256']:
        raise ValueError('Media tool hash differs')
    return str(path)


def sample_video(run_dir, manifest, index, config):
    """Bounded representative frames; no audio or full-video understanding."""
    from .cli_messages import digest, private
    verify(run_dir,manifest,digest(manifest))
    item = manifest['attachments'][index]
    if not item['media_type'].startswith('video/'): raise ValueError('Video required')
    original = item['guest_path']
    probe = pinned_tool(config,'ffprobe'); ffmpeg = pinned_tool(config,'ffmpeg')
    metadata = json.loads(tool_output([probe,'-v','error','-protocol_whitelist','file','-show_entries',
        'format=duration:stream=codec_type,width,height,duration','-of','json',original]))
    streams = [s for s in metadata.get('streams',[]) if s.get('codec_type')=='video']
    duration = float(metadata.get('format',{}).get('duration','nan'))
    import math
    if (not math.isfinite(duration) or not 0 < duration <= MAX_DURATION or len(streams)!=1
            or any(type(streams[0].get(k)) is not int or not 0 < streams[0][k] <= MAX_DIMENSION for k in ('width','height'))
            or streams[0]['width']*streams[0]['height'] > MAX_PIXELS):
        raise ValueError('Video duration/stream/dimension limit or unsupported metadata')
    root = Path(original).parent
    final = root/('frames-'+str(index))
    if final.exists():
        private(final,True)
        retained=json.loads(bounded_read(final/'sampling.json',24000,True))
        verify_sampling(retained,item,digest(manifest))
        return retained
    run_used=sum(p.stat().st_size for p in (run_dir/'media').rglob('*') if p.is_file())
    if run_used+MAX_FRAMES*MAX_FRAME_BYTES>MAX_RUN:
        raise ValueError('Run media/frame storage limit reached')
    if shutil.disk_usage(root).free < RESERVE+MAX_FRAMES*MAX_FRAME_BYTES:
        raise ValueError('Frame storage reserve unavailable')
    final = root/('frames-'+str(index)); temp=Path(tempfile.mkdtemp(prefix='.frames-',dir=root))
    try:
        frames=[]
        for n in range(MAX_FRAMES):
            timestamp = duration*n/MAX_FRAMES
            raw=tool_output([ffmpeg,'-nostdin','-v','error','-threads','1','-protocol_whitelist','file',
                '-ss',format(timestamp,'.6f'),'-i',original,'-map','0:v:0','-an','-sn','-dn',
                '-frames:v','1','-vf','scale=1024:1024:force_original_aspect_ratio=decrease',
                '-f','image2pipe','-c:v','png','pipe:1'],MAX_FRAME_BYTES,12)
            dimensions(raw,'image/png')
            name=str(n)+'.png'
            with (temp/name).open('xb') as out:
                os.chmod(temp/name,0o600);out.write(raw);out.flush();os.fsync(out.fileno())
            frames.append({'timestamp_seconds':timestamp,'sha256':sha(raw),'size':len(raw),'path':str(final/name)})
        result={'schema':1,'original_sha256':item['sha256'],'manifest_sha256':digest(manifest),
                'index':index,'duration_seconds':duration,'width':streams[0]['width'],'height':streams[0]['height'],
                'tools':{n:config.get('media_tools',{}).get(n) for n in ('ffprobe','ffmpeg')},'sampling':'five representative timestamps; no audio/full-video claim',
                'frames':frames}
        from .cli_dispatch import save
        save(temp/'sampling.json',result)
        if final.exists():
            private(final,True)
            if json.loads(bounded_read(final/'sampling.json',24000,True)) != result:
                raise ValueError('Different retained sampling; preserve originals')
            verify_sampling(result,item,digest(manifest))
        else: os.rename(temp,final)
        return result
    finally:
        if temp.exists():shutil.rmtree(temp)


def verify_sampling(sample,item,manifest_hash):
    if (sample.get('schema') != 1 or sample.get('original_sha256') != item['sha256']
            or sample.get('manifest_sha256') != manifest_hash or len(sample.get('frames',[])) != MAX_FRAMES):
        raise ValueError('Sampling binding changed')
    parent=Path(item['guest_path']).parent/('frames-'+str(item['index']))
    from .cli_messages import private
    private(parent,True)
    for n,frame in enumerate(sample['frames']):
        path=parent/(str(n)+'.png')
        if frame['path'] != str(path):raise ValueError('Frame path changed')
        raw=bounded_read(path,MAX_FRAME_BYTES,True)
        if sha(raw)!=frame['sha256'] or len(raw)!=frame['size']:raise ValueError('Frame hash/size changed')
        dimensions(raw,'image/png')


def verify_message(run_dir,message):
    manifest=verify(run_dir,message['media_manifest'],message['media_manifest_sha256'])
    if (any(message.get(k) != v for k,v in manifest['binding'].items())
            or sha(message['text'].encode()) != message['text_sha256']):
        raise ValueError('Manifest differs from exact ordered text/message binding')
    return manifest


def prepare(message, run_dir, config):
    from .cli_messages import digest
    manifest=verify_message(run_dir,message)
    results=[]
    for item in manifest['attachments']:
        if item['media_type'] not in INSPECTABLE:
            results.append({'index':item['index'],'status':'unsupported','reason':'No supported local inspector for media type; original retained'})
        elif item['media_type'].startswith('image/'):
            results.append({'index':item['index'],'status':'ready_for_view','path':item['guest_path'],
                            'sha256':item['sha256'],'instruction':'Use local view_image; record an observed visual fact.'})
        else:
            try:
                sample=sample_video(run_dir,manifest,item['index'],config)
                results.append({'index':item['index'],'status':'ready_for_frame_views','sampling':sample})
            except (ValueError,OSError,KeyError,TypeError,subprocess.SubprocessError) as error:
                results.append({'index':item['index'],'status':'unsupported','reason':str(error)[:180]})
    return {'manifest_sha256':digest(manifest),'attachments':results,'inspected':False,
            'notice':'Preparation is not inspection. Actually view each image/frame before reporting observed facts.'}


def validate_inspection(run_dir,message,receipt):
    """Actual-session worker attestation, separate from transport acknowledgment.

    The worker must really use view_image. Hash checks establish content binding,
    not a machine oracle for whether a model understood an image.
    """
    from .cli_messages import digest
    manifest=verify_message(run_dir,message)
    if (receipt.get('media_preparation_sha256') != message.get('media_preparation_sha256')
            or not receipt.get('media_preparation_sha256')
            or receipt.get('media_manifest_sha256') != message['media_manifest_sha256']
            or receipt.get('content_sha256') != [i['sha256'] for i in manifest['attachments']]
            or not isinstance(receipt.get('inspection'),list)
            or len(receipt['inspection']) != len(manifest['attachments'])):
        raise ValueError('Full manifest/content readback required')
    for item, observed in zip(manifest['attachments'],receipt['inspection']):
        if not isinstance(observed,dict) or observed.get('index') != item['index']:
            raise ValueError('Ordered inspection binding required')
        if observed.get('state') in ('unsupported','not_inspected'):
            if set(observed) != {'index','state','reason'} or not isinstance(observed['reason'],str) or not 1<=len(observed['reason'])<=500:
                raise ValueError('Explicit bounded unsupported/uninspected reason required')
            continue
        if item['media_type'] not in INSPECTABLE:
            raise ValueError('Unsupported media cannot be reported inspected')
        if (set(observed) != {'index','state','tool','views'} or observed['state'] != 'inspected'
                or observed['tool'] != 'view_image' or not isinstance(observed['views'],list)):
            raise ValueError('Actual local image tool attestation required')
        expected=[{'path':item['guest_path'],'sha256':item['sha256']}]
        if item['media_type'].startswith('video/'):
            sample_path=Path(item['guest_path']).parent/('frames-'+str(item['index']))/'sampling.json'
            sample=json.loads(bounded_read(sample_path,24000,True));verify_sampling(sample,item,digest(manifest))
            delivered=message.get('media_preparation',{})
            if digest(delivered)!=message.get('media_preparation_sha256'):
                raise ValueError('Delivered media preparation changed')
            prepared=delivered.get('attachments',[])
            if len(prepared)<=item['index'] or prepared[item['index']].get('sampling')!=sample:
                raise ValueError('Sampling differs from actual delivered frame bindings')
            expected=[{k:f[k] for k in ('path','sha256','timestamp_seconds')} for f in sample['frames']]
        if len(observed['views']) != len(expected): raise ValueError('Every sampled frame/image needs actual view')
        for view, binding in zip(observed['views'],expected):
            if (not isinstance(view,dict) or set(view) != set(binding)|{'visual_fact'}
                    or any(view[k] != v for k,v in binding.items())
                    or not isinstance(view['visual_fact'],str) or not 1 <= len(view['visual_fact']) <= 500):
                raise ValueError('Exact viewed content/timestamp and observed visual fact required')


def main():
    """Fixed guest stdin endpoint on the existing local/SSH transport."""
    import sys
    # Supports executing this installed file with an argument-array SSH command.
    if __package__ is None:
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from coordinator.cli_dispatch import CONFIG, ROOT, rpc_account, check_account
    from coordinator.client import Client
    from coordinator.cli_messages import submit
    os.umask(0o077)
    raw=sys.stdin.buffer.read(MAX_PACKET+1)
    if len(raw)>MAX_PACKET: raise ValueError('Media packet limit exceeded')
    request=json.loads(raw)
    config=json.loads(CONFIG.read_text())
    account,models,limits=rpc_account(config['codex'])
    check_account(account,models,limits,config)
    result=submit(Client(),request,ROOT,CONFIG)
    print(json.dumps(result))  # hashes/IDs/status only; no media bytes/text.


if __name__ == '__main__': main()
