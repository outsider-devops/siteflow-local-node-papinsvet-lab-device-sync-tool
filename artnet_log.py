"""Rotating full-frame capture; disk I/O is outside the UDP receive thread."""
import json
from pathlib import Path
import queue
import re
import shutil
import threading


class ArtNetLog:
    def __init__(self, directory, max_bytes=10*1024*1024, segments=5, min_free=1024*1024*1024):
        self.directory=Path(directory)
        self.max_bytes=max_bytes
        self.segments=segments
        self.min_free=min_free
        self.lock=threading.RLock()
        self.queue=queue.Queue(maxsize=2048)
        self.stop=threading.Event()
        self.written=0
        self.dropped=0
        self.error=None
        self.worker=None

    def start(self):
        try:
            self.directory.mkdir(parents=True,exist_ok=True)
            self.worker=threading.Thread(target=self.loop,daemon=True)
            self.worker.start()
        except OSError as exc:self.error=str(exc)

    def submit(self,record):
        if not self.worker:
            return
        try:self.queue.put_nowait(record)
        except queue.Full:self.dropped+=1

    def path(self,index=0):
        return self.directory/('artnet.jsonl' if index==0 else 'artnet.%d.jsonl'%index)

    def write_record(self,record):
        line=(json.dumps(record,ensure_ascii=False,separators=(',',':'))+'\n').encode()
        with self.lock:
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
                    dropped=self.dropped,queued=self.queue.qsize(),files=files,
                    maxBytes=self.max_bytes*self.segments,fullFrameValues=True)

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
