"""Read-only CoreMIDI event capture without third-party Python packages."""
from collections import deque
import ctypes
import json
import platform
import threading
import time

EVENTS=deque(maxlen=500)
LOCK=threading.Lock()
EVENT_CONDITION=threading.Condition(LOCK)
EVENT_SEQUENCE=0
START_LOCK=threading.Lock()
STARTING=False
NEXT_RETRY=0
STATUS={'available':False,'message':'MIDI capture has not started','ports':[]}

class _MIDIPacket(ctypes.Structure):
    # MIDIServices.h uses #pragma pack(push, 4), including on 64-bit macOS.
    _pack_=4
    _fields_=[('timeStamp',ctypes.c_uint64),('length',ctypes.c_uint16),('data',ctypes.c_uint8*256)]

class _MIDIPacketList(ctypes.Structure):
    _pack_=4
    _fields_=[('numPackets',ctypes.c_uint32),('packet',_MIDIPacket)]

_READ_PROC=ctypes.CFUNCTYPE(None,ctypes.POINTER(_MIDIPacketList),ctypes.c_void_p,ctypes.c_void_p)
_CORE=None
_CF=None
_CLIENT=ctypes.c_uint32()
_PORT=ctypes.c_uint32()
_CALLBACK=None
_SOURCE_REFS=[]

def _cfstring(value):
    raw=value.encode('utf-8')
    return _CF.CFStringCreateWithCString(None,raw,0x08000100)

def _cftext(ref):
    if not ref:return ''
    buf=ctypes.create_string_buffer(512)
    if not _CF.CFStringGetCString(ref,buf,len(buf),0x08000100):return ''
    return buf.value.decode('utf-8','replace')

def _packet_messages(values):
    """Split complete MIDI 1.0 messages; CoreMIDI packets forbid running status."""
    result=[];index=0
    while index<len(values):
        status=values[index]
        if status<0x80:
            index+=1
            continue
        if status==0xF0:
            end=index+1
            while end<len(values) and values[end]!=0xF7:end+=1
            result.append(values[index:min(end+1,len(values))]);index=end+1
            continue
        if status<0xF0:size=2 if status&0xF0 in (0xC0,0xD0) else 3
        else:size={0xF1:2,0xF2:3,0xF3:2}.get(status,1)
        message=values[index:index+size]
        if len(message)==size and all(byte<0x80 for byte in message[1:]):
            result.append(message);index+=size
        else:index+=1
    return result


def _packet_events(packet_list,source):
    """Follow MIDIPacketNext rules: ARM aligns packet addresses, Intel does not."""
    base=ctypes.addressof(packet_list.contents)
    address=base+_MIDIPacketList.packet.offset
    align=platform.machine().lower() in ('arm64','aarch64','arm')
    for _ in range(min(packet_list.contents.numPackets,4096)):
        length=ctypes.c_uint16.from_address(address+_MIDIPacket.length.offset).value
        values=list(ctypes.string_at(address+_MIDIPacket.data.offset,length))
        received=time.time()
        for message in _packet_messages(values):
            yield {'source':source,'bytes':message,'time':received,'monotonic':time.monotonic()}
        address+=_MIDIPacket.data.offset+length
        if align:address=(address+3)&~3

def _reader():
    global STARTING,STATUS,_CORE,_CF,_CALLBACK,NEXT_RETRY
    if platform.system()!='Darwin':
        STATUS={'available':False,'message':'Live MIDI capture is available on macOS only.','ports':[]}
        NEXT_RETRY=float('inf')
        with START_LOCK:STARTING=False
        return
    try:
        _CORE=ctypes.CDLL('/System/Library/Frameworks/CoreMIDI.framework/CoreMIDI')
        _CF=ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        _CF.CFStringCreateWithCString.restype=ctypes.c_void_p
        _CF.CFStringCreateWithCString.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_uint32]
        _CF.CFStringGetCString.restype=ctypes.c_bool
        _CF.CFStringGetCString.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_long,ctypes.c_uint32]
        _CF.CFRelease.argtypes=[ctypes.c_void_p]
        _CORE.MIDIClientCreate.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint32)]
        _CORE.MIDIInputPortCreate.argtypes=[ctypes.c_uint32,ctypes.c_void_p,_READ_PROC,ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint32)]
        _CORE.MIDIGetNumberOfSources.restype=ctypes.c_uint32
        _CORE.MIDIGetSource.argtypes=[ctypes.c_uint32];_CORE.MIDIGetSource.restype=ctypes.c_uint32
        _CORE.MIDIPortConnectSource.argtypes=[ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
        _CORE.MIDIObjectGetStringProperty.argtypes=[ctypes.c_uint32,ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p)]
        cname=_cfstring('SiteFlow MIDI Monitor')
        if _CORE.MIDIClientCreate(cname,None,None,ctypes.byref(_CLIENT))!=0:raise RuntimeError('CoreMIDI client creation failed')
        _CF.CFRelease(cname)
        def receive(packet_list,_,source_ref):
            global EVENT_SEQUENCE
            try:
                source=ctypes.string_at(source_ref).decode('utf-8','replace') if source_ref else 'MIDI input'
                decoded=list(_packet_events(packet_list,source))
                with EVENT_CONDITION:
                    for event in decoded:
                        EVENT_SEQUENCE+=1;event['sequence']=EVENT_SEQUENCE;EVENTS.append(event)
                    if decoded:EVENT_CONDITION.notify_all()
            except Exception:pass
        _CALLBACK=_READ_PROC(receive)
        pname=_cfstring('SiteFlow MIDI Input')
        if _CORE.MIDIInputPortCreate(_CLIENT,pname,_CALLBACK,None,ctypes.byref(_PORT))!=0:raise RuntimeError('CoreMIDI input port creation failed')
        _CF.CFRelease(pname)
        name_prop=_cfstring('name');ports=[]
        for index in range(int(_CORE.MIDIGetNumberOfSources())):
            source=_CORE.MIDIGetSource(index);name_ref=ctypes.c_void_p()
            name='MIDI input'
            if _CORE.MIDIObjectGetStringProperty(source,name_prop,ctypes.byref(name_ref))==0 and name_ref:
                name=_cftext(name_ref);_CF.CFRelease(name_ref)
            ports.append(name)
            ref=ctypes.create_string_buffer(name.encode('utf-8'))
            _SOURCE_REFS.append(ref)
            _CORE.MIDIPortConnectSource(_PORT,source,ctypes.cast(ref,ctypes.c_void_p))
        _CF.CFRelease(name_prop)
        STATUS={'available':True,'message':'Listening to local CoreMIDI inputs.','ports':ports}
        with START_LOCK:STARTING=False
        while True:time.sleep(1)
    except Exception as exc:
        STATUS={'available':False,'message':'CoreMIDI capture unavailable: '+str(exc),'ports':[]}
        NEXT_RETRY=time.monotonic()+5
        with START_LOCK:STARTING=False

def snapshot(after=0):
    global STARTING
    with START_LOCK:
        if not STARTING and not STATUS.get('available') and time.monotonic()>=NEXT_RETRY:
            STARTING=True
            threading.Thread(target=_reader,daemon=True).start()
    with LOCK:events=[dict(e,receivedAt=e['time']) for e in EVENTS if e['time']>after]
    return {'status':dict(STATUS),'events':events,'serverTime':time.time()}


def wait_events(after_sequence,timeout=.5):
    """A server worker consumes MIDI directly; browser polling is not a transport."""
    snapshot(time.time())
    with EVENT_CONDITION:
        EVENT_CONDITION.wait_for(lambda:EVENT_SEQUENCE>after_sequence,timeout)
        return EVENT_SEQUENCE,[dict(e) for e in EVENTS if e.get('sequence',0)>after_sequence]
