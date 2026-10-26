"""Rotating full-frame capture; disk I/O is outside the UDP receive thread."""
import json
from pathlib import Path
import queue
import re
import shutil
import threading
import time


class ArtNetLog:
    def __init__(self, directory, max_bytes=10*1024*1024, segments=5, min_free=1024*1024*1024,
                 max_age_seconds=24*60*60, repeat_seconds=5):
        self.directory=Path(directory)
        self.max_bytes=max_bytes
        self.segments=segments
        self.min_free=min_free
        self.max_age_seconds=max_age_seconds
        self.repeat_seconds=repeat_seconds
        self.lock=threading.RLock()
        self.queue=queue.Queue(maxsize=2048)
        self.stop=threading.Event()
        self.written=0
        self.dropped=0
        self.coalesced=0
        self.error=None
        self.worker=None
        self.last_records={}
        self.last_prune=0

    def start(self):
        try:
            self.directory.mkdir(parents=True,exist_ok=True)
            self.prune_expired(force=True)
            self.worker=threading.Thread(target=self.loop,daemon=True)
            self.worker.start()
        except OSError as exc:self.error=str(exc)

    def submit(self,record):
        if not self.worker:
            return
        # Preserve every channel-value change, but do not write identical
        # 25–40 Hz keepalive frames to disk. One unchanged health sample per
        # source is retained every few seconds.
        temporal={'receivedAt','sentAt','sequence','lastSeen'}
        key=(record.get('type'),record.get('sourceIp'),record.get('destinationIp'),
             record.get('universe'),record.get('physical'),record.get('opcode'),record.get('source'))
        values=record.get('values')
        if isinstance(values,list) and all(type(v) is int and 0<=v<=255 for v in values):
            # A DMX payload is already a byte vector. Avoid JSON-serializing up
            # to 512 channel values on the real-time receive/forwarding path.
            metadata=tuple((k,json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':')))
                           for k,v in sorted(record.items()) if k not in temporal and k!='values')
            signature=(metadata,bytes(values))
        else:
            signature=json.dumps({k:v for k,v in record.items() if k not in temporal},
                                 ensure_ascii=False,sort_keys=True,separators=(',',':'))
        now=time.monotonic()
        with self.lock:
            previous=self.last_records.get(key)
            if previous and previous[0]==signature and now-previous[1]<self.repeat_seconds:
                self.coalesced+=1
                return
            self.last_records[key]=(signature,now)
        try:self.queue.put_nowait(record)
        except queue.Full:self.dropped+=1

    def prune_expired(self,force=False):
        now=time.time()
        if not force and now-self.last_prune<60:return
        self.last_prune=now
        cutoff=now-self.max_age_seconds
        with self.lock:
            for i in range(self.segments):
                path=self.path(i)
                if path.exists() and path.stat().st_mtime<cutoff:path.unlink()

    def path(self,index=0):
        return self.directory/('artnet.jsonl' if index==0 else 'artnet.%d.jsonl'%index)

    def write_record(self,record):
        line=(json.dumps(record,ensure_ascii=False,separators=(',',':'))+'\n').encode()
        with self.lock:
            self.prune_expired()
            if shutil.disk_usage(self.directory).free<self.min_free:
                raise OSError('Logging paused: less than 1 GiB of disk space available')
            path=self.path()
            if path.exists() and path.stat().st_size+len(line)>self.max_bytes:
                for i in range(self.segments-1,0,-1):
                    previous=self.path(i-1)
                    if previous.exists():previous.replace(self.path(i))
            with path.open('ab') as fp:fp.write(line)
            self.written+=1
            self.error=None

    def loop(self):
        while not self.stop.is_set() or not self.queue.empty():
            try:record=self.queue.get(timeout=.2)
            except queue.Empty:continue
            try:self.write_record(record)
            except (OSError,ValueError) as exc:
                self.error=str(exc);self.dropped+=1
            finally:self.queue.task_done()

    def close(self):
        self.stop.set()
        if self.worker:self.worker.join(timeout=3)

    def status(self):
        with self.lock:
            files=[dict(name=self.path(i).name,bytes=self.path(i).stat().st_size)
                   for i in range(self.segments) if self.path(i).exists()]
        return dict(enabled=self.worker is not None,error=self.error,writtenThisSession=self.written,
                    dropped=self.dropped,coalesced=self.coalesced,queued=self.queue.qsize(),files=files,
                    maxBytes=self.max_bytes*self.segments,maxAgeHours=round(self.max_age_seconds/3600),
                    repeatSeconds=self.repeat_seconds,fullFrameValues=True)

    def recent(self,limit=50):
        limit=max(1,min(limit,200))
        result=[]
        with self.lock:
            for i in range(self.segments):
                path=self.path(i)
                if not path.exists():continue
                with path.open('rb') as fp:
                    size=path.stat().st_size;offset=max(0,size-2*1024*1024)
                    fp.seek(offset)
                    if offset:fp.readline()
                    lines=fp.read().splitlines()
                for raw in reversed(lines):
                    try:result.append(json.loads(raw))
                    except (ValueError,UnicodeDecodeError):continue
                    if len(result)>=limit:return result
        return result

    def open_segment(self,name):
        if name not in [self.path(i).name for i in range(self.segments)]:
            raise ValueError('Invalid capture segment')
        with self.lock:
            fp=(self.directory/name).open('rb')
            size=(self.directory/name).stat().st_size
        return fp,size
