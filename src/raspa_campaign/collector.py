"""P06 task-local live capture, with immutable batches and replayable cursors.

Only the runner holding that task's claim writes here. Final scientific parsing
still reads native output from byte zero. No shared campaign-wide JSON writer.
"""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
from typing import Any
from .atomic import atomic_write_json, atomic_write_text
from .hashing import canonical_sha256, sha256_bytes
from .models import utc_now
from .parser import CYCLE_RE, _loading_record, PHASE_RE
from .paths import within, no_symlinks, read_json_file

class IncrementalCollector:
    def __init__(self,campaign_root:Path,*,interval_seconds=20.0):
        self.root=campaign_root;self.interval=max(0.01,float(interval_seconds))
        self.stop_event=asyncio.Event();self.active={};self.errors={}
    def register(self,task_id,attempt_dir,output_globs):
        no_symlinks(attempt_dir)
        if (attempt_dir/'SEALED.json').exists():raise ValueError('Cannot collect into sealed evidence')
        if task_id in self.active:raise ValueError('Collector task already registered')
        self.active[task_id]={'attempt':attempt_dir,'globs':list(output_globs)}
    def unregister(self,task_id):self.active.pop(task_id,None)
    def request_stop(self):self.stop_event.set()
    def task_errors(self,task_id):return self.errors.get(task_id,[])
    def _files(self,record):
        from .reparse import _safe_globs
        patterns=_safe_globs(record['globs'])
        root=record['attempt']/'work';paths=set()
        for g in patterns:
            for p in root.glob(g):
                within(root,p.relative_to(root).as_posix())
                if p.is_file():paths.add(p)
        return sorted(paths)
    def _scan_file(self,tid,attempt,path,*,final=False):
        live=within(attempt,'live');live.mkdir(exist_ok=True)
        cursor_path=within(live,'collector_state.json')
        cursor=read_json_file(cursor_path) if cursor_path.exists() else {}
        rel=path.relative_to(attempt).as_posix();prev=cursor.get(rel,{})
        st=path.stat();offset=prev.get('offset',0);generation=prev.get('generation',0)
        reset=prev and ((st.st_dev,st.st_ino)!=(prev.get('device'),prev.get('inode')) or st.st_size<offset)
        with path.open('rb') as f:
            if prev and not reset and offset:
                f.seek(max(0,offset-128));tail=f.read(min(128,offset))
                reset=sha256_bytes(tail)!=prev.get('boundary_sha256')
            if reset:offset=0;generation+=1;prev={}
            f.seek(offset);data=f.read(1024*1024)
            if not data:return 0
            end=data.rfind(b'\n')+1
            if not end:
                if final and offset+len(data)==st.st_size:end=len(data)
                elif len(data)>=1024*1024:raise ValueError('Live line exceeds 1 MiB; raw is preserved')
                else:return 0
            complete=data[:end];new_offset=offset+end
            f.seek(max(0,new_offset-128));boundary=sha256_bytes(f.read(min(128,new_offset)))
        cycle=prev.get('last_cycle');phase=prev.get('phase','unknown');line_no=prev.get('line_count',0)
        records=[]
        for line in complete.decode('utf-8',errors='replace').splitlines():
            line_no+=1
            pm=PHASE_RE.search(line)
            if pm:phase=pm.group(1).lower()
            cm=CYCLE_RE.search(line)
            if cm:cycle=int(cm.group(1))
            import re
            final_marker=re.search(r'Final state after\s+(\d+)\s+cycles',line,re.I)
            if final_marker:cycle=int(final_marker.group(1));phase='final'
            value=_loading_record(line,cycle=cycle,phase=phase,line_no=line_no,source=rel)
            if value:records.append({**value,'task_id':tid,'attempt':attempt.name,'generation':generation})
        batch={'schema':'rcm-live-batch-p06-v1','task_id':tid,'attempt':attempt.name,'source':rel,
               'generation':generation,'offset_start':offset,'offset_end':new_offset,
               'bytes_sha256':sha256_bytes(complete),'records':records,
               'decode_replacements':complete.decode('utf-8',errors='replace').count('\ufffd')}
        batch_id=canonical_sha256(batch);folder=within(live,'batches');folder.mkdir(exist_ok=True)
        dest=within(folder,'batch_'+batch_id+'.json')
        if dest.exists():
            if read_json_file(dest)!=batch:raise ValueError('Live batch collision/tamper')
        else:atomic_write_json(dest,batch)
        cursor[rel]={'device':st.st_dev,'inode':st.st_ino,'generation':generation,'offset':new_offset,
                     'last_cycle':cycle,'phase':phase,'line_count':line_no,'boundary_sha256':boundary,
                     'last_batch_id':batch_id}
        atomic_write_json(cursor_path,cursor)
        return len(records)
    def scan_once(self,*,task_id=None,final=False):
        count=0
        for tid,record in list(self.active.items()):
            if task_id is not None and tid!=task_id:continue
            if self.errors.get(tid):continue
            try:
                if (record['attempt']/'SEALED.json').exists():raise ValueError('Attempt already sealed')
                for p in self._files(record):
                    # One bounded chunk per live scan. At finalization, consume all remaining bytes.
                    while True:
                        before=None
                        cp=record['attempt']/'live/collector_state.json'
                        if cp.exists():before=read_json_file(cp)
                        count+=self._scan_file(tid,record['attempt'],p,final=final)
                        if not final or (read_json_file(cp) if cp.exists() else None)==before:break
            except (OSError,ValueError,TypeError,KeyError) as exc:
                self.errors.setdefault(tid,[]).append({'type':type(exc).__name__,'error':str(exc),'utc':utc_now()})
        return count
    def finalize(self,task_id):
        record=self.active[task_id];self.scan_once(task_id=task_id,final=True)
        if self.errors.get(task_id):raise ValueError('Task-local live collector failed: '+json.dumps(self.errors[task_id]))
        live=record['attempt']/'live';folder=live/'batches'
        batches=[read_json_file(p) for p in sorted(folder.glob('batch_*.json'))] if folder.exists() else []
        batches.sort(key=lambda b:(b['source'],b['generation'],b['offset_start']))
        # This convenience view can be reconstructed from immutable batch records.
        atomic_write_text(live/'cycle_events.jsonl',''.join(json.dumps(r,ensure_ascii=False,sort_keys=True)+'\n'
                           for b in batches for r in b['records']))
        atomic_write_json(live/'collector_summary.json',{'schema':'rcm-live-summary-p06-v1',
            'batch_count':len(batches),'record_count':sum(len(b['records']) for b in batches),
            'writer_scope':'single claimed task/attempt','final_parser_authoritative':True})
    async def run(self):
        while not self.stop_event.is_set():
            self.scan_once()
            try:await asyncio.wait_for(self.stop_event.wait(),timeout=self.interval)
            except asyncio.TimeoutError:pass
        self.scan_once()
