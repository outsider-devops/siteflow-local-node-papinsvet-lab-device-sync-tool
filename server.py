#!/usr/bin/env python3
"""SiteFlow local inventory and Art-Net monitor. Only explicit mapped-fixture sequence tests."""
from fixtures import initial_patch, validate_fixture, PRESETS, prepare_fixture_update
from input_controls import initial_controls, validate_control, FADER_FUNCTIONS
from led_controls import defaults as led_defaults,virtual_fixture,validate_mapping,controller_frames,LEDRuntime
import argparse
import concurrent.futures
import datetime as dt
import http.client
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
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from artnet_monitor import ArtNetMonitor
from artnet_log import ArtNetLog
from controller_modes import ControllerModes

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


def import_led_registry():
    STATE.setdefault('ledFixtures',led_defaults())
    for ip,name in [('192.168.18.3','LED Strips · P1–P4'),('192.168.18.157','Vertical LED Matrix')]:
        d=next((d for d in STATE['devices'] if d['ip']==ip),None)
        if not d:
            d=identify(ip,'');d['name']=name;STATE['devices'].append(d)
        if d['name'] in ('LED ArtNet','Device 3'):d['name']=name
        d.update(kind='artnet_led',role='Art-Net pixel controller',model='SP801E profile · hardware unverified',tracked=True)
        d['nextStep']='Imported from rc6. Use LED Controls; output remains off until enabled or assigned to an input.'
    save()

def led_virtuals():return [virtual_fixture(f) for f in STATE.get('ledFixtures',[])]

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
    return dict(id=mac or 'ip-' + ip, ip=ip, mac=mac, manufacturer=vendor,
                name='Device ' + ip.rsplit('.', 1)[1], kind=kind,
                model='', firmware='', vendorId='', role='Unidentified device',
                zone='', notes='', tracked=False, confidence='Vendor hint',
                privateMac=private_mac, reachability='Unknown', access='Unverified',
                ports=[], webUrl=None, lastSeen=None, checkedAt=None,
                evidence=['MAC vendor identifies a module, not a finished product.'],
                nextStep='Match the MAC address to a physical device or its vendor app.',
                addedAt=now())


def save():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix('.tmp')
    temp.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(str(temp), str(STATE_FILE))


def event(message, level='info'):
    STATE['events'].insert(0, dict(time=now(), message=message, level=level))
    del STATE['events'][100:]


def load():
    global STATE
    if STATE_FILE.exists():
        STATE = json.loads(STATE_FILE.read_text(encoding='utf-8'))
        STATE['scan']['running'] = False
        STATE['scan']['message'] = 'Ready for a fresh check'
        for d in STATE['devices']:
            if d.get('reachability') == 'Online':
                d['reachability'] = 'Stale'
        return
    seeded = json.loads((ROOT / 'seed.json').read_text(encoding='utf-8'))
    STATE = dict(devices=seeded, events=[], scan=dict(running=False, completed=0,
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
    for method in ('getSystemConfig', 'getPilot'):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1.3)
            sock.sendto(json.dumps({'id': 1, 'method': method, 'params': {}}).encode(), (ip, 38899))
            try:
                data, sender = sock.recvfrom(8192)
                reply = json.loads(data)
                if sender[0] == ip and reply.get('method') == method and isinstance(reply.get('result'), dict):
                    # Retain only device-level metadata and state, not home / location identifiers.
                    results[method] = {k: v for k, v in reply['result'].items()
                                       if k in ('mac', 'moduleName', 'fwVersion', 'state', 'dimming', 'temp', 'r', 'g', 'b', 'sceneId')}
            except (OSError, ValueError, TypeError):
                pass
    return results


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
            for field in ('name', 'zone', 'notes', 'tracked', 'role'):
                result[field] = current.get(field, result.get(field))
            current.update(result)
            if result.get('kind')=='artnet' and result.get('transportMac'):
                STATE['devices'][:]=[d for d in STATE['devices'] if d is current or not (d['ip']==current['ip'] and d.get('mac')==result['transportMac'] and d.get('kind')=='artnet')]


def scan(mode='scan', device_id=None):
    try:
        subnet, host_ip = network()
        MONITOR.configure(subnet, host_ip)
        with LOCK:
            STATE['network'] = dict(subnet=subnet, interface=NETWORK_INTERFACE, hostIp=host_ip)
            STATE['scan']['message'] = 'Discovering devices on ' + subnet if mode == 'scan' else 'Checking saved devices'
        live = set()
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
            with LOCK:
                for ip, mac in found.items():
                    existing = next((d for d in STATE['devices'] if d['mac'] == mac), None)
                    if not existing:
                        existing = next((d for d in STATE['devices'] if d['ip']==ip and d.get('kind') in ('artnet','artnet_led')),None)
                    if existing:
                        existing['ip'] = ip
                        existing['neighborObservedAt'] = now()
                    else:
                        d = identify(ip, mac)
                        d['neighborObservedAt'] = now()
                        STATE['devices'].append(d)
                        event('Discovered ' + ip + ' · ' + d['manufacturer'])
                for ip in live:
                    if valid_target(ip, subnet) and not any(d['ip'] == ip for d in STATE['devices']):
                        STATE['devices'].append(identify(ip, ''))
        with LOCK:
            targets = [dict(d) for d in STATE['devices'] if device_id is None or d['id'] == device_id]
            # A saved IP now owned by a different MAC must never be probed as the old identity.
            current_neighbors = neighbors(subnet)
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
                    d.update(reachability='Stale', access='Identity conflict', webUrl=None, ports=[],
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
            event(('Network scan' if mode == 'scan' else 'Status check') + ' completed. No output commands were sent.')
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
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
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
                return self.reply({'fixtures':STATE.get('ledFixtures',[]),'virtualFixtures':led_virtuals(),'runtime':LED.manual,'effectiveValues':LED.last_values,'status':LED.status,'txMuted':MONITOR.tx_muted})
        if path == '/api/input-controls':
            device_id=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('device',[''])[0]
            with LOCK:
                if not any(d['id']==device_id and d['kind']=='artnet' for d in STATE['devices']):return self.reply({'error':'Art-Net device not found'},404)
                return self.reply({'controls':STATE.get('inputControls',{}).get(device_id,initial_controls(device_id)), 'transmissionEnabled':True,'faderFunctions':FADER_FUNCTIONS})
        if path == '/api/fixtures':
            device_id=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('device',[''])[0]
            with LOCK:
                if not any(d['id']==device_id and d['kind']=='artnet' for d in STATE['devices']):return self.reply({'error':'Art-Net device not found'},404)
                return self.reply({'fixtures':STATE.get('fixtureMaps',{}).get(device_id,initial_patch(device_id)), 'presets':STATE.get('fixturePresets',PRESETS)})
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
                if d.get('reachability') == 'Online' and d.get('lastSeen'):
                    age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(d['lastSeen'])).total_seconds()
                    if age > 180:
                        d['reachability'] = 'Stale'
            snapshot.update(serverTime=now(), csrf=TOKEN, version='0.3.0', outputControl='mapped-fixture-tests-and-input6-forwarding')
            return self.reply(snapshot)
        if path == '/api/export':
            with LOCK:
                result = dict(exportedAt=now(), network=STATE['network'], devices=STATE['devices'])
                return self.reply(result, extra={'Content-Disposition': 'attachment; filename="siteflow-devices.json"'})
        files = {'/led-controls': ('led-controls.html','text/html; charset=utf-8'),
                 '/led-controls.js': ('led-controls.js','text/javascript; charset=utf-8'),
                 '/input-controls': ('input-controls.html','text/html; charset=utf-8'),
                 '/input-controls.js': ('input-controls.js','text/javascript; charset=utf-8'),
                 '/fixtures': ('fixtures.html','text/html; charset=utf-8'),
                 '/fixtures.js': ('fixtures.js','text/javascript; charset=utf-8'),
                 '/': ('index.html', 'text/html; charset=utf-8'),
                 '/Papinsvet-logo.png': ('Papinsvet-logo.png', 'image/png'),
                 '/artnet': ('artnet.html', 'text/html; charset=utf-8'),
                 '/monitor-page.js': ('monitor-page.js', 'text/javascript; charset=utf-8'),
                 '/app.css': ('app.css', 'text/css; charset=utf-8'),
                 '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                 '/artnet.js': ('artnet.js', 'text/javascript; charset=utf-8')}
        if path not in files:
            return self.reply({'error': 'Not found'}, 404)
        filename, mime = files[path]
        return self.reply((ROOT / 'static' / filename).read_bytes(), content_type=mime)

    def do_POST(self):
        if not self.allowed_origin():
            return self.reply({'error': 'Same-origin local requests only'}, 403)
        if self.headers.get('X-SiteFlow-Token') != TOKEN:
            return self.reply({'error': 'Invalid request token'}, 403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length > (131072 if urllib.parse.urlsplit(self.path).path in ('/api/fixtures','/api/input-controls') else 8192) or length < 0:
                return self.reply({'error': 'Request too large'}, 413)
            data = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(data, dict):
                raise ValueError('Invalid request')
            path = urllib.parse.urlsplit(self.path).path
            if path == '/api/artnet/mode':
                if CONTROLLER is None:return self.reply({'error':'Controller mode service unavailable'},503)
                try:result=CONTROLLER.switch(data.get('mode'))
                except ValueError as exc:
                    with LOCK:STATE['txMuted']=True;save()
                    return self.reply({'error':str(exc),'controllerMode':CONTROLLER.snapshot()},409)
                with LOCK:
                    STATE['txMuted']=MONITOR.tx_muted;save()
                return self.reply(result)
            if path == '/api/led/mapping':
                with LOCK:
                    items=STATE.get('ledFixtures',[]);old=next((f for f in items if f['id']==data.get('id')),None)
                    if not old:raise ValueError('LED fixture not found')
                    new=validate_mapping(data,old)
                    candidate=[new if f['id']==old['id'] else f for f in items]
                    virtuals=[virtual_fixture(f) for f in candidate]
                    controls=json.loads(json.dumps(STATE.get('inputControls',{})))
                    for did,bank in controls.items():
                        patch=STATE.get('fixtureMaps',{}).get(did,initial_patch(did))+virtuals
                        controls[did]=[validate_control(c,bank,patch) for c in bank]
                    STATE.update(ledFixtures=candidate,inputControls=controls);LED.manual.pop(old['id'],None);save()
                    return self.reply({'fixtures':candidate})
            if path == '/api/led/control':
                with LOCK:
                    f=next((f for f in STATE.get('ledFixtures',[]) if f['id']==data.get('id')),None)
                    if not f:raise ValueError('LED fixture not found')
                    if MONITOR.tx_muted:return self.reply({'error':'Blackout · resume TX first'},409)
                    LED.set_values(f,data.get('values'),data.get('enabled'))
                    return self.reply({'accepted':True})
            if path == '/api/input-controls':
                with LOCK:
                    device_id=data.get('deviceId')
                    if not any(d['id']==device_id and d['kind']=='artnet' for d in STATE['devices']):return self.reply({'error':'Art-Net device not found'},404)
                    maps=STATE.setdefault('inputControls',{})
                    items=maps.get(device_id,initial_controls(device_id))
                    fixtures=STATE.get('fixtureMaps',{}).get(device_id,initial_patch(device_id))+led_virtuals()
                    control=validate_control(data.get('control'),items,fixtures)
                    maps[device_id]=[control if c['id']==control['id'] else c for c in items] if any(c['id']==control['id'] for c in items) else items+[control]
                    save()
                    return self.reply({'controls':maps[device_id],'transmissionEnabled':True,'faderFunctions':FADER_FUNCTIONS})
            if path == '/api/fixtures':
                device_id=data.get('deviceId')
                with LOCK:
                    if not any(d['id']==device_id and d['kind']=='artnet' for d in STATE['devices']):return self.reply({'error':'Art-Net device not found'},404)
                    maps,presets,controls,affected=prepare_fixture_update(STATE,device_id,data.get('fixture'))
                    STATE.update(fixtureMaps=maps,fixturePresets=presets,inputControls=controls)
                    save()
                    return self.reply({'fixtures':maps[device_id],'presets':presets,'affectedFixtures':affected})
            if path == '/api/input-controls/fader':
                with LOCK:
                    device_id=data.get('deviceId')
                    d=next((d for d in STATE['devices'] if d['id']==device_id and d['kind']=='artnet'),None)
                    control=next((c for c in STATE.get('inputControls',{}).get(device_id,[]) if c['id']==data.get('controlId')),None)
                    if not d or not control:return self.reply({'error':'Input control not found'},404)
                    ip=d['ip'];control=json.loads(json.dumps(control))
                try:override=MONITOR.set_manual_fader(ip,control,data.get('fader'),data.get('value'),data.get('expectedInput'),expected_offline=data.get('expectedInput') is None)
                except ValueError as exc:return self.reply({'error':str(exc)},409)
                return self.reply({'override':override})
            if path == '/api/artnet/blackout':
                enabled=data.get('enabled')
                if type(enabled) is not bool:raise ValueError('Invalid blackout state')
                if CONTROLLER is not None:
                    try:
                        if enabled:CONTROLLER.stop()
                        else:CONTROLLER.resume()
                    except ValueError as exc:return self.reply({'error':str(exc)},409)
                    result={'txMuted':MONITOR.tx_muted,'zeroSent':False,
                            'message':'BLACKOUT · TX stopped without a zero frame' if MONITOR.tx_muted else 'BLACKOUT released'}
                    MONITOR.blackout_status=result
                    with LOCK:
                        STATE['txMuted']=MONITOR.tx_muted;STATE['blackoutStatus']=result;save()
                    return self.reply(result)
                if enabled:
                    with LOCK:
                        device=next((d for d in STATE['devices'] if d['kind']=='artnet'),None)
                    if not device:raise ValueError('Art-Net device not found')
                    result=MONITOR.blackout(device['ip'],controller_frames(STATE.get('ledFixtures',[]),{f['id']:[0]*len(f['mapping']) for f in STATE.get('ledFixtures',[])},0))
                else:
                    MONITOR.set_tx_muted(False)
                    result={'txMuted':False}
                with LOCK:
                    STATE['txMuted']=enabled
                    if enabled:STATE['blackoutStatus']=result
                    save()
                return self.reply(result,409 if result.get('error') else 200)
            if path == '/api/fixtures/test':
                with LOCK:
                    device_id=data.get('deviceId')
                    d=next((d for d in STATE['devices'] if d['id']==device_id and d['kind']=='artnet'),None)
                    if not d:return self.reply({'error':'Art-Net device not found'},404)
                    items=json.loads(json.dumps(STATE.get('fixtureMaps',{}).get(device_id,initial_patch(device_id))))
                    presets=json.loads(json.dumps(STATE.get('fixturePresets',PRESETS)))
                    target=d['ip']
                try:result=MONITOR.start_fixture_test(target,items,presets)
                except ValueError as exc:return self.reply({'error':str(exc)},409)
                return self.reply({'test':result},202)
            if path == '/api/artnet/poll':
                with LOCK:
                    d = next((d for d in STATE['devices'] if d['id'] == data.get('deviceId') and d['kind'] == 'artnet'), None)
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
                    if not isinstance(changes, dict) or any(k not in ('name', 'zone', 'notes', 'role', 'tracked') for k in changes):
                        raise ValueError('Only inventory labels and tracking can be edited')
                    for k, v in changes.items():
                        if k == 'tracked':
                            if not isinstance(v, bool):
                                raise ValueError('Invalid tracking value')
                        elif not isinstance(v, str) or len(v) > (1000 if k == 'notes' else 120):
                            raise ValueError('Invalid label')
                        if k == 'name' and not v.strip():
                            raise ValueError('A device name is required')
                    d.update({k: v.strip() if isinstance(v, str) else v for k, v in changes.items()})
                    event('Updated inventory: ' + d['name'])
                    save()
                return self.reply({'saved': True})
            return self.reply({'error': 'Not found. Device output commands are not implemented.'}, 404)
        except (ValueError, TypeError, KeyError):
            return self.reply({'error': 'Invalid request data'}, 400)


def main():
    global PORT, STATE_FILE, HOST_IP, CONTROLLER
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--bind', default='0.0.0.0',
                        help='Web UI listen address (default: all local interfaces)')
    parser.add_argument('--state', type=Path, default=STATE_FILE)
    parser.add_argument('--host-ip', help='Local IPv4 address of the LAN adapter')
    parser.add_argument('--scan-on-start', action='store_true')
    parser.add_argument('--refresh-seconds', type=int, default=0,
                        help='Known-device full scan interval; 0 disables automatic scans (default: 0)')
    args = parser.parse_args()
    PORT, STATE_FILE = args.port, args.state
    HOST_IP = args.host_ip
    load()
    import_led_registry()
    MONITOR.set_tx_muted(True)
    CONTROLLER=ControllerModes(MONITOR, STATE_FILE.parent/'controller-backups', stopped=bool(STATE.get('txMuted',False)))
    MONITOR.tx_permission=CONTROLLER.tx_allowed
    CONTROLLER.refresh()
    MONITOR.blackout_status=({'txMuted':True,'zeroSent':False,
                              'message':'BLACKOUT · TX stopped without a zero frame'}
                             if MONITOR.tx_muted else {})
    MONITOR.logger=ArtNetLog(STATE_FILE.parent/'artnet-log')
    MONITOR.logger.start()
    try:
        subnet, host_ip = network()
        MONITOR.configure(subnet, host_ip)
    except ValueError as exc:
        print('Network unavailable: %s' % exc, flush=True)
        MONITOR.configure('127.0.0.0/30', '127.0.0.1')
    if MONITOR.start():
        print('Art-Net monitor: UDP %s; output only on an explicit mapped-fixture Test request' % MONITOR.port, flush=True)
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
    print('SiteFlow Node → http://127.0.0.1:%s · LAN http://%s:%s' % (PORT, MONITOR.host_ip, PORT), flush=True)
    if args.scan_on_start:
        start_scan('scan')
    if args.refresh_seconds:
        def refresh_loop():
            while True:
                time.sleep(max(30, args.refresh_seconds))
                start_scan('refresh')
        threading.Thread(target=refresh_loop, daemon=True).start()
    def routing_loop():
        while not MONITOR.stop_event.is_set():
            with LOCK:
                d=next((d for d in STATE['devices'] if d['id']=='00:00:c8:d8:a2:71'),None)
                controls=STATE.get('inputControls',{}).get('00:00:c8:d8:a2:71',[])
                fixtures=STATE.get('fixtureMaps',{}).get('00:00:c8:d8:a2:71',[])
                controls,fixtures=json.loads(json.dumps([controls,fixtures]))
                ip=d['ip'] if d else None
            if ip:
                physical_controls=json.loads(json.dumps(controls))
                known={f['id'] for f in fixtures}
                for c in physical_controls:c['targetFixtures']=[fid for fid in c.get('targetFixtures',[]) if fid in known]
                MONITOR.route_tick(ip,physical_controls,fixtures)
                if MONITOR.tx_blocked():
                    LED.status=dict(active=False,message='BLACKOUT · TX stopped')
                    MONITOR.stop_event.wait(.04)
                    continue
                with LOCK:
                    leds=json.loads(json.dumps(STATE.get('ledFixtures',[])))
                    try:
                        values=LED.values(leds,controls,MONITOR,ip)
                        # A disabled channel on an otherwise active controller must go dark.
                        active_controllers={f['controllerIp'] for f in leds if f['id'] in values}
                        retired=LED.last_active-active_controllers
                        send_values=dict(values)
                        for f in leds:
                            if f['controllerIp'] in retired:send_values[f['id']]=[0]*len(f['mapping'])
                        frames=controller_frames(leds,send_values,time.monotonic())
                        if frames and MONITOR.send_pixel_frames(frames,int(time.monotonic()*25)%255+1):
                            LED.status=dict(active=True,controllers=sorted(active_controllers),lastSentAt=now())
                            LED.last_active=active_controllers
                        elif MONITOR.tx_muted:LED.status=dict(active=False,message='Blackout · TX stopped')
                        else:LED.status=dict(active=False,message='Output disabled')
                    except (ValueError,OSError) as exc:LED.status=dict(active=False,error=str(exc))
            MONITOR.stop_event.wait(.04)
    threading.Thread(target=routing_loop,daemon=True).start()
    def controller_mode_loop():
        while not MONITOR.stop_event.wait(30):CONTROLLER.refresh()
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
    threading.Thread(target=artnet_status_loop,daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        MONITOR.close()
        if MONITOR.logger:MONITOR.logger.close()
        server.server_close()


if __name__ == '__main__':
    main()
