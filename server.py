#!/usr/bin/env python3
"""SiteFlow local inventory, Art-Net input routing, and explicitly operated fixture controls."""
from fixtures import initial_patch, validate_fixture, PRESETS, prepare_fixture_update, prepare_fixture_import, ensure_beam230_patch, ensure_fx_profile
from input_controls import initial_controls, validate_control, FADER_FUNCTIONS
from led_controls import defaults as led_defaults,default_canvases as led_default_canvases,validate_canvases as led_validate_canvases,virtual_fixture,validate_mapping,controller_frames,merge_pixel_frames_htp,complete_controller_frames,locator_frames,output_address_test_frames,output_map_from_frames,canvas_sweep_addresses,canvas_sweep_frame,LEDRuntime
import argparse
import concurrent.futures
import copy
import datetime as dt
import http.client
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
import xml.etree.ElementTree as ET
from artnet_monitor import ArtNetMonitor, DESK_HOLD_SECONDS
from artnet_log import ArtNetLog
from controller_modes import ControllerModes
from local_devices import discover_local_devices
from midi_monitor import snapshot as midi_snapshot, wait_events as wait_midi_events
from midi_controls import remove_control as remove_midi_control, restore_control as restore_midi_control
from traktor_template import load_template as load_traktor_template, merge_template as merge_traktor_template
from midi_inputs import MIDIInputs, validate_section as validate_midi_section
from sequence_profiles import make_virtual_fixtures as sequence_virtual_fixtures, validate_sequence, validate_sample

ROOT = Path(__file__).resolve().parent
LOCK = threading.RLock()
TOKEN = secrets.token_urlsafe(32)
PORT = 8765
STATE_FILE = ROOT / 'data' / 'state.json'
STATE = {}
PORTS = (80, 443, 8080, 8081, 6668, 554, 37777, 22)
MONITOR = ArtNetMonitor()
LED = LEDRuntime()
CONTROLLER = None
MIDI_INPUTS = MIDIInputs()
MIDI_CONFIG_KEY = None
HTTP_SERVER = None
RESTART_REQUESTED = False


def refresh_midi_inputs():
    global MIDI_CONFIG_KEY
    with LOCK:
        banks=STATE.get('midiInputSections',{})
        devices=[d for d in STATE.get('devices',[]) if d.get('kind')=='midi' or d.get('deviceClass')=='midi']
        config_key=(tuple((k,id(v)) for k,v in banks.items()),tuple((d['id'],d.get('midiRevision',0)) for d in devices))
        if config_key==MIDI_CONFIG_KEY:return
        copied_banks=copy.deepcopy(banks);copied_devices=copy.deepcopy(devices)
        MIDI_CONFIG_KEY=config_key
    MIDI_INPUTS.configure(copied_banks,copied_devices)


def midi_patch(device_id):
    io=controller_io()
    fixtures=effective_fixtures(STATE.get('fixtureMaps',{}).get(device_id,initial_patch(device_id)),io)
    fixtures+=led_virtuals(io['outputUniverse'] if io else 1)+wiz_virtual_fixtures(STATE.get('devices',[]),io['outputUniverse'] if io else 1)
    fixtures+=sequence_virtual_fixtures(STATE.get('sequences',{}).get(device_id,[]),io['outputUniverse'] if io else 1)
    return fixtures


def midi_source_cards(controller_id):
    status=midi_snapshot().get('status',{});ports=status.get('ports',[])
    snapshot=MIDI_INPUTS.snapshot();sections=STATE.get('midiInputSections',{}).get(controller_id,[])
    return [dict(id=d['id'],name=d.get('name','MIDI device'),kind='midi',
                 online=d.get('midiTemplate',{}).get('source') in ports,
                 mappedControls=len(d.get('midiControls',[])),sections=sum(s['sourceId']==d['id'] for s in sections),
                 activity=snapshot['activity'].get(d['id'],[]))
            for d in STATE.get('devices',[]) if d.get('kind')=='midi' or d.get('deviceClass')=='midi']


def import_led_registry():
    STATE.setdefault('ledFixtures',led_defaults())
    STATE.setdefault('ledCanvases',led_default_canvases())
    reserved_universes={}
    canvas_ports={(c.get('controllerIp'),c.get('port')) for c in STATE['ledCanvases'] if isinstance(c,dict)}
    for canvas in STATE['ledCanvases']:
        canvas.pop('controlPreset',None)
        canvas.setdefault('colorOrder',next((f.get('order','BRG') for f in STATE['ledFixtures'] if f.get('id')=='led:matrix'),'BRG'))
        # One-time legacy migration for the original matrix canvas. Once a shape
        # is saved, preserve its user-defined geometry across restarts.
        if canvas.get('id')=='canvas:matrix-main' and not canvas.get('shape'):
            canvas.update(shape='rectangle',width=5,height=40,pixelCount=200,order='serpentine',startCorner='bottom-left',scanAxis='vertical',firstUniverse=16,universeCount=2)
        canvas.setdefault('shape','rectangle')
        canvas['pixelCount']=(sum(abs(int(line['endPixel'])-int(line['startPixel']))+1 for line in canvas.get('lines',[]))
                              if canvas.get('shape')=='irregular' and isinstance(canvas.get('lines'),list)
                              else int(canvas.get('width',1))*int(canvas.get('height',1)))
        needed=(int(canvas.get('startPixel',1))+canvas['pixelCount']-1)*3
        needed=(needed+511)//512
        ip=canvas.get('controllerIp','');port=int(canvas.get('port',1));occupied=reserved_universes.setdefault(ip,[])
        first=canvas.get('firstUniverse')
        same_port=[row for row in occupied if row[0]==port]
        if same_port:first=same_port[0][1]
        def other_port_overlap(candidate):
            if any(row[0]!=port and candidate<=row[2] and candidate+needed-1>=row[1] for row in occupied):return True
            for other_port in range(1,5):
                if other_port==port or (ip,other_port) in canvas_ports:continue
                first=16+(other_port-1)*2
                if candidate<=first+1 and candidate+needed-1>=first:return True
            return False
        if type(first) is not int or first<0 or first+needed-1>32767 or canvas.get('universeCount')!=needed or other_port_overlap(first):
            first=16+(max(1,port)-1)*2 if not same_port else same_port[0][1]
            while other_port_overlap(first):first+=needed
            canvas['firstUniverse']=first
        canvas['universeCount']=needed
        occupied.append((port,first,first+needed-1))
    for f in STATE['ledFixtures']:
        if f['id']=='led:matrix':
            f.setdefault('renderMode','pixel-fill');f.setdefault('reverse',False)
            layout=f.setdefault('matrixLayout',dict(pixelsPerRow=40,rows=5,serpentine=True,axis='vertical'))
            if layout.get('pixelsPerRow')==100 and layout.get('rows')==2:
                f['matrixLayout']=dict(pixelsPerRow=40,rows=5,serpentine=True,axis='vertical')
    for ip,name in [('192.168.18.3','LED Strips · P1–P4'),('192.168.18.157','Vertical LED Matrix')]:
        config_mac=next((f.get('controllerId') for f in STATE['ledFixtures']
                         if f.get('controllerIp')==ip and re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',str(f.get('controllerId','')),re.I)),None)
        d=next((d for d in STATE['devices'] if config_mac and d.get('mac') and mac_normalize(d['mac'])==mac_normalize(config_mac)),None)
        if not d:d=next((d for d in STATE['devices'] if d['ip']==ip),None)
        if not d:
            # Keep a persistent record only when the saved configuration has a MAC.
            # IP-only imports are not device identities and will reappear on discovery.
            if not config_mac:continue
            d=identify(ip,config_mac);d['name']=name;STATE['devices'].append(d)
        if config_mac and not d.get('mac'):
            old_id=d.get('id');d['id']=config_mac;d['mac']=config_mac
            STATE['knownOrder']=[config_mac if item==old_id else item for item in STATE.get('knownOrder',[])]
        if d['name'] in ('LED ArtNet','Device 3'):d['name']=name
        d['configurationOnly']=True
        d.setdefault('inventoryOrigin','imported-config')
        # One-time cleanup of the old IP-based guess. An address in an imported
        # patch is not evidence of either presence or hardware model.
        if not d.get('profileImportReviewed'):
            if d.get('controllerProfileSource') != 'user':
                for key in ('controllerProfileId','controllerProfileSource','controllerProfileBase',
                            'deviceProfile','deviceProfileSource','model'):
                    d.pop(key,None)
                d['controllerProfileSource']='imported-unverified'
            d['profileImportReviewed']=True
        if d.get('controllerProfileSource') not in ('user',):
            d['role']='Saved LED configuration · hardware type unverified'
            d['confidence']='Imported configuration'
            d['kind']='artnet_led'  # internal association for saved LED routes only
        d['present']=False  # startup is not a live network observation
        if not d.get('trackedAt'):
            d['tracked'] = False
        d['nextStep']='Saved LED patch imported. This does not confirm that a controller is currently on the network or identify its model.'
    for d in STATE['devices']:
        if d.get('kind') == 'host' and not d.get('trackedAt'):
            d['tracked'] = False
    # Bind LED profiles and canvas geometry to the stable network identity,
    # not merely to an address that may later be reassigned.
    for fixture in STATE['ledFixtures']:
        device=next((d for d in STATE['devices'] if d.get('ip')==fixture.get('controllerIp') and d.get('mac')),None)
        if not fixture.get('controllerId') and device:fixture['controllerId']=mac_normalize(device['mac'])
        if not fixture.get('controllerId'):fixture['controllerId']='orphan:'+str(fixture.get('controllerIp','unknown'))
        if fixture.get('controllerId'):
            device=next((d for d in STATE['devices'] if d.get('mac') and mac_normalize(d['mac'])==fixture['controllerId']),None)
            if device:fixture['controllerIp']=device['ip']
    for canvas in STATE['ledCanvases']:
        device=next((d for d in STATE['devices'] if d.get('ip')==canvas.get('controllerIp') and d.get('kind')=='artnet_led' and d.get('mac')),None)
        if not canvas.get('controllerId') and device:canvas['controllerId']=mac_normalize(device['mac'])
        if not canvas.get('controllerId'):canvas['controllerId']='orphan:'+str(canvas.get('controllerIp','unknown'))
        if canvas.get('controllerId'):
            device=next((d for d in STATE['devices'] if d.get('mac') and mac_normalize(d['mac'])==canvas['controllerId']),None)
            if device:canvas['controllerIp']=device['ip']
    # Migrate the old global matrix layout to explicit targets. The canvas owns
    # physical wiring; the emulator only keeps a per-canvas display orientation.
    for fixture in STATE['ledFixtures']:
        if fixture.get('id')=='led:matrix' and not isinstance(fixture.get('canvasTargets'),list):
            fixture['canvasTargets']=[dict(id=c['id'],rowOrientation=c.get('scanAxis','vertical'))
                                      for c in STATE['ledCanvases'] if c.get('controllerId')==fixture.get('controllerId')]
        if fixture.get('id')=='led:matrix':fixture.pop('matrixLayout',None)
    # Reconcile saved Canvas starts to the controller's current per-port
    # Art-Net map. A stale U25 on a P1 canvas otherwise physically lights P2.
    for canvas in STATE['ledCanvases']:
        owner=next((d for d in STATE['devices'] if d.get('mac') and mac_normalize(d['mac'])==canvas.get('controllerId')),None)
        ranges=owner.get('artnetOutputRanges') if owner else None
        port=int(canvas.get('port',0))
        if not isinstance(ranges,list) or len(ranges)!=4 or not 1<=port<=4 or not ranges[port-1]:continue
        first=ranges[port-1][0];needed=(int(canvas['startPixel'])+int(canvas['pixelCount'])-1)*3
        needed=(needed+511)//512
        if first+needed-1<=ranges[port-1][-1]:
            canvas['firstUniverse']=first;canvas['universeCount']=needed
    # Replace the old controller-wide virtual fixture with its concrete Canvas
    # targets. Keep saved desk and MIDI assignments, modes and relative rules.
    matrix=next((f for f in STATE['ledFixtures'] if f['id']=='led:matrix'),None)
    if matrix:
        selected={row.get('id') for row in matrix.get('canvasTargets',[]) if isinstance(row,dict)}
        canvas_ids=[c['id'] for c in STATE['ledCanvases'] if c.get('controllerId')==matrix.get('controllerId')
                    and c.get('enabled',True) and c['id'] in selected]
        for bank in list(STATE.get('inputControls',{}).values())+list(STATE.get('midiInputSections',{}).values()):
            for control in bank:
                targets=control.get('targetFixtures',[])
                if 'led:matrix' not in targets:continue
                control['targetFixtures']=list(dict.fromkeys(fid for target in targets
                    for fid in (canvas_ids if target=='led:matrix' else [target])))
                modes=control.setdefault('targetModes',{});mode=modes.pop('led:matrix',None)
                if mode:
                    for fid in canvas_ids:modes.setdefault(fid,mode)
                if isinstance(control.get('rules'),list):
                    control['rules']=[dict(rule,fixtureId=fid) for rule in control['rules']
                        for fid in (canvas_ids if rule.get('fixtureId')=='led:matrix' else [rule.get('fixtureId')])]
    save()

def sync_led_controller_address(device):
    """Keep the stored endpoint current for profiles bound to this MAC."""
    identity=mac_normalize(device.get('mac','')) if device.get('mac') else ''
    if not identity:return
    for fixture in STATE.get('ledFixtures',[]):
        if fixture.get('controllerId')==identity:fixture['controllerIp']=device['ip']
    for canvas in STATE.get('ledCanvases',[]):
        if canvas.get('controllerId')==identity:canvas['controllerIp']=device['ip']

def controller_profile_catalog():
    profiles=[]
    for path in sorted((Path(__file__).resolve().parent/'profiles').glob('*.json')):
        try:
            data=json.loads(path.read_text(encoding='utf-8'))
            if data.get('profileType')=='pixel-controller':
                profiles.append({key:data[key] for key in ('profileId','displayName','manufacturer','model','profileType') if key in data})
        except (OSError,ValueError,TypeError):
            continue
    return profiles

def apply_detected_profiles(device):
    """Attach safe card/profile defaults only when the scan has positive identity evidence."""
    if device.get('controllerProfileSource') == 'user-cleared':
        # Respect an explicit unassignment across scans. The generic pixel-controller
        # card type can remain, but stale discovery data must not restore a model.
        device.pop('controllerProfileId', None)
        device.pop('model', None)
        device.pop('deviceProfile', None)
        device.pop('deviceProfileSource', None)
        return device
    if device.get('configurationOnly') and not str(device.get('model') or '').strip():
        return device
    if device.get('deviceProfileSource') == 'user' or device.get('controllerProfileSource') in ('user','site-confirmed'):
        return device
    assigned_id = device.get('controllerProfileId')
    if assigned_id:
        assigned = next((p for p in controller_profile_catalog() if p.get('profileId') == assigned_id), None)
        if assigned:
            device.update(kind='artnet_led', model=assigned.get('model'), role='Art-Net pixel controller',
                          deviceProfile='artnet_pixels', deviceProfileSource='detected')
            return device
    detected = {
        'artnet':'artnet_dmx', 'artnet_led':'artnet_pixels', 'wiz':'wiz_light',
        'midi':'midi_controller'
    }.get(device.get('kind'))
    if device.get('deviceClass') == 'midi':
        detected = 'midi_controller'
    if detected:
        device['deviceProfile'] = detected
        device['deviceProfileSource'] = 'detected'
    model = str(device.get('model') or '').strip().casefold()
    manufacturer = str(device.get('manufacturer') or '').strip().casefold()
    if model:
        for profile in controller_profile_catalog():
            aliases = [str(x).casefold() for x in profile.get('modelAliases', [])]
            expected_model = str(profile.get('model') or '').casefold()
            expected_manufacturer = str(profile.get('manufacturer') or '').casefold()
            if model in ([expected_model] + aliases) and (not expected_manufacturer or manufacturer == expected_manufacturer or device.get('kind') == 'artnet_led'):
                device.update(controllerProfileId=profile['profileId'], controllerProfileSource='detected',
                              kind='artnet_led', model=profile.get('model'), role='Art-Net pixel controller')
                break
    return device

def apply_sp801e_artpoll_profile(device,node):
    """Trust a fresh ArtPoll identity and MAC suffix, not a saved IP or vendor guess."""
    if not node or node.get('type')!='ArtPollReply' or node.get('sourceIp')!=device.get('ip'):
        return False
    match=re.fullmatch(r'SP801E_([0-9A-Fa-f]{6})',str(node.get('shortName') or ''))
    mac=mac_normalize(str(device.get('mac') or ''))
    if not match or not mac.replace(':','').endswith(match.group(1).lower()):return False
    outputs=[p.get('outputUniverse') for p in node.get('ports',[]) if p.get('output')]
    if not outputs or len(outputs)%4 or len(outputs)>32 or any(type(u) is not int or not 0<=u<=32767 for u in outputs):return False
    if outputs!=list(range(outputs[0],outputs[0]+len(outputs))):return False
    per_port=len(outputs)//4
    reported_ranges=[outputs[i*per_port:(i+1)*per_port] for i in range(4)]
    manual_ranges=device.get('artnetOutputManualRanges')
    output_ranges=manual_ranges if valid_sp801e_port_ranges(manual_ranges) else reported_ranges
    changes=dict(kind='artnet_led',model='SP801E',role='Art-Net pixel controller',
                 controllerProfileId='suntech.sp801e',controllerProfileSource='artpoll',
                 deviceProfile='artnet_pixels',deviceProfileSource='detected',
                 artnetOutputUniverses=[row[0] for row in output_ranges],artnetOutputRanges=output_ranges,
                 artnetReportedUniverses=outputs,artnetReportedOutputRanges=reported_ranges,
                 artnetPortMappingSource='Manual per-port map' if output_ranges is manual_ranges else 'ArtPoll range grouped across 4 physical outputs; optical check pending',
                 configurationOnly=False)
    changed=any(device.get(k)!=v for k,v in changes.items())
    device.update(changes,present=True,reachability='Online',lastSeen=now(),neighborObservedAt=now(),
                  confidence='ArtPollReply + MAC identity',access='Art-Net response')
    return changed

def valid_sp801e_port_ranges(ranges):
    if not isinstance(ranges,list) or len(ranges)!=4:return False
    previous=-1
    for row in ranges:
        if not isinstance(row,list) or not 1<=len(row)<=8:return False
        if any(type(value) is not int or not 0<=value<=32767 for value in row):return False
        if row!=list(range(row[0],row[0]+len(row))) or row[0]<=previous:return False
        previous=row[-1]
    return True

def led_virtuals(universe=1):
    fixtures=STATE.get('ledFixtures',[])
    targets=[virtual_fixture(f,universe) for f in fixtures if f['id']!='led:matrix']
    matrix=next((f for f in fixtures if f['id']=='led:matrix'),None)
    if matrix:
        controller=next((d for d in STATE.get('devices',[]) if d.get('mac') and mac_normalize(d['mac'])==matrix.get('controllerId')),None)
        for canvas in STATE.get('ledCanvases',[]):
            if canvas.get('controllerId')!=matrix.get('controllerId') or not canvas.get('enabled',True):continue
            target=virtual_fixture(matrix,universe,canvas)
            if controller:target['name']=f"{canvas['name']} · {controller['name']} · P{canvas['port']}"
            targets.append(target)
    return targets

def controller_io():
    """Return the verified physical input/output patch without changing hardware."""
    if CONTROLLER is None:return None
    status=CONTROLLER.snapshot()
    inputs=[p for p in status.get('ports',[]) if p.get('direction')=='Input']
    outputs=[p for p in status.get('ports',[]) if p.get('direction')=='Output']
    if status.get('connection')!='ONLINE' or not status.get('fresh') or len(inputs)!=1 or len(outputs)!=1:return None
    return dict(inputPort=inputs[0]['port'],inputUniverse=inputs[0]['universe'],
                outputPort=outputs[0]['port'],outputUniverse=outputs[0]['universe'])

def effective_controls(items,io):
    result=json.loads(json.dumps(items))
    if io:
        for control in result:control['universe']=io['inputUniverse']
    return result

def effective_fixtures(items,io):
    result=json.loads(json.dumps(items))
    if io:
        for fixture in result:fixture['universe']=io['outputUniverse']
    return result

def wiz_virtual_fixtures(devices,universe):
    """Expose paired WiZ lights as virtual PAR LED targets, never DMX addresses."""
    # PAR LED semantic controls are approximated in software for RGB WiZ lamps.
    roles=['Master','Red','Green','Blue','White','Amber','UV','Strobe']
    result=[]
    for device in devices:
        if device.get('kind')!='wiz' or not device.get('id') or not device.get('ip'):continue
        result.append(dict(id='wiz-'+str(device['id']).replace(':','').lower(),deviceId=device['id'],
                           name=device.get('name') or 'WiZ light',universe=universe,start=1,
                           mappingPreset='wiz-parled',virtualWiz=True,
                           channels=[dict(function=role,default=0) for role in roles]))
    return result

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def run(args, timeout=5):
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
        return result.stdout, result.returncode
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ''
        return out.decode(errors='replace') if isinstance(out, bytes) else out, 124
    except OSError:
        return '', 127


HOST_IP = None
NETWORK_INTERFACE = 'LAN'

def network():
    global NETWORK_INTERFACE
    from network_platform import select_network
    subnet, ip, NETWORK_INTERFACE = select_network(HOST_IP)
    return subnet, ip


def valid_target(ip, subnet):
    try:
        addr, net = ipaddress.IPv4Address(ip), ipaddress.IPv4Network(subnet)
        return addr in net and addr not in (net.network_address, net.broadcast_address)
    except ValueError:
        return False


def mac_normalize(value):
    return ':'.join(x.zfill(2) for x in value.lower().split(':'))


def neighbors(subnet):
    from network_platform import neighbor_entries
    return neighbor_entries(subnet, network()[1])


def vendors():
    path = Path('/opt/homebrew/share/nmap/nmap-mac-prefixes')
    if not path.exists():
        return {}
    result = {}
    for line in path.read_text().splitlines():
        parts = line.split(' ', 1)
        if len(parts) == 2 and re.fullmatch('[0-9A-F]{6}', parts[0]):
            result[parts[0]] = parts[1]
    return result


VENDORS = vendors()


def identify(ip, mac):
    vendor = VENDORS.get(mac.replace(':', '')[:6].upper(), 'Unknown vendor')
    private_mac = bool(int(mac.split(':')[0], 16) & 2) if mac else False
    kind = 'unknown'
    for needle, name in [('wiz', 'wiz'), ('tuya', 'tuya'), ('beken', 'beken'),
                         ('dahua', 'dahua'), ('huawei', 'router'), ('apple', 'host')]:
        if needle in vendor.lower():
            kind = name
            break
    result = dict(id=mac or 'ip-' + ip, ip=ip, mac=mac, manufacturer=vendor,
                name='Device ' + ip.rsplit('.', 1)[1], kind=kind, interface='network', deviceClass='network', present=False,
                model='', firmware='', vendorId='', role='Unidentified device',
                zone='', notes='', tracked=False, confidence='Vendor hint',
                privateMac=private_mac, reachability='Unknown', access='Unverified',
                ports=[], webUrl=None, lastSeen=None, checkedAt=None,
                evidence=['MAC vendor identifies a module, not a finished product.'],
                nextStep='Match the MAC address to a physical device or its vendor app.',
                addedAt=now())
    return apply_detected_profiles(result)


def save():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix('.tmp')
    temp.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(str(temp), str(STATE_FILE))


def finish_beam_lamp_action(fixture_id, started_at, action):
    with LOCK:
        item=STATE.setdefault('beamLampStates',{}).get(fixture_id,{})
        if item.get('startedAt')!=started_at:return
        item['phase']={'safe-off':'off-sent','lamp-on':'ignition-wait','lamp-reset':'reset-sent'}[action]
        item['actionUntil']=None
        save()


def event(message, level='info'):
    STATE['events'].insert(0, dict(time=now(), message=message, level=level))
    del STATE['events'][100:]


def load():
    global STATE
    if STATE_FILE.exists():
        STATE = json.loads(STATE_FILE.read_text(encoding='utf-8'))
        STATE.setdefault('knownOrder', [])
        STATE['scan']['running'] = False
        STATE['scan']['message'] = 'Ready for a fresh check'
        for d in STATE['devices']:
            d['present'] = bool(d.get('connected')) if d.get('interface') in ('usb', 'bluetooth') else False
            if d.get('reachability') == 'Online':
                d['reachability'] = 'Stale'
        return
    seeded = json.loads((ROOT / 'seed.json').read_text(encoding='utf-8'))
    STATE = dict(devices=seeded, knownOrder=[], events=[], scan=dict(running=False, completed=0,
                 total=0, mode=None, message='No live scan in this session', lastCompleted=None),
                 network=dict(subnet='192.168.18.0/24', interface=NETWORK_INTERFACE, hostIp='192.168.18.6'))
    event('Imported observed devices. Availability requires a fresh response.')
    save()


def connect(ip, port):
    try:
        with socket.create_connection((ip, port), timeout=0.55):
            return True
    except OSError:
        return False


def http_page(ip, port):
    """No redirects, proxies, credentials, scripts, or links from device HTML."""
    conn = http.client.HTTPConnection(ip, port, timeout=2)
    try:
        conn.request('GET', '/', headers={'User-Agent': 'SiteFlow-Local/0.1', 'Connection': 'close'})
        r = conn.getresponse()
        body = r.read(40000).decode('utf-8', errors='replace')
        title = re.search(r'<title[^>]*>(.*?)</title>', body, re.I | re.S)
        return {'status': r.status, 'title': re.sub('<[^>]+>', '', title[1]).strip()[:100] if title else '', 'body': body}
    finally:
        conn.close()


def wiz_read(ip):
    results = {}
    # Ask for both pieces of state in one round trip window; sequential UDP
    # timeouts made a healthy lamp look unresponsive for several seconds.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(.7)
        expected={'getSystemConfig','getPilot'}
        for request_id,method in enumerate(sorted(expected),1):
            sock.sendto(json.dumps({'id':request_id,'method':method,'params':{}}).encode(),(ip,38899))
        deadline=time.monotonic()+.7
        while expected:
            sock.settimeout(max(.01,deadline-time.monotonic()))
            try:
                data,sender=sock.recvfrom(8192)
                reply=json.loads(data);method=reply.get('method')
                if sender[0]==ip and method in expected and isinstance(reply.get('result'),dict):
                    results[method]={k:v for k,v in reply['result'].items()
                                     if k in ('mac','moduleName','fwVersion','state','dimming','temp','r','g','b','sceneId')}
                    expected.remove(method)
            except (OSError,ValueError,TypeError):break
    return results


def wiz_command(ip, params, readback=True, wait_ack=True):
    """Send one local WiZ setPilot command and optionally require fresh readback."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(.7)
        request = {'id': 1, 'method': 'setPilot', 'params': params}
        sock.sendto(json.dumps(request, separators=(',', ':')).encode(), (ip, 38899))
        if wait_ack:
            try:
                data, sender = sock.recvfrom(8192)
                reply = json.loads(data)
                if sender[0] != ip or reply.get('method') != 'setPilot' or reply.get('error') or (isinstance(reply.get('result'), dict) and reply['result'].get('success') is False):
                    raise ValueError('WiZ did not confirm the command')
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError('WiZ command was not confirmed; check local control settings and connectivity') from exc
    if not readback:return None
    state = wiz_read(ip).get('getPilot')
    if not state:
        raise ValueError('Command sent, but WiZ state could not be read back')
    return state


def probe(original, subnet, host_ip):
    d = dict(original)
    d['evidence'] = []
    d['checkedAt'] = now()
    if not valid_target(d['ip'], subnet):
        d.update(reachability='Stale', access='Outside network', ports=[], webUrl=None,
                 nextStep='This saved address is outside the connected Wi-Fi subnet.')
        return d
    if d['ip'] == host_ip:
        d.update(reachability='Online', access='Local host', lastSeen=now(), ports=[], webUrl=None,
                 nextStep='This Mac runs the local node. Device output controls are disabled.')
        d['evidence'] = ['Address confirmed on the selected LAN interface.']
        return d
    opened = [p for p in PORTS if connect(d['ip'], p)]
    d.update(ports=opened, webUrl=None, access='Unverified', reachability='Unreachable', state=None)
    if opened:
        d.update(reachability='Online', lastSeen=now(), access='Service detected')
        d['evidence'].append('Fresh TCP response: ' + ', '.join(map(str, opened)) + '.')
    if d.get('kind') == 'wiz':
        data = wiz_read(d['ip'])
        if data:
            config = data.get('getSystemConfig', {})
            reply_mac = config.get('mac', '').replace(':', '').lower()
            expected = d.get('mac', '').replace(':', '').lower()
            if reply_mac and expected and reply_mac != expected:
                d.update(access='Identity conflict', reachability='Online', nextStep='WiZ returned a different MAC. Reconcile identity before adding.')
                d['evidence'].append('WiZ identity differs from the saved MAC.')
                return d
            d.update(reachability='Online', access='Local API', lastSeen=now(),
                     model=config.get('moduleName', d.get('model', '')),
                     firmware=config.get('fwVersion', d.get('firmware', '')),
                     state=data.get('getPilot'), confidence='Protocol response',
                     nextStep='Local read access confirmed. Match this light to its room before enabling control in a later stage.')
            d['evidence'].append('WiZ read-only API responded. No setPilot commands sent.')
        else:
            d['nextStep'] = 'Check local communication and verified-control settings in WiZ. No security settings have been changed.'
    if 80 in opened or 8080 in opened:
        for port in (80, 8080):
            if port not in opened:
                continue
            try:
                page = http_page(d['ip'], port)
                d.update(webUrl='http://' + d['ip'] + (':' + str(port) if port != 80 else '') + '/',
                         webStatus=page['status'], webTitle=page['title'], access='Web interface')
                d['evidence'].append('HTTP %s at /%s' % (page['status'], ' — ' + page['title'] if page['title'] else ''))
                body = page['body']
                if 'ArtNet-DMX' in body:
                    cells = dict(re.findall(r'<th>([^<]+)</th>\s*<td>([^<]+)</td>', body, re.I))
                    d.update(kind='artnet', manufacturer='ArtNet-DMX', model=cells.get('Model', ''),
                             firmware=cells.get('Firmware Ver.', ''), role='Art-Net / DMX node', confidence='Device response',
                             access='Status page', nextStep='Status page reachable. DMX reception is shown separately in the signal monitor.')
                    d['reportedName'] = cells.get('Name', '')
                elif "ProductName = '" in body:
                    match = re.search(r"ProductName\s*=\s*'([^']+)'", body)
                    if match:
                        d.update(model=match[1], kind='router', manufacturer='Huawei Technologies', confidence='Device response')
                    d['nextStep'] = 'Local login page is available. Router configuration is outside this node’s current scope.'
                elif d.get('kind') == 'dahua':
                    d['nextStep'] = 'Local login page is available. A permitted device account and exact model are required for integration.'
                else:
                    d['nextStep'] = 'Open the local interface to identify the device. A web response does not confirm control access.'
                break
            except (OSError, http.client.HTTPException, ValueError):
                d['evidence'].append('HTTP did not return a readable root page.')
    if 6668 in opened and d.get('access') == 'Service detected':
        if d.get('kind') == 'unknown':
            d.update(kind='tuya', confidence='Port signature only')
        d['nextStep'] = 'Port 6668 suggests Tuya LAN. Identify the product and obtain permitted credentials before integration.'
    if 8081 in opened and d.get('kind') == 'ewelink':
        d['nextStep'] = 'LAN service responds. Encrypted eWeLink access needs a device key and verified channel mapping.'
    if not opened and d['reachability'] != 'Online':
        d['evidence'].append('No response to the selected TCP / supported API checks. This does not prove power is off.')
    return d


def merge_result(result):
    # Preserve edits made while a network check is in flight.
    with LOCK:
        current = next((d for d in STATE['devices'] if d['id'] == result['id']), None)
        if current:
            for field in ('name', 'zone', 'notes', 'tracked', 'trackedAt', 'deviceProfile', 'role', 'controllerProfileId', 'controllerProfileSource', 'controllerProfileBase', 'configurationOnly', 'inventoryOrigin', 'profileImportReviewed'):
                result[field] = current.get(field, result.get(field))
            current.update(result)
            sync_led_controller_address(current)
            apply_detected_profiles(current)
            if current.get('controllerProfileId'):
                profile = next((p for p in controller_profile_catalog() if p.get('profileId') == current['controllerProfileId']), None)
                if profile:
                    current.update(kind='artnet_led', model=profile.get('model'), role='Art-Net pixel controller')
            if result.get('kind')=='artnet' and result.get('transportMac'):
                STATE['devices'][:]=[d for d in STATE['devices'] if d is current or not (d['ip']==current['ip'] and d.get('mac')==result['transportMac'] and d.get('kind')=='artnet')]


def dedupe_unknown_placeholders():
    """Deduplicate only records sharing a MAC; an IP alone is not identity."""
    by_ip = {}
    for device in STATE['devices']:
        if device.get('ip'):
            by_ip.setdefault(device['ip'], []).append(device)
    removed = set()
    for rows in by_ip.values():
        identified = next((d for d in rows if d.get('mac')), None)
        if not identified:
            continue
        same_identity = [d for d in rows if d.get('mac') == identified.get('mac')]
        canonical = next((d for d in same_identity if d.get('id') == identified.get('mac')), identified)
        for duplicate in same_identity:
            if duplicate is canonical:
                continue
            for field in ('name', 'zone', 'notes', 'tracked', 'trackedAt', 'deviceProfile', 'role', 'controllerProfileId', 'controllerProfileSource', 'controllerProfileBase'):
                value = duplicate.get(field)
                if (field == 'tracked' and value) or (field != 'tracked' and value):
                    if field in ('tracked', 'trackedAt', 'deviceProfile', 'role') or not canonical.get(field):
                        canonical[field] = value
            removed.add(duplicate['id'])
    if removed:
        STATE['devices'][:] = [d for d in STATE['devices'] if d.get('id') not in removed]
        event('Merged duplicate unidentified network records by IP and MAC')


def update_local_inventory():
    """Refresh USB/Bluetooth inventory without probing or commanding devices."""
    devices, supported = discover_local_devices()
    if devices is None:
        return None, 'USB/Bluetooth inventory could not be read.'
    if not supported:
        return 0, 'USB/Bluetooth inventory is unavailable on this platform.'
    present = {d['id'] for d in devices}
    with LOCK:
        for result in devices:
            result['present'] = bool(result.get('connected'))
            current = next((d for d in STATE['devices'] if d['id'] == result['id']), None)
            if current:
                for field in ('name', 'zone', 'notes', 'tracked', 'trackedAt', 'deviceProfile', 'role', 'controllerProfileId', 'controllerProfileSource', 'controllerProfileBase'):
                    result[field] = current.get(field, result.get(field))
                current.update(result)
            else:
                STATE['devices'].append(result)
                event('Discovered local ' + result['interface'].upper() + ' device · ' + result['name'])
        for old in STATE['devices']:
            if old.get('interface') in ('usb', 'bluetooth') and old['id'] not in present:
                old.update(reachability='Stale', access='Disconnected', connected=False,
                           present=False, checkedAt=now(), evidence=['No longer listed by the operating system.'])
        STATE['scan']['localDevices'] = len(devices)
    return len(devices), None


def scan(mode='scan', device_id=None):
    try:
        subnet, host_ip = network()
        MONITOR.configure(subnet, host_ip)
        local_count, local_warning = update_local_inventory()
        with LOCK:
            STATE['scan']['peripheralWarning'] = local_warning
            STATE['network'] = dict(subnet=subnet, interface=NETWORK_INTERFACE, hostIp=host_ip)
            STATE['scan']['message'] = 'Discovering devices on ' + subnet if mode == 'scan' else 'Checking saved devices'
        live = set()
        found_ips = set()
        if mode == 'scan':
            nmap = shutil.which('nmap') or '/opt/homebrew/bin/nmap'
            xml, code = run([nmap, '-sn', '-n', '--max-retries', '1', '-oX', '-', subnet], timeout=40)
            if code == 0:
                try:
                    for host in ET.fromstring(xml).findall('host'):
                        if host.find('status').get('state') == 'up':
                            live.update(a.get('addr') for a in host.findall('address') if a.get('addrtype') == 'ipv4')
                except (ET.ParseError, AttributeError):
                    code = 1
            with LOCK:
                STATE['scan']['discoveryWarning'] = None if code == 0 else 'Discovery was incomplete. Neighbor observations and device checks are still included.'
            found = neighbors(subnet)
            found_ips = set(found)
            with LOCK:
                for ip, mac in found.items():
                    existing = next((d for d in STATE['devices'] if d['mac'] == mac), None)
                    if existing:
                        existing['ip'] = ip
                        existing['mac'] = mac
                        existing['present'] = True
                        existing['neighborObservedAt'] = now()
                        sync_led_controller_address(existing)
                    else:
                        d = identify(ip, mac)
                        d['present'] = True
                        d['inventoryOrigin']='network-discovery'
                        d['neighborObservedAt'] = now()
                        STATE['devices'].append(d)
                        event('Discovered ' + ip + ' · ' + d['manufacturer'])
                dedupe_unknown_placeholders()
                for ip in live:
                    if valid_target(ip, subnet) and not any(d['ip'] == ip for d in STATE['devices']):
                        d = identify(ip, '')
                        d['present'] = True
                        d['inventoryOrigin']='network-observation'
                        STATE['devices'].append(d)
        with LOCK:
            # A saved IP now owned by a different MAC must never be probed as the old identity.
            current_neighbors = neighbors(subnet)
            present_ips = set(live) | found_ips | set(current_neighbors)
            for d in STATE['devices']:
                if d.get('interface') not in ('usb', 'bluetooth'):
                    d['present'] = d.get('kind') == 'host' or (bool(d.get('mac')) and d['ip'] in present_ips) or (not d.get('mac') and d.get('inventoryOrigin')=='network-observation' and d['ip'] in live)
            targets = [dict(d) for d in STATE['devices'] if d.get('interface') not in ('usb', 'bluetooth') and (bool(d.get('mac')) or d.get('kind') == 'host' or d.get('inventoryOrigin')=='network-observation') and (device_id is None or d['id'] == device_id)]
            conflicts = set()
            for d in targets:
                actual = current_neighbors.get(d['ip'])
                if actual and d['mac'] and actual != d['mac']:
                    if d.get('kind')=='artnet':
                        try:
                            body=http_page(d['ip'],80)['body']
                            cells=dict(re.findall(r'<th>([^<]+)</th>\s*<td>([^<]+)</td>',body,re.I))
                            if 'ArtNet-DMX' in body and cells.get('NodeA MAC Address','').lower()==d['mac'].lower():
                                d['transportMac']=actual
                                continue
                        except Exception:
                            pass
                    d.update(reachability='Stale', access='Identity conflict', webUrl=None, ports=[], present=False,
                             checkedAt=now(), evidence=['Saved IP now has a different MAC.'],
                             nextStep='Reconcile the saved identity with the current network neighbor.')
                    merge_result(d)
                    conflicts.add(d['id'])
            targets = [d for d in targets if d['id'] not in conflicts]
            STATE['scan'].update(total=len(targets), completed=0, message='Checking local access')
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            tasks = {pool.submit(probe, d, subnet, host_ip): d for d in targets}
            for task in concurrent.futures.as_completed(tasks):
                try:
                    result = task.result()
                    result['present'] = result.get('reachability') == 'Online' or result['ip'] in present_ips
                    if result['ip'] in live and result['reachability'] == 'Unreachable':
                        result.update(reachability='Online', lastSeen=now())
                        result['evidence'].append('Fresh discovery probe responded; application access remains unverified.')
                    merge_result(result)
                except Exception as exc:
                    with LOCK:
                        event('Check failed for ' + tasks[task]['ip'] + ': ' + type(exc).__name__, 'warning')
                with LOCK:
                    STATE['scan']['completed'] += 1
        with LOCK:
            STATE['scan'].update(lastCompleted=now(), message='Local access check completed')
            event(('Network scan' if mode == 'scan' else 'Status check') + ' completed · USB/Bluetooth: ' + str(local_count if local_count is not None else 'unavailable') + '. No output commands were sent.')
    except Exception as exc:
        with LOCK:
            STATE['scan']['message'] = str(exc)
            event(str(exc), 'error')
    finally:
        with LOCK:
            STATE['scan']['running'] = False
            save()


def start_scan(mode, device_id=None):
    with LOCK:
        if STATE['scan']['running']:
            return False
        STATE['scan'].update(running=True, mode=mode, completed=0, total=0,
                             message='Starting local check', discoveryWarning=None)
        threading.Thread(target=scan, args=(mode, device_id), daemon=True).start()
        return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def allowed_host(self):
        try:host=urllib.parse.urlsplit('//'+self.headers.get('Host','')).hostname
        except ValueError:return False
        return host in ('127.0.0.1','localhost',MONITOR.host_ip)

    def allowed_origin(self):
        try:
            origin=urllib.parse.urlsplit(self.headers.get('Origin',''))
            return origin.scheme=='http' and origin.netloc==self.headers.get('Host') and self.allowed_host()
        except ValueError:return False

    def reply(self, data, status=200, content_type='application/json; charset=utf-8', extra=None):
        body = json.dumps(data, ensure_ascii=False).encode() if isinstance(data, (dict, list)) else data
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        embedded=urllib.parse.urlsplit(self.path).path=='/midi' and urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('embed')==['1']
        self.send_header('X-Frame-Options', 'SAMEORIGIN' if embedded else 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors "+("'self'" if embedded else "'none'")+"; base-uri 'none'; form-action 'self'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.allowed_host():
            return self.reply({'error': 'Local host only'}, 403)
        path = urllib.parse.urlsplit(self.path).path
        if path == '/api/led':
            with LOCK:
                controller=next((d for d in STATE.get('devices',[]) if mac_normalize(str(d.get('mac','')))=='20:e7:c8:f6:68:80'),None)
                return self.reply({'fixtures':STATE.get('ledFixtures',[]),'canvases':STATE.get('ledCanvases',led_default_canvases()),'virtualFixtures':led_virtuals(),'runtime':LED.manual,'effectiveValues':LED.last_values,'status':LED.status,'outputMap':LED.output_map,'outputTestActive':LED.output_test is not None,'txMuted':MONITOR.tx_muted,'txBlocked':MONITOR.tx_blocked(),'controllerOnline':bool(controller and controller.get('reachability')=='Online'),'portMapSource':controller.get('artnetPortMappingSource') if controller else None})
        if path == '/api/wiz':
            device_id=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('device',[''])[0]
            with LOCK: device=next((dict(d) for d in STATE['devices'] if d['id']==device_id and d.get('kind')=='wiz'),None)
            if not device:return self.reply({'error':'WiZ device not found'},404)
            data=wiz_read(device['ip']); config=data.get('getSystemConfig',{}); state=data.get('getPilot')
            mac=lambda value:str(value or '').replace(':','').lower()
            if config.get('mac') and device.get('mac') and mac(config['mac'])!=mac(device['mac']):return self.reply({'error':'WiZ identity conflict; control disabled'},409)
            if not state:return self.reply({'error':'WiZ is offline or local control is disabled'},503)
            with LOCK:
                current=next((d for d in STATE['devices'] if d['id']==device_id and d.get('kind')=='wiz'),None)
                if current:current.update(reachability='Online',access='Local API',lastSeen=now(),present=True,
                                           state=state,confidence='Protocol response',
                                           model=config.get('moduleName',current.get('model','')),
                                           firmware=config.get('fwVersion',current.get('firmware','')))
            return self.reply({'device':device,'state':state,'online':True})
        if path == '/api/midi':
            params=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            device_id=params.get('device',[''])[0]
            try:after=float(params.get('after',['0'])[0])
            except ValueError:after=0
            with LOCK:
                stored=next((d for d in STATE.get('devices',[]) if d.get('id')==device_id and (d.get('deviceClass')=='midi' or d.get('kind')=='midi')),None)
                if not stored:return self.reply({'error':'MIDI device not found in the local inventory'},404)
                revision=stored.get('midiRevision',0)
                include_config=params.get('revision',[''])[0]!=str(revision)
                device=copy.deepcopy({key:value for key,value in stored.items()
                                      if include_config or key not in ('midiControls','midiRemovedControls','midiTemplate')})
                device['midiRevision']=revision
            return self.reply({'device':device,**midi_snapshot(after)})
        if path == '/api/midi-inputs':
            params=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            controller_id=params.get('device',[''])[0];source_id=params.get('source',[''])[0]
            refresh_midi_inputs()
            with LOCK:
                controller=next((d for d in STATE['devices'] if d['id']==controller_id and d.get('kind')=='artnet'),None)
                if not controller:return self.reply({'error':'Art-Net device not found'},404)
                source=next((d for d in STATE['devices'] if d['id']==source_id and (d.get('kind')=='midi' or d.get('deviceClass')=='midi')),None)
                canvas_revision=tuple((c.get('id'),c.get('name'),c.get('port'),c.get('enabled',True))
                                     for c in STATE.get('ledCanvases',[]) if c.get('controllerId')=='20:e7:c8:f6:68:80')
                token=str(STATE.get('midiInputRevision',0))+'/'+str(source.get('midiRevision',0) if source else 0)+'/'+repr(canvas_revision)
                result=dict(devices=midi_source_cards(controller_id),runtime=MIDI_INPUTS.snapshot(source_id) if source_id else None,
                            configRevision=token,txAllowed=not MONITOR.tx_blocked())
                if source and params.get('revision',[''])[0]!=token:
                    fixtures=midi_patch(controller_id)
                    roles=list(dict.fromkeys(FADER_FUNCTIONS+[ch['function'] for f in fixtures for ch in f['channels'] if ch['function'].casefold() not in ('unknown','unassigned')]))
                    presets=[dict(id=p['id'],name=p['name'],functions=[p['channels'][i]['function'] if i<len(p['channels']) and p['channels'][i]['function'].casefold()!='unknown' else None for i in range(16)]) for p in STATE.get('fixturePresets',PRESETS)]
                    presets.append(dict(id='led-matrix-pixel-fill',name='LED Matrix · Pixel Fill',functions=['Master','Red','Green','Blue','White']+[None]*11))
                    result.update(source=copy.deepcopy(source),sections=copy.deepcopy([s for s in STATE.get('midiInputSections',{}).get(controller_id,[]) if s['sourceId']==source_id]),
                                  fixtures=fixtures,functions=roles,presets=presets,controller=dict(id=controller_id,name=controller['name'],ip=controller['ip']))
                return self.reply(result)
        if path == '/api/input-controls':
            device_id=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('device',[''])[0]
            with LOCK:
                if not any(d['id']==device_id and d['kind']=='artnet' for d in STATE['devices']):return self.reply({'error':'Art-Net device not found'},404)
                io=controller_io();items=STATE.get('inputControls',{}).get(device_id,initial_controls(device_id))
                wiz_targets=wiz_virtual_fixtures(STATE.get('devices',[]),io['outputUniverse'] if io else 1)
                wiz_targets+=sequence_virtual_fixtures(STATE.get('sequences',{}).get(device_id,[]),io['outputUniverse'] if io else 1)
                return self.reply({'controls':effective_controls(items,io),'transmissionEnabled':True,
                                   'inputUniverse':io['inputUniverse'] if io else None,
                                   'outputUniverse':io['outputUniverse'] if io else None,
                                   'faderFunctions':FADER_FUNCTIONS,'wizTargets':wiz_targets})
        if path == '/api/fixtures':
            device_id=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('device',[''])[0]
            with LOCK:
                if not any(d['id']==device_id and d['kind']=='artnet' for d in STATE['devices']):return self.reply({'error':'Art-Net device not found'},404)
                io=controller_io();items=effective_fixtures(STATE.get('fixtureMaps',{}).get(device_id,initial_patch(device_id)),io)
                return self.reply({'fixtures':items,'presets':STATE.get('fixturePresets',PRESETS),
                                   'inputUniverse':io['inputUniverse'] if io else None,
                                   'outputUniverse':io['outputUniverse'] if io else None,
                                   'fxActivations':copy.deepcopy(MONITOR.fx_activations),
                                   'fxSequence':copy.deepcopy(MONITOR.fx_sequence),
                                   'fixtureControls':{k:copy.deepcopy(v) for k,v in MONITOR.fixture_controls.items()
                                       if (v.get('manualUntil') or 0)>time.monotonic() or (v.get('actionDeadline') or 0)>time.monotonic()},
                                   'beamLampStates':copy.deepcopy(STATE.get('beamLampStates',{}))})
        if path == '/api/sequences':
            device_id=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('device',[''])[0]
            with LOCK:
                if not any(d['id']==device_id and d.get('kind')=='artnet' for d in STATE['devices']):return self.reply({'error':'Art-Net device not found'},404)
                io=controller_io();fixtures=effective_fixtures(STATE.get('fixtureMaps',{}).get(device_id,initial_patch(device_id)),io)
                fx=sorted((f for f in fixtures if f.get('mappingPreset')=='fx-activate-effect-4ch'),key=lambda f:f['start'])
                sequences=STATE.setdefault('sequences',{}).setdefault(device_id,[])
                samples=STATE.setdefault('sequenceSamples',{}).setdefault(device_id,[])
                if not samples and fx:
                    sample=dict(id='sample-fx-walk',name='FX Walk',kind='fx-walk',steps=[f['id'] for f in fx],effectSeconds=1.0,intervalSeconds=1.0)
                    samples.append(sample)
                    if not sequences:sequences.append(dict(sample,id='sequence-fx-walk',armed=False))
                    save()
                return self.reply({'sequences':copy.deepcopy(sequences),'samples':copy.deepcopy(samples),
                                   'fixtures':fx,'outputUniverse':io['outputUniverse'] if io else None,
                                   'fxActivations':copy.deepcopy(MONITOR.fx_activations),'fxSequence':copy.deepcopy(MONITOR.fx_sequence)})
        if path == '/api/artnet':
            snapshot=MONITOR.snapshot()
            snapshot['controllerMode']=CONTROLLER.snapshot() if CONTROLLER else None
            return self.reply(snapshot)
        if path == '/api/artnet/export':
            capture=MONITOR.snapshot()
            capture['controllerMode']=CONTROLLER.snapshot() if CONTROLLER else None
            capture['captureLog']=(
                {'logging':MONITOR.logger.status(),'records':MONITOR.logger.recent(50)}
                if MONITOR.logger else None
            )
            return self.reply(capture, extra={'Content-Disposition': 'attachment; filename="siteflow-artnet-capture.json"'})
        if path == '/api/artnet/log':
            if not MONITOR.logger:
                return self.reply({'error':'Capture log is not enabled'},409)
            params=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:limit=int(params.get('limit',['50'])[0])
            except ValueError:return self.reply({'error':'Invalid limit'},400)
            return self.reply({'logging':MONITOR.logger.status(),'records':MONITOR.logger.recent(limit)})
        if path == '/api/artnet/log/file':
            if not MONITOR.logger:return self.reply({'error':'Capture log is not enabled'},409)
            name=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('name',['artnet.jsonl'])[0]
            try:fp,size=MONITOR.logger.open_segment(name)
            except (ValueError,OSError):return self.reply({'error':'Log segment not found'},404)
            self.send_response(200)
            self.send_header('Content-Type','application/x-ndjson')
            self.send_header('Content-Disposition','attachment; filename="'+name+'"')
            self.send_header('Content-Length',str(size))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            try:
                with fp:
                    remaining=size
                    while remaining:
                        block=fp.read(min(65536,remaining))
                        if not block:break
                        self.wfile.write(block);remaining-=len(block)
            except (BrokenPipeError,ConnectionResetError):pass
            return
        if path == '/api/state':
            with LOCK:
                snapshot = json.loads(json.dumps(STATE))
            # Mark old observations stale without pretending a cached address is live.
            for d in snapshot['devices']:
                observed = d.get('neighborObservedAt') or d.get('lastSeen')
                if d.get('present') and observed:
                    age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(observed)).total_seconds()
                    if age > 180:
                        d['present'] = False
                        if d.get('reachability') == 'Online':
                            d['reachability'] = 'Stale'
            snapshot.update(serverTime=now(), csrf=TOKEN, version='0.3.0', outputControl='mapped-fixture-tests-and-input6-forwarding', controllerProfiles=controller_profile_catalog())
            return self.reply(snapshot)
        if path == '/api/export':
            with LOCK:
                result = dict(exportedAt=now(), network=STATE['network'], devices=STATE['devices'])
                return self.reply(result, extra={'Content-Disposition': 'attachment; filename="siteflow-devices.json"'})
        files = {'/led-controls': ('led-controls.html','text/html; charset=utf-8'),
                 '/led-controls.js': ('led-controls.js','text/javascript; charset=utf-8'),
                 '/input-controls': ('input-controls.html','text/html; charset=utf-8'),
                 '/input-controls.js': ('input-controls.js','text/javascript; charset=utf-8'),
                 '/midi-input-cards.js': ('midi-input-cards.js','text/javascript; charset=utf-8'),
                 '/midi-input': ('midi-input.html','text/html; charset=utf-8'),
                 '/midi-input.js': ('midi-input.js','text/javascript; charset=utf-8'),
                 '/traktor-icon.svg': ('traktor-icon.svg','image/svg+xml'),
                 '/fixtures': ('fixtures.html','text/html; charset=utf-8'),
                 '/fixtures.js': ('fixtures.js','text/javascript; charset=utf-8'),
                 '/sequences': ('sequences.html','text/html; charset=utf-8'),
                 '/sequences.js': ('sequences.js','text/javascript; charset=utf-8'),
                 '/fixture': ('fixture.html','text/html; charset=utf-8'),
                 '/fixture.js': ('fixture.js','text/javascript; charset=utf-8'),
                 '/wiz': ('wiz.html','text/html; charset=utf-8'),
                 '/wiz.js': ('wiz.js','text/javascript; charset=utf-8'),
                 '/midi': ('midi.html','text/html; charset=utf-8'),
                 '/midi.js': ('midi.js','text/javascript; charset=utf-8'),
                 '/traktor-surface.js': ('traktor-surface.js','text/javascript; charset=utf-8'),
                 '/assets/traktor/traktor-atlas-v1.png': ('assets/traktor/traktor-atlas-v1.png','image/png'),
                 '/assets/traktor/brushed-black-v1.png': ('assets/traktor/brushed-black-v1.png','image/png'),
                 '/': ('index.html', 'text/html; charset=utf-8'),
                 '/Papinsvet-logo.png': ('Papinsvet-logo.png', 'image/png'),
                 '/artnet': ('artnet.html', 'text/html; charset=utf-8'),
                 '/monitor-page.js': ('monitor-page.js', 'text/javascript; charset=utf-8'),
                 '/app.css': ('app.css', 'text/css; charset=utf-8'),
                 '/restart.js': ('restart.js', 'text/javascript; charset=utf-8'),
                 '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                 '/artnet.js': ('artnet.js', 'text/javascript; charset=utf-8')}
        if path not in files:
            return self.reply({'error': 'Not found'}, 404)
        filename, mime = files[path]
        return self.reply((ROOT / 'static' / filename).read_bytes(), content_type=mime)

    def do_POST(self):
        global RESTART_REQUESTED
        if not self.allowed_origin():
            return self.reply({'error': 'Same-origin local requests only'}, 403)
        if self.headers.get('X-SiteFlow-Token') != TOKEN:
            return self.reply({'error': 'Invalid request token'}, 403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length > (262144 if urllib.parse.urlsplit(self.path).path in ('/api/fixtures','/api/fixtures/import','/api/input-controls','/api/midi-inputs/section') else 8192) or length < 0:
                return self.reply({'error': 'Request too large'}, 413)
            data = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(data, dict):
                raise ValueError('Invalid request')
            path = urllib.parse.urlsplit(self.path).path
            if path == '/api/restart':
                if RESTART_REQUESTED:return self.reply({'error':'Restart is already in progress'},409)
                # Stop our own output first. This does not persist BLACKOUT;
                # startup re-verifies the controller before allowing TX.
                if CONTROLLER is not None:CONTROLLER.stop()
                else:MONITOR.set_tx_muted(True)
                MONITOR.wake_routing()
                RESTART_REQUESTED=True
                self.reply({'accepted':True,'message':'Art-Net TX paused; restarting SiteFlow Node.'},202)
                if HTTP_SERVER is not None:
                    threading.Timer(.35,HTTP_SERVER.shutdown).start()
                return
            if path == '/api/wiz/control':
                device_id=data.get('deviceId')
                with LOCK: device=next((dict(d) for d in STATE['devices'] if d['id']==device_id and d.get('kind')=='wiz'),None)
                if not device:return self.reply({'error':'WiZ device not found'},404)
                if not valid_target(device['ip'],STATE['network'].get('subnet','192.168.18.0/24')):return self.reply({'error':'Device is outside the current local network'},409)
                try:
                    current=wiz_read(device['ip']);config=current.get('getSystemConfig',{})
                    mac=lambda value:str(value or '').replace(':','').lower()
                    if not current.get('getPilot'):raise ValueError('WiZ is offline or local control is disabled')
                    if config.get('mac') and device.get('mac') and mac(config['mac'])!=mac(device['mac']):raise ValueError('WiZ identity conflict; control disabled')
                    action=data.get('action')
                    if action=='off':params={'state':False}
                    elif action=='on':params={'state':True}
                    elif action=='values':
                        brightness=data.get('brightness'); rgb=data.get('rgb'); temp=data.get('temp')
                        if type(brightness) is not int or not 10<=brightness<=100:raise ValueError('Brightness must be 10–100%')
                        params={'state':True,'dimming':brightness}
                        if isinstance(rgb,list) and len(rgb)==3 and all(type(v) is int and 0<=v<=255 for v in rgb):params.update(dict(zip(('r','g','b'),rgb)))
                        elif type(temp) is int and 2200<=temp<=6500:params['temp']=temp
                        elif rgb is not None or temp is not None:raise ValueError('Use RGB values 0–255 or white temperature 2200–6500 K')
                    else:raise ValueError('Select a WiZ control action')
                    state=wiz_command(device['ip'],params)
                except ValueError as exc:return self.reply({'error':str(exc)},409)
                return self.reply({'accepted':True,'state':state})
            if path == '/api/midi/template/import':
                device_id=data.get('deviceId')
                with LOCK:
                    device=next((d for d in STATE.get('devices',[]) if d.get('id')==device_id and (d.get('deviceClass')=='midi' or d.get('kind')=='midi')),None)
                    if not device:return self.reply({'error':'MIDI device not found'},404)
                    if 's2' not in device.get('name','').lower() or 'mk2' not in device.get('name','').lower():
                        return self.reply({'error':'Select the Traktor Kontrol S2 MK2'},400)
                ports=midi_snapshot().get('status',{}).get('ports',[])
                source=data.get('source')
                if source not in ports or 'traktor' not in source.lower() or 's2' not in source.lower():
                    return self.reply({'error':'Select the connected Traktor MIDI input first'},409)
                template_path=Path.home()/'Library/Application Support/Native Instruments/Controller Editor/TraktorKontrolS2MK2.ncs'
                if not template_path.is_file():template_path=ROOT/'profiles/traktor-s2-mk2.ncs'
                try:
                    labels=json.loads((ROOT/'profiles/traktor-surface-labels.json').read_text())
                    records,profile=load_traktor_template(template_path,source,labels)
                except (OSError,ValueError,ET.ParseError) as exc:
                    return self.reply({'error':'Could not read Controller Editor template: '+str(exc)},409)
                with LOCK:
                    backups=STATE_FILE.parent/'midi-backups';backups.mkdir(parents=True,exist_ok=True)
                    backup=backups/('s2-mk2-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+secrets.token_hex(4)+'.json')
                    backup.write_text(json.dumps({key:device.get(key) for key in ('id','midiControls','midiRemovedControls','midiTemplate')},ensure_ascii=False,indent=2))
                    merge_traktor_template(device,records,profile)
                    device['midiRevision']=device.get('midiRevision',0)+1
                    save()
                    return self.reply({'controls':device['midiControls'],'template':profile,
                                       'removedControls':device.get('midiRemovedControls',[]),'backup':backup.name})
            if path == '/api/midi/controls':
                device_id=data.get('deviceId');item=data.get('control')
                if not isinstance(item,dict):return self.reply({'error':'MIDI control details are required'},400)
                with LOCK:
                    device=next((d for d in STATE.get('devices',[]) if d.get('id')==device_id and (d.get('deviceClass')=='midi' or d.get('kind')=='midi')),None)
                    if not device:return self.reply({'error':'MIDI device not found in the local inventory'},404)
                    name=str(item.get('name','')).strip();kind=item.get('type');channel=item.get('channel');number=item.get('number');minimum=item.get('min');maximum=item.get('max')
                    if not name or len(name)>80:return self.reply({'error':'Control name must contain 1–80 characters'},400)
                    if kind not in ('Control Change','Pitch Bend','Note'):return self.reply({'error':'Unsupported MIDI message type'},400)
                    if type(channel) is not int or not 1<=channel<=16:return self.reply({'error':'MIDI channel must be 1–16'},400)
                    if kind!='Pitch Bend' and (type(number) is not int or not 0<=number<=127):return self.reply({'error':'Controller or note number must be 0–127'},400)
                    if kind=='Pitch Bend':number=None
                    if type(minimum) is not int or type(maximum) is not int or minimum>maximum or minimum < (-8192 if kind=='Pitch Bend' else 0) or maximum>(8191 if kind=='Pitch Bend' else 127):return self.reply({'error':'Observed MIDI range is invalid'},400)
                    controls=device.setdefault('midiControls',[])
                    record=dict(name=name,type=kind,channel=channel,number=number,min=minimum,max=maximum,
                                source=str(item.get('source',''))[:128],interpretation=str(item.get('interpretation',''))[:48],
                                surfaceControlId=str(item.get('surfaceControlId',''))[:80],
                                surfaceControlName=str(item.get('surfaceControlName',''))[:100],
                                events=max(0,min(int(item.get('events',0)),1000000)),origin='learned',addedAt=now())
                    existing=next((c for c in controls if c.get('type')==kind and c.get('channel')==channel and c.get('number')==number and c.get('source')==record['source']),None)
                    if existing:existing.update(record)
                    else:controls.append(record)
                    device['midiRevision']=device.get('midiRevision',0)+1
                    save()
                    return self.reply({'controls':controls,'added':record})
            if path in ('/api/midi/controls/remove','/api/midi/controls/restore'):
                device_id=data.get('deviceId')
                with LOCK:
                    device=next((d for d in STATE.get('devices',[]) if d.get('id')==device_id and (d.get('deviceClass')=='midi' or d.get('kind')=='midi')),None)
                    if not device:return self.reply({'error':'MIDI device not found'},404)
                    try:
                        if path.endswith('/remove'):
                            identity=data.get('control')
                            if not isinstance(identity,dict):return self.reply({'error':'Select a saved MIDI control'},400)
                            changed=remove_midi_control(device,identity)
                        else:changed=restore_midi_control(device,data.get('removalId'))
                    except ValueError as exc:return self.reply({'error':str(exc)},409)
                    device['midiRevision']=device.get('midiRevision',0)+1
                    save()
                    return self.reply({'controls':device.get('midiControls',[]),
                                       'removedControls':device.get('midiRemovedControls',[]),'changed':changed})
            if path == '/api/midi/assignment':
                device_id=data.get('deviceId');identity=data.get('control')
                if not isinstance(identity,dict):return self.reply({'error':'Select a saved MIDI control'},400)
                role=data.get('functionRole');target=data.get('target')
                if not isinstance(role,str) or not role.strip() or len(role)>80:return self.reply({'error':'Choose a system function'},400)
                if target not in ('artnet','global') and (not isinstance(target,str) or len(target)>120):return self.reply({'error':'Choose a valid target'},400)
                with LOCK:
                    device=next((d for d in STATE.get('devices',[]) if d.get('id')==device_id and (d.get('deviceClass')=='midi' or d.get('kind')=='midi')),None)
                    if not device:return self.reply({'error':'MIDI device not found'},404)
                    controls=device.get('midiControls',[])
                    control=next((c for c in controls if c.get('type')==identity.get('type') and c.get('channel')==identity.get('channel') and c.get('number')==identity.get('number') and c.get('source')==identity.get('source')),None)
                    if not control:return self.reply({'error':'Save this learned control before assigning its function'},404)
                    control.update(functionRole=role.strip(),target=target,
                                   mappingName=str(data.get('mappingName','')).strip()[:80],assignedAt=now())
                    device['midiRevision']=device.get('midiRevision',0)+1
                    save()
                    return self.reply({'saved':True,'control':control})
            if path == '/api/artnet/mode':
                if CONTROLLER is None:return self.reply({'error':'Controller mode service unavailable'},503)
                try:result=CONTROLLER.switch(data.get('mode'))
                except ValueError as exc:
                    with LOCK:STATE['txMuted']=True;save()
                    return self.reply({'error':str(exc),'controllerMode':CONTROLLER.snapshot()},409)
                with LOCK:
                    STATE['txMuted']=MONITOR.tx_muted;save()
                MONITOR.wake_routing()
                return self.reply(result)
            if path == '/api/led/mapping':
                with LOCK:
                    items=STATE.get('ledFixtures',[]);old=next((f for f in items if f['id']==data.get('id')),None)
                    if not old:raise ValueError('LED fixture not found')
                    new=validate_mapping(data,old,STATE.get('ledCanvases',[]))
                    candidate=[new if f['id']==old['id'] else f for f in items]
                    virtuals=[virtual_fixture(f) for f in candidate]
                    controls=json.loads(json.dumps(STATE.get('inputControls',{})))
                    for did,bank in controls.items():
                        patch=STATE.get('fixtureMaps',{}).get(did,initial_patch(did))+virtuals
                        controls[did]=[validate_control(c,bank,patch) for c in bank]
                    STATE.update(ledFixtures=candidate,inputControls=controls);LED.manual.pop(old['id'],None);save()
                    MONITOR.wake_routing()
                    return self.reply({'fixtures':candidate})
            if path == '/api/led/port-map':
                controller_id=mac_normalize(str(data.get('controllerId','')))
                ranges=data.get('ranges')
                if not valid_sp801e_port_ranges(ranges):
                    raise ValueError('Enter four ordered, non-overlapping contiguous Universe ranges (1–8 Universes per port).')
                with LOCK:
                    controller=next((d for d in STATE.get('devices',[]) if mac_normalize(str(d.get('mac','')))==controller_id and d.get('kind')=='artnet_led'),None)
                    if not controller or controller.get('model')!='SP801E':
                        raise ValueError('Select the confirmed SP801E controller by MAC address.')
                    # Protect saved canvases from silently moving to a different port patch.
                    for canvas in STATE.get('ledCanvases',[]):
                        if mac_normalize(str(canvas.get('controllerId','')))!=controller_id:continue
                        port=int(canvas.get('port',0));start=canvas.get('firstUniverse');count=canvas.get('universeCount',0)
                        if not 1<=port<=4 or type(start) is not int or type(count) is not int:
                            raise ValueError(f'Canvas “{canvas.get("name","(unnamed)")}” has an invalid port patch; edit or remove it before changing ranges.')
                        row=ranges[port-1];end=start+count-1
                        if start<row[0] or end>row[-1]:
                            raise ValueError(f'New P{port} range U{row[0]}–U{row[-1]} would exclude canvas “{canvas.get("name","(unnamed)")}” (U{start}–U{end}).')
                    controller['artnetOutputManualRanges']=copy.deepcopy(ranges)
                    controller['artnetOutputRanges']=copy.deepcopy(ranges)
                    controller['artnetOutputUniverses']=[row[0] for row in ranges]
                    controller['artnetPortMappingSource']='Manual per-port map'
                    save()
                MONITOR.wake_routing()
                return self.reply({'saved':True,'artnetOutputRanges':ranges,'artnetOutputUniverses':[row[0] for row in ranges],
                                   'source':'Manual per-port map'})
            if path == '/api/led/canvas-test':
                if data.get('action')=='stop':
                    canvas_id=str(data.get('canvasId',''))
                    with LOCK:
                        active=LED.output_test
                        if not active or active.get('mode')!='canvas-sweep' or active.get('canvas',{}).get('id')!=canvas_id:
                            return self.reply({'error':'That Canvas test is not running'},409)
                        LED.output_test=None
                        LED.status=dict(active=False,message='Canvas sweep stopped')
                    return self.reply({'accepted':True,'stopped':True})
                if MONITOR.tx_blocked():return self.reply({'error':'Art-Net TX is stopped; select SOFTWARE CONTROL and release BLACKOUT first'},409)
                if not MONITOR.running:return self.reply({'error':'Art-Net sender is unavailable'},503)
                controller_id=mac_normalize(str(data.get('controllerId','')))
                with LOCK:
                    controller=next((d for d in STATE.get('devices',[]) if mac_normalize(str(d.get('mac','')))==controller_id and d.get('kind')=='artnet_led'),None)
                    ranges=controller.get('artnetOutputRanges') if controller else None
                    if not controller or controller.get('reachability')!='Online' or not isinstance(ranges,list) or len(ranges)!=4 or any(not isinstance(row,list) or not row for row in ranges):
                        return self.reply({'error':'Fresh controller output map is required'},409)
                    if LED.output_test is not None:return self.reply({'error':'Another LED output test is already running'},409)
                    canvas_id=str(data.get('canvasId',''))
                    raw_canvas=data.get('canvas')
                    if isinstance(raw_canvas,dict):
                        if mac_normalize(str(raw_canvas.get('controllerId','')))!=controller_id:return self.reply({'error':'Canvas belongs to another controller'},400)
                        raw_canvas=dict(raw_canvas,controllerIp=controller['ip'])
                    else:
                        raw_canvas=next((copy.deepcopy(c) for c in STATE.get('ledCanvases',[]) if c.get('id')==canvas_id and mac_normalize(str(c.get('controllerId','')))==controller_id),None)
                    if not isinstance(raw_canvas,dict):return self.reply({'error':'Canvas settings were not found'},404)
                    candidates=[copy.deepcopy(c) for c in STATE.get('ledCanvases',[]) if c.get('id')!=raw_canvas.get('id')]
                    candidates.append(raw_canvas)
                    validated=led_validate_canvases(candidates)
                    canvas=next(c for c in validated if c['id']==raw_canvas.get('id'))
                    port_ranges=ranges[canvas['port']-1]
                    first=canvas['firstUniverse'];last=first+canvas['universeCount']-1
                    reported=controller.get('artnetOutputUniverses')
                    if first!=port_ranges[0] or not isinstance(reported,list) or len(reported)!=4 or first!=reported[canvas['port']-1] or last>port_ranges[-1]:
                        return self.reply({'error':f'Canvas U{first}–U{last} does not match the current controller output range for P{canvas["port"]}'},409)
                    addresses=canvas_sweep_addresses(canvas)
                    if not addresses:return self.reply({'error':'Canvas has no mapped pixels'},400)
                    base_order=next((f.get('order','BRG') for f in STATE.get('ledFixtures',[]) if f.get('controllerId')==controller_id),'BRG')
                    orders=[next((c.get('colorOrder',base_order) for c in STATE.get('ledCanvases',[]) if c.get('controllerId')==controller_id and c.get('port')==port),base_order) for port in range(1,5)]
                    orders[canvas['port']-1]=canvas['colorOrder']
                    LED.locator=None
                    LED.locator_suspended=True
                    started=time.monotonic()
                    LED.output_test=dict(mode='canvas-sweep',ip=controller['ip'],controllerId=controller_id,ranges=copy.deepcopy(ranges),orders=orders,
                                         canvas=canvas,addresses=addresses,started=started,stepSeconds=.12,step=0,nextStepAt=started)
                return self.reply({'accepted':True,'canvasId':canvas['id'],'pixels':canvas['pixelCount'],'passes':['top-to-bottom','left-to-right']})
            if path == '/api/led/output-test':
                if MONITOR.tx_blocked():return self.reply({'error':'Art-Net TX is stopped; select SOFTWARE CONTROL and release BLACKOUT first'},409)
                if not MONITOR.running:return self.reply({'error':'Art-Net sender is unavailable'},503)
                controller_id=mac_normalize(str(data.get('controllerId','')))
                with LOCK:
                    controller=next((d for d in STATE.get('devices',[]) if mac_normalize(str(d.get('mac','')))==controller_id and d.get('kind')=='artnet_led'),None)
                    ranges=controller.get('artnetOutputRanges') if controller else None
                    if not controller or controller.get('reachability')!='Online' or not isinstance(ranges,list) or len(ranges)!=4 or any(not isinstance(row,list) or not row for row in ranges):
                        return self.reply({'error':'Fresh controller output map is required'},409)
                    if LED.output_test is not None:return self.reply({'error':'Output test already running'},409)
                    base_order=next((f.get('order','BRG') for f in STATE.get('ledFixtures',[]) if f.get('controllerId')==controller_id),'BRG')
                    orders=[next((c.get('colorOrder',base_order) for c in STATE.get('ledCanvases',[]) if c.get('controllerId')==controller_id and c.get('port')==port),base_order) for port in range(1,5)]
                    LED.locator=None
                    LED.locator_suspended=True
                    LED.output_test=dict(ip=controller['ip'],controllerId=controller_id,ranges=copy.deepcopy(ranges),orders=orders,started=time.monotonic())
                return self.reply({'accepted':True,'durationSeconds':2,'phases':['white','red','off']})
            if path == '/api/led/locator':
                if data.get('active') is not True:
                    with LOCK:
                        LED.locator=None
                    return self.reply({'accepted':True,'active':False})
                if MONITOR.tx_blocked():return self.reply({'error':'Blackout · resume TX first'},409)
                controller_id=mac_normalize(str(data.get('controllerId','')))
                port=data.get('port');pixel=data.get('pixel');universe=data.get('firstUniverse');order=data.get('colorOrder','BRG')
                if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',controller_id):raise ValueError('Controller MAC identity is required')
                if type(port) is not int or not 1<=port<=4 or type(pixel) is not int or not 1<=pixel<=1024:raise ValueError('Select port P1–P4 and pixel 1–1024')
                if type(universe) is not int or not 0<=universe<=32767 or order not in ('RGB','RBG','GRB','GBR','BRG','BGR'):raise ValueError('Invalid locator Universe or color order')
                with LOCK:
                    controller=next((d for d in STATE.get('devices',[]) if d.get('mac') and mac_normalize(d['mac'])==controller_id and d.get('kind')=='artnet_led'),None)
                    if not controller:raise ValueError('Known LED controller is not currently available')
                    reported=controller.get('artnetOutputUniverses')
                    if isinstance(reported,list) and len(reported)==4 and universe!=reported[port-1]:
                        raise ValueError(f'Port P{port} starts at Art-Net Universe {reported[port-1]}')
                    ranges=controller.get('artnetOutputRanges')
                    if not isinstance(ranges,list) or len(ranges)!=4 or not ranges[port-1]:raise ValueError('Controller output map is unavailable')
                    pixel_capacity=min(1024,len(ranges[port-1])*512//3)
                    if pixel>pixel_capacity:raise ValueError(f'P{port} can address only {pixel_capacity} pixels with its reported Universe range (SP801E limit: 1024)')
                    if LED.output_test is not None:raise ValueError('Output address test is running')
                    if LED.locator_suspended and data.get('userAction') is not True:
                        return self.reply({'error':'Address locator paused by LED control; move its slider to resume'},409)
                    if data.get('userAction') is True:LED.locator_suspended=False
                    LED.locator=dict(controllerId=controller_id,ip=controller['ip'],port=port,pixel=pixel,
                                     firstUniverse=universe,reportedUniverses=[u for row in ranges for u in row],colorOrder=order,updated=time.monotonic())
                return self.reply({'accepted':True,'active':True,'port':port,'pixel':pixel})
            if path == '/api/led/canvases':
                with LOCK:
                    canvases=list(STATE.get('ledCanvases',led_default_canvases()))
                    action=data.get('action','save')
                    if action=='save':
                        canvas=data.get('canvas')
                        if not isinstance(canvas,dict):raise ValueError('Canvas settings are required')
                        controller_id=mac_normalize(str(canvas.get('controllerId','')))
                        controller=next((d for d in STATE.get('devices',[]) if d.get('mac') and mac_normalize(d['mac'])==controller_id),None)
                        if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',controller_id) or not controller or controller.get('kind')!='artnet_led':
                            raise ValueError('Select a known Art-Net LED controller by its MAC identity')
                        canvas['controllerId']=controller_id
                        canvas['controllerIp']=controller['ip']
                        canvases=[c for c in canvases if c.get('id')!=canvas.get('id')]+[canvas]
                    elif action=='delete':
                        canvas_id=data.get('id')
                        if not any(c.get('id')==canvas_id for c in canvases):raise ValueError('Canvas not found')
                        canvases=[c for c in canvases if c.get('id')!=canvas_id]
                    else:raise ValueError('Invalid canvas action')
                    canvases=led_validate_canvases(canvases)
                    for saved_canvas in canvases:
                        owner=next((d for d in STATE['devices'] if mac_normalize(str(d.get('mac') or ''))==saved_canvas['controllerId']),None)
                        outputs=owner.get('artnetOutputUniverses') if owner else None
                        ranges=owner.get('artnetOutputRanges') if owner else None
                        if not isinstance(outputs,list) or len(outputs)!=4:continue
                        port=saved_canvas['port'];start=saved_canvas['firstUniverse'];end=start+saved_canvas['universeCount']-1
                        pixel_capacity=min(1024,len(ranges[port-1])*512//3) if isinstance(ranges,list) and len(ranges)==4 and ranges[port-1] else 0
                        if saved_canvas['startPixel']+saved_canvas['pixelCount']-1>pixel_capacity:
                            raise ValueError(f'Canvas exceeds P{port} capacity: {pixel_capacity} pixels from the current ArtPoll Universe allocation (maximum 1024)')
                        if start!=outputs[port-1]:raise ValueError(f'Port P{port} is reported on Universe {outputs[port-1]}')
                        if isinstance(ranges,list) and len(ranges)==4 and end>ranges[port-1][-1]:
                            raise ValueError(f'Canvas exceeds the ArtPoll Universe range allocated to P{port}')
                        if any(start<=u<=end for index,u in enumerate(outputs) if index+1!=port):
                            raise ValueError('Canvas pixels cross into another reported output Universe; verify controller port mapping')
                    STATE['ledCanvases']=canvases;save()
                    return self.reply({'saved':True,'canvases':canvases,
                                       'outputNote':'Canvas software output is assigned to its saved Universe range. Match that Universe start in the SP801E port settings; the controller itself is not reconfigured here.'})
            if path == '/api/led/control':
                with LOCK:
                    f=next((f for f in STATE.get('ledFixtures',[]) if f['id']==data.get('id')),None)
                    if not f:raise ValueError('LED fixture not found')
                    if MONITOR.tx_muted:return self.reply({'error':'Blackout · resume TX first'},409)
                    LED.set_values(f,data.get('values'),data.get('enabled'))
                    LED.locator=None
                    LED.locator_suspended=True
                    return self.reply({'accepted':True})
            if path == '/api/midi-inputs/section':
                controller_id=data.get('deviceId');source_id=data.get('sourceId');action=data.get('action','save')
                with LOCK:
                    if not any(d['id']==controller_id and d.get('kind')=='artnet' for d in STATE['devices']):return self.reply({'error':'Art-Net device not found'},404)
                    source=next((d for d in STATE['devices'] if d['id']==source_id and (d.get('kind')=='midi' or d.get('deviceClass')=='midi')),None)
                    if not source:return self.reply({'error':'MIDI device not found'},404)
                    banks=STATE.setdefault('midiInputSections',{});sections=list(banks.get(controller_id,[]))
                    if action=='save':
                        section=validate_midi_section(data.get('section'),sections,source.get('midiControls',[]),midi_patch(controller_id),source_id)
                        sections=[s for s in sections if s['id']!=section['id']]+[section]
                    elif action=='remove':
                        section=next((s for s in sections if s['id']==data.get('sectionId') and s['sourceId']==source_id),None)
                        if not section:return self.reply({'error':'MIDI section not found'},404)
                        STATE.setdefault('midiRemovedSections',[]).append(dict(controllerId=controller_id,section=section,removedAt=now()))
                        sections=[s for s in sections if s['id']!=section['id']]
                    else:raise ValueError('Invalid MIDI section action')
                    banks[controller_id]=sections;STATE['midiInputRevision']=STATE.get('midiInputRevision',0)+1;save()
                refresh_midi_inputs();MONITOR.wake_routing()
                return self.reply({'section':section,'sections':[s for s in sections if s['sourceId']==source_id]})
            if path == '/api/midi-inputs/element':
                controller_id=data.get('deviceId');source_id=data.get('sourceId')
                with LOCK:
                    if not any(d['id']==controller_id and d.get('kind')=='artnet' for d in STATE['devices']):return self.reply({'error':'Art-Net device not found'},404)
                if not isinstance(data.get('control'),dict):raise ValueError('Select a MIDI element')
                refresh_midi_inputs()
                matched=MIDI_INPUTS.mouse(controller_id,source_id,data['control'],data.get('kind'),data.get('value'))
                MONITOR.wake_routing()
                return self.reply({'accepted':True,'matchedSlots':matched,'runtime':MIDI_INPUTS.snapshot(source_id)})
            if path == '/api/midi-inputs/fader':
                with LOCK:
                    section=next((s for s in STATE.get('midiInputSections',{}).get(data.get('deviceId'),[]) if s['id']==data.get('sectionId') and s['sourceId']==data.get('sourceId')),None)
                    if not section:return self.reply({'error':'MIDI section not found'},404)
                refresh_midi_inputs();MIDI_INPUTS.manual(section['id'],data.get('fader'),data.get('value'));MONITOR.wake_routing()
                return self.reply({'accepted':True,'runtime':MIDI_INPUTS.snapshot(section['sourceId'])})
            if path == '/api/input-controls':
                with LOCK:
                    device_id=data.get('deviceId')
                    if not any(d['id']==device_id and d['kind']=='artnet' for d in STATE['devices']):return self.reply({'error':'Art-Net device not found'},404)
                    io=controller_io()
                    maps=STATE.setdefault('inputControls',{})
                    items=copy.deepcopy(maps.get(device_id,initial_controls(device_id)))
                    if io:
                        for item in items:item['universe']=io['inputUniverse']
                    fixtures=copy.deepcopy(STATE.get('fixtureMaps',{}).get(device_id,initial_patch(device_id)))
                    if io:
                        for item in fixtures:item['universe']=io['outputUniverse']
                    fixtures+=led_virtuals(io['outputUniverse'] if io else 1)
                    fixtures+=wiz_virtual_fixtures(STATE.get('devices',[]),io['outputUniverse'] if io else 1)
                    fixtures+=sequence_virtual_fixtures(STATE.get('sequences',{}).get(device_id,[]),io['outputUniverse'] if io else 1)
                    control=validate_control(data.get('control'),items,fixtures)
                    maps[device_id]=[control if c['id']==control['id'] else c for c in items] if any(c['id']==control['id'] for c in items) else items+[control]
                    save()
                    MONITOR.wake_routing()
                    io=controller_io()
                    return self.reply({'controls':effective_controls(maps[device_id],io),'transmissionEnabled':True,
                                       'inputUniverse':io['inputUniverse'] if io else None,
                                       'outputUniverse':io['outputUniverse'] if io else None,
                                       'faderFunctions':FADER_FUNCTIONS})
            if path == '/api/fixtures':
                device_id=data.get('deviceId')
                with LOCK:
                    if not any(d['id']==device_id and d['kind']=='artnet' for d in STATE['devices']):return self.reply({'error':'Art-Net device not found'},404)
                    io=controller_io()
                    maps,presets,controls,affected=prepare_fixture_update(
                        STATE,device_id,data.get('fixture'),
                        io['inputUniverse'] if io else None,io['outputUniverse'] if io else None,
                        wiz_virtual_fixtures(STATE.get('devices',[]),io['outputUniverse'] if io else 1))
                    STATE.update(fixtureMaps=maps,fixturePresets=presets,inputControls=controls)
                    save()
                    MONITOR.wake_routing()
                    io=controller_io();items=effective_fixtures(maps[device_id],io)
                    return self.reply({'fixtures':items,'presets':presets,'affectedFixtures':affected,
                                       'inputUniverse':io['inputUniverse'] if io else None,
                                       'outputUniverse':io['outputUniverse'] if io else None})
            if path == '/api/fixtures/import':
                device_id=data.get('deviceId')
                with LOCK:
                    if not any(d['id']==device_id and d['kind']=='artnet' for d in STATE['devices']):return self.reply({'error':'Art-Net device not found'},404)
                    io=controller_io();plan=prepare_fixture_import(
                        STATE,device_id,data.get('fixtures'),
                        io['inputUniverse'] if io else None,io['outputUniverse'] if io else None,
                        wiz_virtual_fixtures(STATE.get('devices',[]),io['outputUniverse'] if io else 1))
                    patch_snapshot=STATE.get('fixtureMaps',{}).get(device_id,initial_patch(device_id))
                    fingerprint=hashlib.sha256(json.dumps(patch_snapshot,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                    applied=False
                    if data.get('action')=='apply':
                        if data.get('confirm') is not True:return self.reply({'error':'Import confirmation required'},400)
                        if data.get('expectedFingerprint')!=fingerprint or data.get('expectedOutputUniverse')!=(io['outputUniverse'] if io else None):
                            return self.reply({'error':'The patch or controller output changed after preview. Review the refreshed import preview.'},409)
                        maps,presets,controls=plan['state']
                        if maps is not None:
                            STATE.update(fixtureMaps=maps,fixturePresets=presets,inputControls=controls)
                            save();MONITOR.wake_routing();applied=True
                    elif data.get('action')!='preview':return self.reply({'error':'Choose preview or apply'},400)
                    return self.reply({'rows':plan['rows'],'ready':plan['ready'],'existing':plan['existing'],
                                       'blocked':plan['blocked'],'applied':applied,
                                       'patchFingerprint':fingerprint,
                                       'fixtures':effective_fixtures(STATE.get('fixtureMaps',{}).get(device_id,initial_patch(device_id)),io),
                                       'inputUniverse':io['inputUniverse'] if io else None,
                                       'outputUniverse':io['outputUniverse'] if io else None})
            if path == '/api/fixtures/control':
                device_id=data.get('deviceId')
                action=data.get('action')
                with LOCK:
                    d=next((d for d in STATE['devices'] if d['id']==device_id and d['kind']=='artnet'),None)
                    fixture=next((f for f in STATE.get('fixtureMaps',{}).get(device_id,initial_patch(device_id)) if f['id']==data.get('fixtureId')),None)
                    io=controller_io()
                    if not d or not fixture:return self.reply({'error':'Fixture not found'},404)
                    if fixture.get('mappingPreset')!='beam230-16ch':return self.reply({'error':'Mouse control is available for the Beam 230 profile'},409)
                    if not io or io['outputUniverse'] is None:return self.reply({'error':'Controller output configuration is unknown'},409)
                    universe=io['outputUniverse'];fixture=copy.deepcopy(fixture);fixture['universe']=universe
                    fixture_id=fixture['id'];lamp_states=STATE.setdefault('beamLampStates',{});lamp=lamp_states.setdefault(fixture_id,dict(phase='unknown',coolingUntil=None,actionUntil=None))
                    now_epoch=time.time();phase=lamp.get('phase','unknown')
                    if action=='lamp-stable':
                        if phase!='ignition-wait':return self.reply({'error':'No pending ignition confirmation'},409)
                        lamp.update(phase='on',stableConfirmedAt=now(),coolingUntil=None);save()
                        return self.reply({'accepted':True,'lampState':copy.deepcopy(lamp),'physicalIgnitionConfirmed':False})
                    if action not in (None,'safe-off','lamp-on','lamp-reset'):return self.reply({'error':'Unknown Beam lamp action'},400)
                    if action in ('lamp-on','safe-off','lamp-reset') and (lamp.get('actionUntil') or 0)>now_epoch:return self.reply({'error':'Lamp command is still in progress'},409)
                    if action=='lamp-reset':
                        if data.get('confirmed') is not True:return self.reply({'error':'Manual reset requires explicit confirmation'},409)
                try:
                    result=MONITOR.set_fixture_control(d['ip'],fixture,data.get('mode','original'),data.get('values'),action,universe,data.get('colorSlots'))
                except ValueError as exc:return self.reply({'error':str(exc)},409)
                if action in ('safe-off','lamp-on','lamp-reset'):
                    started=time.time()
                    with LOCK:
                        lamp=STATE.setdefault('beamLampStates',{}).setdefault(fixture_id,{})
                        lamp.update(phase={'safe-off':'off-pulse','lamp-on':'on-pulse','lamp-reset':'reset-pulse'}[action],
                                    action=action,startedAt=started,actionUntil=started+4,
                                    coolingUntil=None,
                                    stableConfirmedAt=None if action in ('safe-off','lamp-on') else lamp.get('stableConfirmedAt'))
                        save();result['lampState']=copy.deepcopy(lamp)
                    timer=threading.Timer(4,finish_beam_lamp_action,args=(fixture_id,started,action));timer.daemon=True;timer.start()
                elif action is None:
                    with LOCK:result['lampState']=copy.deepcopy(STATE.setdefault('beamLampStates',{}).get(fixture_id,{}))
                MONITOR.wake_routing()
                return self.reply(result)
            if path == '/api/fixtures/fx-activate':
                device_id=data.get('deviceId');fixture_id=data.get('fixtureId');enabled=data.get('enabled')
                with LOCK:
                    d=next((d for d in STATE['devices'] if d['id']==device_id and d['kind']=='artnet'),None)
                    io=controller_io()
                    fixture=next((f for f in effective_fixtures(STATE.get('fixtureMaps',{}).get(device_id,initial_patch(device_id)),io) if f['id']==fixture_id),None) if d else None
                    if not d or not fixture:return self.reply({'error':'FX fixture not found'},404)
                    if not io or io['outputUniverse'] is None:return self.reply({'error':'Controller output universe is unverified'},409)
                    target=d['ip'];universe=io['outputUniverse']
                try:result=MONITOR.set_fx_activation(target,fixture,enabled,universe)
                except ValueError as exc:return self.reply({'error':str(exc)},409)
                return self.reply(result)
            if path == '/api/sequences':
                device_id=data.get('deviceId');action=data.get('action','save')
                with LOCK:
                    d=next((d for d in STATE['devices'] if d['id']==device_id and d.get('kind')=='artnet'),None)
                    if not d:return self.reply({'error':'Art-Net device not found'},404)
                    io=controller_io();fixtures=effective_fixtures(STATE.get('fixtureMaps',{}).get(device_id,initial_patch(device_id)),io)
                    fx=[f for f in fixtures if f.get('mappingPreset')=='fx-activate-effect-4ch']
                    sequences=list(STATE.setdefault('sequences',{}).get(device_id,[]))
                    samples=list(STATE.setdefault('sequenceSamples',{}).get(device_id,[]))
                    if action=='save':
                        sequence=validate_sequence(data.get('sequence'),sequences,fx)
                        sequences=[sequence if item['id']==sequence['id'] else item for item in sequences]
                        if not any(item['id']==sequence['id'] for item in sequences):sequences.append(sequence)
                    elif action=='delete':
                        sequence=next((item for item in sequences if item['id']==data.get('sequenceId')),None)
                        if not sequence:return self.reply({'error':'Sequence not found'},404)
                        if MONITOR.fx_sequence.get('active') and set(MONITOR.fx_sequence.get('fixtureIds',[])) & set(sequence['steps']):return self.reply({'error':'Stop the running FX sequence before deleting it'},409)
                        sequences=[item for item in sequences if item['id']!=sequence['id']]
                    elif action=='save-sample':
                        sample=validate_sample(data.get('sample'),fx)
                        samples=[sample if item['id']==sample['id'] else item for item in samples]
                        if not any(item['id']==sample['id'] for item in samples):samples.append(sample)
                    elif action=='add-sample':
                        sample=next((item for item in samples if item['id']==data.get('sampleId')),None)
                        if not sample:return self.reply({'error':'Sample preset not found'},404)
                        sequence=validate_sequence(dict(sample,id='sequence-'+secrets.token_hex(8),armed=False),sequences,fx)
                        sequences.append(sequence)
                    elif action=='arm':
                        sequence=next((item for item in sequences if item['id']==data.get('sequenceId')),None)
                        if not sequence:return self.reply({'error':'Sequence not found'},404)
                        if type(data.get('armed')) is not bool:raise ValueError('Invalid trigger state')
                        sequence=dict(sequence,armed=data['armed'])
                        sequences=[sequence if item['id']==sequence['id'] else item for item in sequences]
                    else:raise ValueError('Invalid sequence action')
                    STATE.setdefault('sequences',{})[device_id]=sequences
                    STATE.setdefault('sequenceSamples',{})[device_id]=samples
                    save()
                    universe=io['outputUniverse'] if io else 1
                    virtuals=sequence_virtual_fixtures(sequences,universe)
                    MONITOR.wake_routing()
                    return self.reply({'sequences':sequences,'samples':samples,'sequence':sequence if action in ('save','arm','add-sample') else None,
                                       'virtualFixtures':virtuals})
            if path == '/api/fixtures/sequence-test':
                device_id=data.get('deviceId');fixture_ids=data.get('fixtureIds')
                effect_seconds=data.get('effectSeconds');interval_seconds=data.get('intervalSeconds')
                with LOCK:
                    d=next((d for d in STATE['devices'] if d['id']==device_id and d['kind']=='artnet'),None)
                    io=controller_io()
                    fixtures=effective_fixtures(STATE.get('fixtureMaps',{}).get(device_id,initial_patch(device_id)),io) if d else []
                    if not d:return self.reply({'error':'Art-Net device not found'},404)
                    if not io or io['outputUniverse'] is None:return self.reply({'error':'Controller output universe is unverified'},409)
                    target=d['ip'];universe=io['outputUniverse']
                try:result=MONITOR.start_fx_sequence(target,fixtures,fixture_ids,universe,effect_seconds,interval_seconds)
                except ValueError as exc:return self.reply({'error':str(exc)},409)
                return self.reply({'sequence':result},202)
            if path == '/api/fixtures/sequence-stop':
                MONITOR.stop_fx_sequence()
                return self.reply({'sequence':copy.deepcopy(MONITOR.fx_sequence)})
            if path == '/api/input-controls/fader':
                with LOCK:
                    device_id=data.get('deviceId')
                    d=next((d for d in STATE['devices'] if d['id']==device_id and d['kind']=='artnet'),None)
                    control=next((c for c in STATE.get('inputControls',{}).get(device_id,[]) if c['id']==data.get('controlId')),None)
                    if not d or not control:return self.reply({'error':'Input control not found'},404)
                    ip=d['ip'];control=json.loads(json.dumps(control));io=controller_io()
                    if io:control['universe']=io['inputUniverse']
                try:override=MONITOR.set_manual_fader(ip,control,data.get('fader'),data.get('value'),data.get('expectedInput'),expected_offline=data.get('expectedInput') is None)
                except ValueError as exc:return self.reply({'error':str(exc)},409)
                MONITOR.wake_routing()
                return self.reply({'override':override})
            if path == '/api/artnet/blackout':
                enabled=data.get('enabled')
                if type(enabled) is not bool:raise ValueError('Invalid blackout state')
                if CONTROLLER is not None:
                    try:
                        result=CONTROLLER.stop() if enabled else CONTROLLER.resume()
                    except ValueError as exc:return self.reply({'error':str(exc)},409)
                    if not isinstance(result,dict):result={'txMuted':MONITOR.tx_muted,'zeroSent':False}
                    result.update(txMuted=MONITOR.tx_muted,
                                  message='BLACKOUT · 512 zero values sent before TX stopped' if MONITOR.tx_muted and result.get('zeroSent') else 'BLACKOUT · TX stopped; zero frame not confirmed' if MONITOR.tx_muted else 'BLACKOUT released')
                    MONITOR.blackout_status=result
                    with LOCK:
                        STATE['txMuted']=MONITOR.tx_muted;STATE['blackoutStatus']=result;save()
                    MONITOR.wake_routing()
                    return self.reply(result)
                if enabled:
                    with LOCK:
                        device=next((d for d in STATE['devices'] if d['kind']=='artnet'),None)
                    if not device:raise ValueError('Art-Net device not found')
                    result=MONITOR.blackout(device['ip'],controller_frames(STATE.get('ledFixtures',[]),{f['id']:[0]*len(f['mapping']) for f in STATE.get('ledFixtures',[])},0,canvases=STATE.get('ledCanvases',[])))
                else:
                    MONITOR.set_tx_muted(False)
                    result={'txMuted':False}
                with LOCK:
                    STATE['txMuted']=enabled
                    if enabled:STATE['blackoutStatus']=result
                    save()
                MONITOR.wake_routing()
                return self.reply(result,409 if result.get('error') else 200)
            if path == '/api/fixtures/test':
                with LOCK:
                    device_id=data.get('deviceId')
                    d=next((d for d in STATE['devices'] if d['id']==device_id and d['kind']=='artnet'),None)
                    if not d:return self.reply({'error':'Art-Net device not found'},404)
                    io=controller_io()
                    items=effective_fixtures(STATE.get('fixtureMaps',{}).get(device_id,initial_patch(device_id)),io)
                    presets=json.loads(json.dumps(STATE.get('fixturePresets',PRESETS)))
                    target=d['ip']
                try:result=MONITOR.start_fixture_test(target,items,presets,
                                                       io['outputUniverse'] if io else None,
                                                       io['inputUniverse'] if io else None)
                except ValueError as exc:return self.reply({'error':str(exc)},409)
                return self.reply({'test':result},202)
            if path == '/api/artnet/poll':
                with LOCK:
                    d = next((d for d in STATE['devices'] if d['id'] == data.get('deviceId') and d['kind'] in ('artnet', 'artnet_led')), None)
                    if d is None:
                        return self.reply({'error': 'Art-Net device not found'}, 404)
                    target = d['ip']
                try:
                    MONITOR.poll(target)
                except (ValueError, OSError) as exc:
                    return self.reply({'error': str(exc)}, 409)
                return self.reply({'sent': 'ArtPoll', 'outputEnabled': False})
            if path == '/api/scan':
                mode = data.get('mode', 'scan')
                if mode not in ('scan', 'refresh'):
                    raise ValueError('Invalid scan mode')
                target = data.get('deviceId')
                with LOCK:
                    if target is not None and not any(d['id'] == target for d in STATE['devices']):
                        return self.reply({'error': 'Device not found'}, 404)
                return self.reply({'started': start_scan(mode, target)})
            if path == '/api/device':
                with LOCK:
                    d = next((d for d in STATE['devices'] if d['id'] == data.get('id')), None)
                    if d is None:
                        return self.reply({'error': 'Device not found'}, 404)
                    changes = data.get('changes', {})
                    if not isinstance(changes, dict) or any(k not in ('name', 'zone', 'notes', 'role', 'tracked', 'deviceProfile', 'controllerProfileId') for k in changes):
                        raise ValueError('Only inventory labels and tracking can be edited')
                    if d.get('controllerProfileSource')=='artpoll':
                        if 'controllerProfileId' in changes and changes['controllerProfileId']!=d.get('controllerProfileId'):
                            raise ValueError('SP801E profile is fixed by the controller ArtPoll identity')
                        if 'deviceProfile' in changes and changes['deviceProfile'] not in ('auto',d.get('deviceProfile')):
                            raise ValueError('SP801E card type is fixed by the controller ArtPoll identity')
                        changes={k:v for k,v in changes.items() if k not in ('controllerProfileId','deviceProfile')}
                    for k, v in changes.items():
                        if k == 'tracked':
                            if not isinstance(v, bool):
                                raise ValueError('Invalid tracking value')
                        elif k == 'deviceProfile':
                            if v not in ('auto','artnet_dmx','artnet_pixels','wiz_light','smart_switch','midi_controller','usb_device','bluetooth_device','camera','router','network_device'):
                                raise ValueError('Invalid device profile')
                        elif k == 'controllerProfileId':
                            profile_ids = {p.get('profileId') for p in controller_profile_catalog()}
                            if v not in ('', None) and v not in profile_ids:
                                raise ValueError('Unknown controller profile')
                        elif not isinstance(v, str) or len(v) > (1000 if k == 'notes' else 120):
                            raise ValueError('Invalid label')
                        if k == 'name' and not v.strip():
                            raise ValueError('A device name is required')
                    previous_controller_profile = d.get('controllerProfileId')
                    d.update({k: v.strip() if isinstance(v, str) else v for k, v in changes.items()})
                    if 'deviceProfile' in changes:
                        if changes['deviceProfile'] == 'auto':
                            d.pop('deviceProfileSource', None)
                        else:
                            d['deviceProfileSource'] = 'user'
                            d['configurationOnly']=False
                    if 'controllerProfileId' in changes:
                        profile_id = changes['controllerProfileId']
                        if profile_id:
                            selected_profile = next(p for p in controller_profile_catalog() if p.get('profileId') == profile_id)
                            if previous_controller_profile != profile_id and not d.get('controllerProfileBase'):
                                d['controllerProfileBase'] = {key:d.get(key) for key in ('kind','model','role')}
                            d.update(controllerProfileId=profile_id, controllerProfileSource='user', kind='artnet_led',
                                     model=selected_profile.get('model'), role='Art-Net pixel controller')
                            d['configurationOnly']=False
                        else:
                            base = d.pop('controllerProfileBase', {})
                            d['controllerProfileId'] = None
                            d['controllerProfileSource'] = 'user-cleared'
                            d.pop('deviceProfile', None)
                            d.pop('deviceProfileSource', None)
                            for key, value in base.items():
                                if value is None:
                                    d.pop(key, None)
                                else:
                                    d[key] = value
                            # A manual clear is an explicit override of auto-detection.
                            # Keep the controller family/card type, but do not resurrect
                            # the model from cached discovery metadata on the next scan.
                            d.pop('model', None)
                    elif 'deviceProfile' in changes and changes['deviceProfile'] != 'auto' and d.get('controllerProfileId'):
                        d.pop('controllerProfileId', None)
                        d.pop('controllerProfileSource', None)
                        base = d.pop('controllerProfileBase', {})
                        for key, value in base.items():
                            if value is None:
                                d.pop(key, None)
                            else:
                                d[key] = value
                    if 'tracked' in changes:
                        d['trackedAt'] = now() if changes['tracked'] else None
                    event('Updated inventory: ' + d['name'])
                    save()
                return self.reply({'saved': True})
            if path == '/api/known-order':
                with LOCK:
                    order = data.get('order')
                    known_ids = {d['id'] for d in STATE['devices'] if d.get('tracked') is True}
                    if not isinstance(order, list) or any(not isinstance(item, str) for item in order):
                        raise ValueError('Invalid Known order')
                    if len(order) != len(set(order)) or set(order) != known_ids:
                        raise ValueError('Known order must include each saved device exactly once')
                    STATE['knownOrder'] = order
                    save()
                return self.reply({'saved': True})
            return self.reply({'error': 'Not found. Device output commands are not implemented.'}, 404)
        except (ValueError, TypeError, KeyError):
            return self.reply({'error': 'Invalid request data'}, 400)


def main():
    global PORT, STATE_FILE, HOST_IP, CONTROLLER, HTTP_SERVER
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--bind', default='0.0.0.0',
                        help='Web UI listen address (default: all local interfaces)')
    parser.add_argument('--state', type=Path, default=STATE_FILE)
    parser.add_argument('--host-ip', help='Local IPv4 address of the LAN adapter')
    parser.add_argument('--scan-on-start', action='store_true')
    parser.add_argument('--no-browser', action='store_true', help='Do not open the local node page in the default browser')
    parser.add_argument('--refresh-seconds', type=int, default=0,
                        help='Known-device full scan interval; 0 disables automatic scans (default: 0)')
    args = parser.parse_args()
    PORT, STATE_FILE = args.port, args.state
    HOST_IP = args.host_ip
    load()
    MONITOR.beam_lamp_states=STATE.setdefault('beamLampStates',{})
    import_led_registry()
    # Restore only an explicit, persisted user Blackout. With Blackout released,
    # TX is still gated until the controller confirms SOFTWARE CONTROL.
    user_blackout=STATE.get('txMuted') is True
    STATE['txMuted']=user_blackout
    save()
    MONITOR.set_tx_muted(user_blackout)
    CONTROLLER=ControllerModes(MONITOR, STATE_FILE.parent/'controller-backups', stopped=user_blackout)
    MONITOR.tx_permission=CONTROLLER.tx_allowed
    CONTROLLER.refresh()
    live_io=controller_io()
    ensure_beam230_patch(STATE,'00:00:c8:d8:a2:71',
                         live_io['inputUniverse'] if live_io else 0,
                         live_io['outputUniverse'] if live_io else 1)
    ensure_fx_profile(STATE,'00:00:c8:d8:a2:71')
    save()
    MONITOR.blackout_status=({'txMuted':True,'zeroSent':False,
                              'message':'BLACKOUT · TX stopped without a zero frame'}
                             if user_blackout else {})
    MONITOR.logger=ArtNetLog(STATE_FILE.parent/'artnet-log')
    MONITOR.logger.start()
    try:
        subnet, host_ip = network()
        MONITOR.configure(subnet, host_ip)
    except ValueError as exc:
        print('Network unavailable: %s' % exc, flush=True)
        MONITOR.configure('127.0.0.0/30', '127.0.0.1')
    if MONITOR.start():
        print('Art-Net monitor: UDP %s; routing and fixture controls require confirmed SOFTWARE CONTROL TX' % MONITOR.port, flush=True)
        for d in STATE['devices']:
            if d['kind'] == 'artnet' and valid_target(d['ip'], MONITOR.subnet):
                try:
                    MONITOR.poll(d['ip'])
                except (ValueError, OSError):
                    pass
                break
    else:
        print(MONITOR.error, flush=True)
    server = ThreadingHTTPServer((args.bind, PORT), Handler)
    HTTP_SERVER=server
    print('SiteFlow Node → http://127.0.0.1:%s · LAN http://%s:%s' % (PORT, MONITOR.host_ip, PORT), flush=True)
    restarted=bool(os.environ.pop('SITEFLOW_NODE_RESTARTED',None))
    if not args.no_browser and not restarted:
        threading.Timer(1.0, webbrowser.open, args=('http://127.0.0.1:%s/' % PORT,)).start()
    if args.scan_on_start:
        start_scan('scan')
    if args.refresh_seconds:
        def refresh_loop():
            while True:
                time.sleep(max(30, args.refresh_seconds))
                start_scan('refresh')
        threading.Thread(target=refresh_loop, daemon=True).start()
    def midi_input_loop():
        sequence=0
        while not MONITOR.stop_event.is_set():
            refresh_midi_inputs()
            sequence,events=wait_midi_events(sequence,.25)
            if MIDI_INPUTS.ingest(events):MONITOR.wake_routing()
    refresh_midi_inputs()
    threading.Thread(target=midi_input_loop,daemon=True).start()

    def routing_loop():
        """Forward the newest accepted input frame; never queue stale frames."""
        revision=0;cached_key=None;controls=[];fixtures=[];ip=None;output_universe=None;input_universe=None
        stale_deadline=None
        while not MONITOR.stop_event.is_set():
            timeout=None if stale_deadline is None else max(0,stale_deadline-time.monotonic())
            next_revision,trigger=MONITOR.wait_for_route_frame(revision,timeout)
            timed_out=next_revision==revision and stale_deadline is not None and time.monotonic()>=stale_deadline
            if next_revision==revision and not timed_out:continue
            revision=next_revision
            if timed_out:trigger=None
            with LOCK:
                d=next((d for d in STATE['devices'] if d['id']=='00:00:c8:d8:a2:71'),None)
                raw_controls=STATE.get('inputControls',{}).get('00:00:c8:d8:a2:71')
                raw_fixtures=STATE.get('fixtureMaps',{}).get('00:00:c8:d8:a2:71')
                wiz_devices=[d for d in STATE.get('devices',[]) if d.get('kind')=='wiz']
                io=controller_io()
                config_key=(id(raw_controls),id(raw_fixtures),id(STATE.get('ledFixtures')),id(STATE.get('ledCanvases')),
                            id(STATE.get('sequences',{}).get('00:00:c8:d8:a2:71')),
                            io['inputUniverse'] if io else None,io['outputUniverse'] if io else None,
                            tuple((d.get('id'),d.get('ip'),d.get('name')) for d in wiz_devices))
                ip=d['ip'] if d else None
                input_universe=io['inputUniverse'] if io else None
                output_universe=io['outputUniverse'] if io else None
                if config_key!=cached_key:
                    raw_controls=raw_controls if raw_controls is not None else initial_controls('00:00:c8:d8:a2:71')
                    raw_fixtures=raw_fixtures if raw_fixtures is not None else initial_patch('00:00:c8:d8:a2:71')
                    controls=effective_controls(raw_controls,io)
                    fixtures=effective_fixtures(raw_fixtures,io)
                    fixtures+=led_virtuals(io['outputUniverse'] if io else 1)
                    fixtures+=wiz_virtual_fixtures(wiz_devices,io['outputUniverse'] if io else 1)
                    fixtures+=sequence_virtual_fixtures(STATE.get('sequences',{}).get('00:00:c8:d8:a2:71',[]),io['outputUniverse'] if io else 1)
                    known={f['id'] for f in fixtures}
                    for control in controls:
                        control['targetFixtures']=[fid for fid in control.get('targetFixtures',[]) if fid in known]
                    cached_key=config_key
            if not ip:continue
            if MONITOR.tx_blocked():continue
            if trigger is not None:
                source_ip,source_universe,received_at=trigger
                active_sources={c['universe'] for c in controls if c.get('enabled')}
                if source_ip!=ip or source_universe not in active_sources:continue
                stale_deadline=received_at+DESK_HOLD_SECONDS
            elif timed_out:
                stale_deadline=None
            # A plain wake is caused by a control edit, a manual fader change,
            # blackout/mode transition, or the one-time worker initialization.
            MONITOR.route_tick(ip,controls,fixtures,output_universe,midi_controls=MIDI_INPUTS.routing_controls('00:00:c8:d8:a2:71'))
    MONITOR.wake_routing()
    threading.Thread(target=routing_loop,daemon=True).start()

    def fx_activation_loop():
        """Keep CH1 active and sequence CH2 state on the confirmed output universe."""
        while not MONITOR.stop_event.is_set():
            with MONITOR.lock:active=bool(MONITOR.fx_activations)
            if not active or MONITOR.tx_blocked():
                MONITOR.stop_event.wait(.1);continue
            try:
                with LOCK:
                    io=controller_io();d=next((x for x in STATE.get('devices',[]) if x.get('id')=='00:00:c8:d8:a2:71' and x.get('kind')=='artnet'),None)
                    raw_controls=STATE.get('inputControls',{}).get('00:00:c8:d8:a2:71')
                    raw_fixtures=STATE.get('fixtureMaps',{}).get('00:00:c8:d8:a2:71')
                    wiz_devices=[x for x in STATE.get('devices',[]) if x.get('kind')=='wiz']
                    controls=effective_controls(raw_controls if raw_controls is not None else initial_controls('00:00:c8:d8:a2:71'),io)
                    fixtures=effective_fixtures(raw_fixtures if raw_fixtures is not None else initial_patch('00:00:c8:d8:a2:71'),io)
                    fixtures+=led_virtuals(io['outputUniverse'] if io else 1)
                    fixtures+=wiz_virtual_fixtures(wiz_devices,io['outputUniverse'] if io else 1)
                    fixtures+=sequence_virtual_fixtures(STATE.get('sequences',{}).get('00:00:c8:d8:a2:71',[]),io['outputUniverse'] if io else 1)
                    ip=d['ip'] if d else None;universe=io['outputUniverse'] if io else None
                if ip and universe is not None:
                    MONITOR.route_tick(ip,controls,fixtures,universe,midi_controls=MIDI_INPUTS.routing_controls('00:00:c8:d8:a2:71'))
            except Exception as exc:
                with MONITOR.lock:
                    MONITOR.fx_sequence.update(error='FX output loop: '+str(exc))
            MONITOR.stop_event.wait(1/30)
    threading.Thread(target=fx_activation_loop,daemon=True).start()

    def wiz_output_loop():
        """Apply the newest selected WiZ target values off the Art-Net hot path."""
        last_sent={};retry_after={};identity_checked={}
        while not MONITOR.stop_event.is_set():
            if MONITOR.tx_blocked():
                MONITOR.stop_event.wait(.1);continue
            with MONITOR.lock:targets=copy.deepcopy(MONITOR.wiz_targets)
            with LOCK:devices=[dict(d) for d in STATE.get('devices',[]) if d.get('kind')=='wiz']
            by_virtual={'wiz-'+str(d['id']).replace(':','').lower():d for d in devices if d.get('id') and d.get('ip')}
            for target_id,values in targets.items():
                device=by_virtual.get(target_id)
                if not device or not isinstance(values,dict):continue
                roles={str(k).casefold():int(v) for k,v in values.items() if type(v) is int and 0<=v<=255}
                master=roles.get('master',0)
                red,green,blue=(roles.get(color,0) for color in ('red','green','blue'))
                white,amber,uv=(roles.get(color,0) for color in ('white','amber','uv'))
                # WiZ has RGB hardware rather than Amber/UV emitters. Approximate
                # these PAR LED functions with orange and visible violet RGB mixes.
                rgb=[max(0,min(255,round(v*master/255))) for v in (
                    red+white+amber+uv*.35,
                    green+white+amber*.42,
                    blue+white+uv,
                )]
                master_dimming=max(10,min(100,round(master*100/255)))
                params={'state':master>0,'dimming':master_dimming}
                if white>0 and not any((red,green,blue,amber,uv)):
                    params['temp']=4000
                else:params.update(dict(zip(('r','g','b'),rgb)))
                strobe=roles.get('strobe',0)
                # WiZ has no verified native strobe command. Approximate it with
                # fast dimmer pulses while keeping the lamp powered on. DMX 0/255
                # remain steady; 1..254 request the fastest local software pulse.
                strobe_hz=10.0 if 0<strobe<255 else 0.0
                strobe_on=bool(strobe_hz and int(time.monotonic()*strobe_hz*2)%2==0)
                if strobe_hz and master:
                    params['state']=True
                    params['dimming']=master_dimming if strobe_on else 10
                if params==last_sent.get(target_id) or time.monotonic()<retry_after.get(target_id,0):continue
                try:
                    if not valid_target(device['ip'],STATE.get('network',{}).get('subnet','192.168.18.0/24')):
                        raise ValueError('WiZ target is outside the current local network')
                    if identity_checked.get(target_id)!=device.get('ip'):
                        current=wiz_read(device['ip']);config=current.get('getSystemConfig',{})
                        expected=str(device.get('mac') or '').replace(':','').lower()
                        actual=str(config.get('mac') or '').replace(':','').lower()
                        if not current.get('getPilot') or (expected and actual and expected!=actual):
                            raise ValueError('WiZ is offline or its identity changed')
                        identity_checked[target_id]=device['ip']
                    # Flash edges use the acknowledged setPilot response without a
                    # second readback round-trip; ordinary color changes retain it.
                    wiz_command(device['ip'],params,readback=not bool(strobe_hz),wait_ack=not bool(strobe_hz))
                    last_sent[target_id]=params
                    MONITOR.wiz_routing_status[target_id]=dict(online=True,lastSentAt=now(),error=None,
                                                              mode='WiZ dimmer pulse · 10 Hz target' if strobe_hz else 'PAR LED RGB emulation',
                                                              strobeValue=strobe,strobeHz=strobe_hz,
                                                              dimming=params.get('dimming'))
                except (OSError,ValueError) as exc:
                    retry_after[target_id]=time.monotonic()+1.0
                    MONITOR.wiz_routing_status[target_id]=dict(online=False,error=str(exc),checkedAt=now())
            MONITOR.stop_event.wait(.05)
    threading.Thread(target=wiz_output_loop,daemon=True).start()

    def led_output_loop():
        """Keep LED rendering on its own cadence so it cannot delay DMX routing."""
        cached_key=None;leds=[];runtime_controls=[];ip=None;canvases=[];last_locator=None;output_ranges=[];output_orders=['BRG']*4;output_ip=None
        while not MONITOR.stop_event.is_set():
            with LOCK:
                raw_leds=STATE.get('ledFixtures',[])
                raw_canvases=STATE.get('ledCanvases',[])
                raw_controls=STATE.get('inputControls',{}).get('00:00:c8:d8:a2:71')
                io=controller_io()
                d=next((d for d in STATE['devices'] if d['id']=='00:00:c8:d8:a2:71'),None)
                ip=d['ip'] if d else None
                current_controllers=tuple(sorted((str(d.get('mac','')).lower(),d.get('ip'),d.get('kind'),tuple(tuple(row) for row in d.get('artnetOutputRanges',[]) if isinstance(row,list))) for d in STATE['devices'] if d.get('mac')))
                key=(id(raw_leds),id(raw_canvases),id(raw_controls),io['inputUniverse'] if io else None,current_controllers)
                if key!=cached_key:
                    mac_devices={mac_normalize(d['mac']):d for d in STATE['devices'] if d.get('mac')}
                    copied=json.loads(json.dumps(raw_leds));leds=[]
                    for fixture in copied:
                        identity=fixture.get('controllerId')
                        if identity:
                            bound=mac_devices.get(identity)
                            if not bound:continue
                            fixture['controllerIp']=bound['ip']
                        leds.append(fixture)
                    canvases=json.loads(json.dumps(raw_canvases))
                    for canvas in canvases:
                        bound=mac_devices.get(canvas.get('controllerId'))
                        if bound:canvas['controllerIp']=bound['ip']
                    pixel_controller=mac_devices.get('20:e7:c8:f6:68:80')
                    output_ip=pixel_controller.get('ip') if pixel_controller else None
                    output_ranges=json.loads(json.dumps(pixel_controller.get('artnetOutputRanges',[]))) if pixel_controller else []
                    base_order=next((f.get('order','BRG') for f in leds if f.get('controllerId')=='20:e7:c8:f6:68:80'),'BRG')
                    output_orders=[next((c.get('colorOrder',base_order) for c in canvases if c.get('controllerId')=='20:e7:c8:f6:68:80' and c.get('port')==port),base_order) for port in range(1,5)]
                    runtime_controls=effective_controls(raw_controls or initial_controls('00:00:c8:d8:a2:71'),io)
                    cached_key=key
            if ip or output_ip or LED.locator or LED.output_test:
                if MONITOR.tx_blocked():
                    with LOCK:LED.locator=None;LED.output_test=None;LED.output_map=None
                    last_locator=None
                    LED.status=dict(active=False,message='BLACKOUT · TX stopped')
                else:
                    try:
                        with LOCK:
                            output_test=copy.deepcopy(LED.output_test)
                            locator=copy.deepcopy(LED.locator)
                            if locator and time.monotonic()-locator['updated']>1.5:
                                LED.locator=None;locator=None
                        if output_test:
                            if output_test.get('mode')=='canvas-sweep':
                                strips=output_test['addresses'];step_seconds=output_test.get('stepSeconds',.12);step=int(output_test.get('step',0));total=len(strips)
                                wait_until=float(output_test.get('nextStepAt',output_test['started']))
                                remaining=wait_until-time.monotonic()
                                if remaining>0:
                                    MONITOR.stop_event.wait(min(remaining,.04))
                                    continue
                                values=LED.values(leds,runtime_controls,MONITOR,ip,midi_controls=MIDI_INPUTS.routing_controls('00:00:c8:d8:a2:71'),canvases=canvases)
                                base_frames=controller_frames(leds,values,time.monotonic(),LED.render_modes,canvases)
                                canvas=output_test['canvas']
                                if step>=total:
                                    frames=canvas_sweep_frame(base_frames,output_test['ip'],output_test['ranges'],canvas,[],'off')
                                    frames=complete_controller_frames(frames,output_test['ip'],output_test['ranges'])
                                    if MONITOR.send_pixel_frames(frames,int(time.monotonic()*25)%255+1):
                                        LED.output_map=dict(ports=output_map_from_frames(output_test['ip'],output_test['ranges'],output_test['orders'],frames),source='Canvas sweep · complete',sentAt=now())
                                        with LOCK:LED.output_test=None
                                        LED.status=dict(active=False,message='Canvas sweep complete · top-down, then left-right')
                                    else:
                                        with LOCK:LED.output_test=None
                                        LED.status=dict(active=False,error='Canvas sweep stopped · Art-Net TX is unavailable')
                                else:
                                    strip_state=strips[step];strip=strip_state['addresses'];pass_index=strip_state['passIndex'];color='white' if step%2==0 else 'red'
                                    frames=canvas_sweep_frame(base_frames,output_test['ip'],output_test['ranges'],canvas,strip,color)
                                    frames=complete_controller_frames(frames,output_test['ip'],output_test['ranges'])
                                    if MONITOR.send_pixel_frames(frames,int(time.monotonic()*25)%255+1):
                                        LED.output_map=dict(ports=output_map_from_frames(output_test['ip'],output_test['ranges'],output_test['orders'],frames),source=f'Canvas sweep · {color.upper()}',sentAt=now())
                                        LED.status=dict(active=True,message=f'Canvas sweep · {"top-down" if pass_index==0 else "left-right"} · {strip_state["step"]}/{strip_state["total"]}',
                                                        controllers=[output_test['ip']],lastSentAt=now(),canvasTest=dict(canvasId=canvas['id'],address=strip[0] if strip else canvas['startPixel'],addresses=strip,color=color,passIndex=pass_index,step=step+1,total=total))
                                        with LOCK:
                                            if LED.output_test and LED.output_test.get('canvas',{}).get('id')==canvas['id']:
                                                LED.output_test['step']=step+1
                                                LED.output_test['nextStepAt']=time.monotonic()+step_seconds
                                    else:
                                        with LOCK:LED.output_test=None
                                        LED.status=dict(active=False,error='Canvas sweep stopped · Art-Net TX is unavailable')
                                last_locator=None
                                MONITOR.stop_event.wait(.04)
                                continue
                            elapsed=time.monotonic()-output_test['started']
                            color='white' if elapsed<1 else 'red' if elapsed<2 else 'off'
                            if elapsed>=2.2:
                                base_values=LED.values(leds,runtime_controls,MONITOR,ip,midi_controls=MIDI_INPUTS.routing_controls('00:00:c8:d8:a2:71'),canvases=canvases)
                                frames={key:value for key,value in controller_frames(leds,base_values,time.monotonic(),LED.render_modes,canvases).items() if key[0]!=output_test['ip']}
                                frames.update(output_address_test_frames(output_test['ip'],output_test['ranges'],output_test['orders'],'off'))
                                if MONITOR.send_pixel_frames(frames,int(time.monotonic()*25)%255+1):
                                    LED.output_map=dict(ports=output_map_from_frames(output_test['ip'],output_test['ranges'],output_test['orders'],frames),source='Output test · OFF',sentAt=now())
                                    with LOCK:LED.output_test=None
                                    LED.status=dict(active=False,message='Output test finished · white 1 s · red 1 s · off')
                                else:
                                    with LOCK:LED.output_test=None
                                    LED.output_map=None
                                    LED.status=dict(active=False,message='Output test stopped · TX became unavailable before OFF was sent')
                            else:
                                base_values=LED.values(leds,runtime_controls,MONITOR,ip,midi_controls=MIDI_INPUTS.routing_controls('00:00:c8:d8:a2:71'),canvases=canvases)
                                frames={key:value for key,value in controller_frames(leds,base_values,time.monotonic(),LED.render_modes,canvases).items() if key[0]!=output_test['ip']}
                                frames.update(output_address_test_frames(output_test['ip'],output_test['ranges'],output_test['orders'],color))
                                if MONITOR.send_pixel_frames(frames,int(time.monotonic()*25)%255+1):
                                    LED.output_map=dict(ports=output_map_from_frames(output_test['ip'],output_test['ranges'],output_test['orders'],frames),source='Output test · '+color.upper(),sentAt=now())
                                    LED.status=dict(active=color!='off',message=f'Output test · {color.upper()}',controllers=[output_test['ip']],lastSentAt=now())
                            last_locator=None
                            MONITOR.stop_event.wait(.04)
                            continue
                        if locator:
                            values=LED.values(leds,runtime_controls,MONITOR,ip,midi_controls=MIDI_INPUTS.routing_controls('00:00:c8:d8:a2:71'),canvases=canvases)
                            frames=merge_pixel_frames_htp(controller_frames(leds,values,time.monotonic(),LED.render_modes,canvases),locator_frames(locator,last_locator))
                            frames=complete_controller_frames(frames,locator['ip'],output_ranges)
                            if MONITOR.send_pixel_frames(frames,int(time.monotonic()*25)%255+1):
                                if output_ranges and output_ip==locator['ip']:
                                    LED.output_map=dict(ports=output_map_from_frames(locator['ip'],output_ranges,output_orders,frames),source='Canvas + Address locator',sentAt=now())
                                LED.status=dict(active=True,message=f'Address locator · P{locator["port"]} · U{locator["firstUniverse"]} · pixel {locator["pixel"]}',
                                                controllers=[locator['ip']],lastSentAt=now())
                                LED.last_active={f['controllerIp'] for f in leds if f['id'] in values}
                            last_locator=locator
                            MONITOR.stop_event.wait(.04)
                            continue
                        values=LED.values(leds,runtime_controls,MONITOR,ip,midi_controls=MIDI_INPUTS.routing_controls('00:00:c8:d8:a2:71'),canvases=canvases)
                        active_controllers={f['controllerIp'] for f in leds if f['id'] in values}
                        retired=LED.last_active-active_controllers
                        send_values=dict(values)
                        for fixture in leds:
                            if fixture['controllerIp'] in retired:send_values[fixture['id']]=[0]*len(fixture['mapping'])
                        frames=controller_frames(leds,send_values,time.monotonic(),LED.render_modes,canvases)
                        if last_locator:
                            frames=merge_pixel_frames_htp(frames,locator_frames(None,last_locator))
                            last_locator=None
                        frames=complete_controller_frames(frames,output_ip,output_ranges)
                        if frames and MONITOR.send_pixel_frames(frames,int(time.monotonic()*25)%255+1):
                            if output_ranges and output_ip:
                                LED.output_map=dict(ports=output_map_from_frames(output_ip,output_ranges,output_orders,frames),source='Canvas / virtual control',sentAt=now())
                            LED.status=dict(active=True,controllers=sorted(active_controllers),lastSentAt=now())
                            LED.last_active=active_controllers
                        elif MONITOR.tx_muted:LED.status=dict(active=False,message='Blackout · TX stopped');LED.output_map=None
                        else:LED.status=dict(active=False,message='No Canvas output configured' if not canvases else 'Waiting for LED control');LED.output_map=None
                    except (ValueError,OSError) as exc:LED.status=dict(active=False,error=str(exc))
            MONITOR.stop_event.wait(.04)
    threading.Thread(target=led_output_loop,daemon=True).start()
    def controller_mode_loop():
        while not MONITOR.stop_event.wait(30 if CONTROLLER.state.get('connection')=='ONLINE' else 5):CONTROLLER.refresh()
    threading.Thread(target=controller_mode_loop,daemon=True).start()
    def artnet_status_loop():
        index=0
        while True:
            time.sleep(2.5)
            with LOCK:
                targets=[d['ip'] for d in STATE['devices'] if d['kind'] in ('artnet','artnet_led')]
            selected=targets[index%len(targets):index%len(targets)+1] if targets else []
            index+=1
            for target in selected:
                try:MONITOR.poll(target)
                except (ValueError,OSError):pass
            with MONITOR.lock:
                replies={ip:copy.deepcopy(node) for ip,node in MONITOR.nodes.items()
                         if time.monotonic()-node.get('_last',0)<30}
            changed=False
            with LOCK:
                for device in STATE['devices']:
                    if apply_sp801e_artpoll_profile(device,replies.get(device.get('ip'))):changed=True
                if changed:save()
    threading.Thread(target=artnet_status_loop,daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        MONITOR.close()
        if MONITOR.logger:MONITOR.logger.close()
        server.server_close()
    if RESTART_REQUESTED:
        os.environ['SITEFLOW_NODE_RESTARTED']='1'
        script=str(Path(__file__).resolve())
        os.execv(sys.executable,[sys.executable,script,*sys.argv[1:]])


if __name__ == '__main__':
    main()
