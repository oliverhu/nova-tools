import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import unittest
from unittest.mock import patch
import zlib

from coordinator import cli_media as media
from coordinator import cli_messages as messages
from coordinator.cli_dispatch import save
from tests.test_cli_messages import MessageTests


def png(w=2,h=2):
    def chunk(kind,raw):
        return struct.pack('>I',len(raw))+kind+raw+struct.pack('>I',zlib.crc32(kind+raw))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',w,h,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+b'\xff\0\0'*w)*h))+chunk(b'IEND',b'')


class MediaTests(MessageTests):
    def setUp(self):
        super().setUp()
        self.image=self.root/'explicit local.png'; self.image.write_bytes(png())
        self.media_request=media.pack(self.req,[{'path':str(self.image),'media_type':'image/png'}])

    def send_media(self,request=None):return self.send(request or self.media_request)

    def media_receipt(self,delivered,state='not_inspected'):
        receipt={k:delivered[k] for k in ('run_id','message_id','sequence','text_sha256','media_manifest_sha256','media_preparation_sha256','content_sha256')}
        receipt['inspection']=[{'index':0,'state':state,'reason':'Explicitly not yet viewed'}]
        return receipt

    def test_atomic_bound_delivery_retry_full_readback_and_next_legacy(self):
        first=self.send_media(); self.assertEqual(first['media_status'],'staged')
        self.assertEqual(first,self.send_media())
        self.send({**self.req,'message_id':'e'*32,'text':'legacy next'})
        delivered=self.receive(); self.assertEqual(delivered['media_status'],'delivered')
        manifest=delivered['media_manifest']; b=manifest['binding']
        for key in self.req:
            if key!='text':self.assertEqual(b[key],self.req[key])
        self.assertEqual(b['sequence'],1)
        self.assertEqual(Path(manifest['attachments'][0]['guest_path']).read_bytes(),self.image.read_bytes())
        ledger=(self.run_dir/'messages.json').read_text()
        self.assertNotIn('data_base64',ledger)
        self.assertNotIn(str(self.image),ledger)
        with self.assertRaises(ValueError):self.ack(delivered)
        receipt=self.media_receipt(delivered)
        for key in ['text_sha256','media_manifest_sha256','content_sha256']:
            changed=copy.deepcopy(receipt);changed[key]=['0'*64] if key=='content_sha256' else '0'*64
            with self.assertRaises(ValueError):messages.acknowledge(self.client,changed,self.root,self.cfg)
        result=messages.acknowledge(self.client,receipt,self.root,self.cfg)
        self.assertEqual(result['media_status'],'acknowledged');self.assertEqual(result['inspection'][0]['state'],'not_inspected')
        self.assertEqual(messages.acknowledge(self.client,receipt,self.root,self.cfg),result)
        self.assertEqual(self.receive()['sequence'],2)

    def test_actual_view_attestation_requires_exact_path_hash_and_fact(self):
        self.send_media();delivered=self.receive();receipt=self.media_receipt(delivered)
        item=delivered['media_manifest']['attachments'][0]
        receipt['inspection']=[{'index':0,'state':'inspected','tool':'view_image',
            'views':[{'path':item['guest_path'],'sha256':item['sha256'],'visual_fact':'Fixture contains red pixels.'}]}]
        for key,value in [('path','/tmp/other.png'),('sha256','0'*64),('visual_fact','')]:
            bad=copy.deepcopy(receipt);bad['inspection'][0]['views'][0][key]=value
            with self.assertRaises(ValueError):messages.acknowledge(self.client,bad,self.root,self.cfg)
        result=messages.acknowledge(self.client,receipt,self.root,self.cfg)
        self.assertEqual(result['inspection'][0]['state'],'inspected')

    def test_tampered_original_manifest_and_stale_binding_retained(self):
        self.send_media();delivered=self.receive();item=delivered['media_manifest']['attachments'][0]
        path=Path(item['guest_path']);original=path.read_bytes();path.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):self.receive()
        with self.assertRaises(ValueError):self.send_media()
        path.write_bytes(original)
        manifest=path.parent/'manifest.json';data=json.loads(manifest.read_text());data['binding']['text_sha256']='0'*64
        save(manifest,data)
        with self.assertRaises(ValueError):self.receive()
        self.assertTrue(path.exists())

    def test_corrupt_partial_unsafe_oversize_and_symlinks_do_not_publish(self):
        for change in [{'sha256':'0'*64},{'size':100},{'data_base64':'broken!'},
                       {'name':'../secret.png'},{'size':media.MAX_FILE+1}, {'media_type':'text/html'}]:
            bad=copy.deepcopy(self.media_request);bad['media'][0].update(change)
            with self.assertRaises(ValueError):self.send_media(bad)
        final=self.run_dir/'media'/self.req['message_id'];self.assertFalse(final.exists())
        self.assertFalse(list((self.run_dir/'media').glob('.stage-*')))
        link=self.root/'link.png';link.symlink_to(self.image)
        with self.assertRaises(ValueError):media.pack(self.req,[{'path':str(link),'media_type':'image/png'}])
        bad=copy.deepcopy(self.media_request);bad['media']*=5
        with self.assertRaises(ValueError):self.send_media(bad)
        with patch.object(media.shutil,'disk_usage',return_value=type('Disk',(),{'free':1})()):
            with self.assertRaises(ValueError):self.send_media()
        self.assertFalse(final.exists())

    def test_crash_after_atomic_stage_recovers_exactly_and_preserves_foreign_directory(self):
        with patch.object(messages,'save',side_effect=OSError('ledger interruption')):
            with self.assertRaises(OSError):self.send_media()
        final=self.run_dir/'media'/self.req['message_id'];self.assertTrue(final.is_dir())
        self.assertEqual(self.send_media()['sequence'],1)
        self.assertFalse(list(final.parent.glob('.stage-*')))
        unread=messages.closeout(self.run_dir);self.assertEqual(unread,[self.req['message_id']]);self.assertTrue(final.exists())

    def test_unsupported_original_is_retained_and_never_inspected(self):
        webp=self.root/'animation.webp';webp.write_bytes(b'RIFFunsupported')
        self.send_media(media.pack(self.req,[{'path':str(webp),'media_type':'image/webp'}]))
        delivered=self.receive();self.assertEqual(delivered['media_preparation']['attachments'][0]['status'],'unsupported')
        item=delivered['media_manifest']['attachments'][0]
        self.assertEqual(Path(item['guest_path']).read_bytes(),webp.read_bytes())
        receipt=self.media_receipt(delivered,'unsupported')
        result=messages.acknowledge(self.client,receipt,self.root,self.cfg)
        self.assertEqual(result['inspection'][0]['state'],'unsupported')

    def video_delivery(self):
        video=self.root/'temporal video.mov';video.write_bytes(b'\0\0\0\x14ftypqt  '+b'\0'*40)
        self.send_media(media.pack(self.req,[{'path':str(video),'media_type':'video/quicktime'}]))
        return self.receive()

    def test_missing_video_tools_explicit_not_inspected_with_original_preserved(self):
        delivered=self.video_delivery();prep=delivered['media_preparation']
        self.assertFalse(prep['inspected']);self.assertEqual(prep['attachments'][0]['status'],'unsupported')
        self.assertIn('ffprobe unavailable',prep['attachments'][0]['reason'])
        self.assertTrue(Path(delivered['media_manifest']['attachments'][0]['guest_path']).is_file())

    def test_bounded_timestamped_frames_bind_original_and_each_actual_view(self):
        delivered=self.video_delivery();calls=[]
        def output(argv,limit=32768,timeout=15):
            calls.append((argv,limit,timeout))
            if argv[0]=='probe':return json.dumps({'format':{'duration':'10'},'streams':[{'codec_type':'video','width':640,'height':480}]}).encode()
            return png()
        with patch.object(media,'pinned_tool',side_effect=lambda c,n:'probe' if n=='ffprobe' else 'ffmpeg'),patch.object(media,'tool_output',side_effect=output):
            delivered=self.receive()
            prepared=delivered['media_preparation']
        sample=prepared['attachments'][0]['sampling'];self.assertEqual([f['timestamp_seconds'] for f in sample['frames']],[0,2,4,6,8])
        self.assertEqual(len(calls),6)
        for argv,limit,timeout in calls[1:]:
            self.assertIsInstance(argv,list);self.assertIn('-nostdin',argv);self.assertIn('-an',argv)
            self.assertEqual(limit,media.MAX_FRAME_BYTES);self.assertEqual(timeout,12)
        receipt=self.media_receipt(delivered)
        receipt['inspection']=[{'index':0,'state':'inspected','tool':'view_image','views':[
            {k:frame[k] for k in ('path','sha256','timestamp_seconds')}|{'visual_fact':'Observed red frame.'} for frame in sample['frames']]}]
        missing=copy.deepcopy(receipt);missing['inspection'][0]['views'].pop()
        with self.assertRaises(ValueError):messages.acknowledge(self.client,missing,self.root,self.cfg)
        sampling_path=Path(sample['frames'][0]['path']).parent/'sampling.json'
        original_sampling=sampling_path.read_bytes();changed=copy.deepcopy(sample);changed['frames'][0]['timestamp_seconds']=9
        save(sampling_path,changed)
        with self.assertRaises(ValueError):messages.acknowledge(self.client,receipt,self.root,self.cfg)
        sampling_path.write_bytes(original_sampling)
        self.assertEqual(messages.acknowledge(self.client,receipt,self.root,self.cfg)['inspection'][0]['state'],'inspected')
        Path(sample['frames'][0]['path']).write_bytes(b'corrupt')
        with self.assertRaises(ValueError):messages.acknowledge(self.client,receipt,self.root,self.cfg)

    def test_invalid_video_metadata_and_partial_extraction_keep_original_and_clean_temp(self):
        self.video_delivery()
        for duration in ['nan','0','121','-1']:
            def output(argv,limit=32768,timeout=15):
                return json.dumps({'format':{'duration':duration},'streams':[{'codec_type':'video','width':640,'height':480}]}).encode()
            with patch.object(media,'pinned_tool',return_value='tool'),patch.object(media,'tool_output',side_effect=output):
                delivered=self.receive()
                self.assertEqual(delivered['media_preparation']['attachments'][0]['status'],'unsupported')
        count=0
        def partial(argv,limit=32768,timeout=15):
            nonlocal count
            if argv[0]=='probe':return b'{"format":{"duration":"10"},"streams":[{"codec_type":"video","width":640,"height":480}]}'
            count+=1
            if count==2:raise ValueError('Decoder failed on later frame')
            return png()
        with patch.object(media,'pinned_tool',side_effect=lambda c,n:'probe' if n=='ffprobe' else 'ffmpeg'),patch.object(media,'tool_output',side_effect=partial):
            delivered=self.receive()
            self.assertEqual(delivered['media_preparation']['attachments'][0]['status'],'unsupported')
        original=Path(delivered['media_manifest']['attachments'][0]['guest_path'])
        self.assertTrue(original.exists());self.assertFalse(list(original.parent.glob('.frames-*')))
        self.assertFalse((original.parent/'frames-0').exists())

    def test_malicious_display_name_and_oversize_run_reject_before_publication(self):
        bad=copy.deepcopy(self.media_request);bad['media'][0]['name']='$(touch owned).png'
        with self.assertRaises(ValueError):self.send_media(bad)
        with patch.object(media,'MAX_RUN',1):
            with self.assertRaises(ValueError):self.send_media()
        self.assertFalse((self.run_dir/'media'/self.req['message_id']).exists())

    def test_tool_time_and_output_bounds_reap_owned_child(self):
        with self.assertRaises(ValueError):media.tool_output([sys.executable,'-c','import time;time.sleep(10)'],timeout=0.05)
        with self.assertRaises(ValueError):media.tool_output([sys.executable,'-c','print("x"*10000)'],limit=100)
        self.assertEqual(media.tool_output([sys.executable,'-c','print("ok")']),b'ok\n')

    def test_wrong_worker_generation_thread_and_account_refuse_before_media_publish(self):
        for key,value in [('worker','mini1'),('generation',2),('thread_id','f'*36),('account_email_sha256','f'*64)]:
            with self.assertRaises(ValueError):self.send_media({**self.media_request,key:value})
        self.assertFalse((self.run_dir/'media').exists())

    def test_image_dimensions_reject_decompression_bomb(self):
        raw=bytearray(png());raw[16:24]=struct.pack('>II',100000,100000)
        with self.assertRaises(ValueError):media.dimensions(bytes(raw),'image/png')


class SenderTests(unittest.TestCase):
    def test_sender_uses_only_explicit_local_bytes_and_constant_remote_command(self):
        spec=importlib.util.spec_from_file_location('sender',Path(__file__).resolve().parents[1]/'scripts/send-cli-media.py')
        sender=importlib.util.module_from_spec(spec);spec.loader.exec_module(sender)
        with patch.object(sender,'pack',return_value={'bounded':'packet'}) as pack, patch.object(sender,'tool_output',return_value=b'{"status":"queued"}') as output:
            self.assertEqual(sender.send({'message_id':'a'*32,'local_media':[{'path':'/explicit.png','media_type':'image/png'}]},'nova-mini-vm2'),{'status':'queued'})
            argv=output.call_args.args[0]
            self.assertIn('StrictHostKeyChecking=yes',argv)
            self.assertNotIn('/explicit.png',' '.join(argv))
            self.assertEqual(argv[-1],'/usr/bin/python3 .local/share/nova-coordinator/coordinator/cli_media.py')
            self.assertEqual(output.call_args.kwargs['timeout'],90)
            self.assertEqual(output.call_args.kwargs['limit'],24000)
        for host in ['-oProxyCommand=x','user@host','host;touch x','https://host']:
            with self.assertRaises(ValueError):sender.send({},host)

    def test_transport_input_and_output_are_bounded_without_pipe_deadlock(self):
        raw=b'x'*1000000
        result=media.tool_output([sys.executable,'-c','import sys; print(len(sys.stdin.buffer.read()))'],input_data=raw)
        self.assertEqual(result,b'1000000\n')
        with self.assertRaises(ValueError):
            media.tool_output([sys.executable,'-c','import sys; print("x"*10000);sys.stdout.flush();sys.stdin.buffer.read()'],limit=100,input_data=raw)


if __name__=='__main__':unittest.main()
