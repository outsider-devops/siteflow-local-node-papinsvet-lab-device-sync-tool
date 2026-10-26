"""Bounded Art-Net receiver with discovery and explicit mapped-fixture sequence test."""
from collections import deque
from fixtures import fixture_test_plan, fixture_test_frame
from input_controls import semantic_rules, routing_mode
import datetime as dt
import ipaddress
import socket
import struct
import threading
import time

ARTNET_ID = b'Art-Net\x00'
ARTPOLL = ARTNET_ID + struct.pack('<H', 0x2000) + b'\x00\x0e\x00\x00'


def receiver_reply(host_ip, universes):
    """Advertise a virtual visualizer subscription; never emits ArtDmx."""
    universes=sorted(set(universes))[:4]
    if not universes:
        universes=[0,1]
    base=universes[0] & 0x7ff0
    universes=[u for u in universes if (u & 0x7ff0)==base]
    data=bytearray(239)
    data[:10]=ARTNET_ID+struct.pack('<H',0x2100)
    data[10:14]=socket.inet_aton(host_ip)
    data[14:16]=struct.pack('<H',6454)
    data[16:18]=b'\x00\x01'
    data[18],data[19]=(base>>8)&127,(base>>4)&15
    data[20:22]=b'\xff\xff'
    short=b'SiteFlow RX';long=b'SiteFlow receive-only virtual visualizer'
    data[26:26+len(short)]=short;data[44:44+len(long)]=long
    report=b'#0001 [0000] RX monitor; no physical DMX output'
    data[108:108+len(report)]=report
    data[172:174]=struct.pack('>H',len(universes))
    for i,u in enumerate(universes):
        data[174+i]=128
        data[190+i]=u&15
    data[200]=6  # StVisual, not a physical DMX gateway.
    data[207:211]=socket.inet_aton(host_ip)
    data[211]=1
    return bytes(data)


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse_packet(data):
    if len(data) < 10 or data[:8] != ARTNET_ID:
        raise ValueError('Invalid Art-Net header')
    opcode = int.from_bytes(data[8:10], 'little')
    if opcode == 0x5000:
        if len(data) < 18 or int.from_bytes(data[10:12], 'big') < 14:
            raise ValueError('Invalid ArtDmx version or header')
        length = int.from_bytes(data[16:18], 'big')
        if not 2 <= length <= 512 or length % 2 or len(data) < 18 + length or data[15] > 127:
            raise ValueError('Invalid ArtDmx length or Port-Address')
        return dict(type='ArtDmx', universe=(data[15] << 8) | data[14],
                    net=data[15], subnet=data[14] >> 4, universeNibble=data[14] & 15,
                    sequence=data[12], physical=data[13], values=list(data[18:18 + length]))
    if opcode == 0x2100:
        if len(data) < 194:
            raise ValueError('Truncated ArtPollReply')
        text = lambda a, b: data[a:b].split(b'\x00', 1)[0].decode('utf-8', errors='replace')
        base = ((data[18] & 127) << 8) | ((data[19] & 15) << 4)
        count = min(int.from_bytes(data[172:174], 'big'), 4)
        ports = []
        for i in range(count):
            flags = data[174 + i]
            ports.append(dict(port=i + 1, input=bool(flags & 64), output=bool(flags & 128),
                              protocol=flags & 63, inputUniverse=base | (data[186 + i] & 15),
                              outputUniverse=base | (data[190 + i] & 15),
                              inputData=bool(data[178 + i] & 128),
                              inputDisabled=bool(data[178 + i] & 8),
                              outputData=bool(data[182 + i] & 128)))
        return dict(type='ArtPollReply', shortName=text(26, 44), longName=text(44, 108),
                    report=text(108, 172), firmwareCode=int.from_bytes(data[16:18], 'big'),
                    ports=ports)
    return dict(type='Other', opcode='0x%04x' % opcode)


class ArtNetMonitor:
    def __init__(self, subnet='192.168.18.0/24', host_ip='192.168.18.6', port=6454, logger=None):
        self.lock = threading.RLock()
        self.subnet, self.host_ip, self.port = subnet, host_ip, port
        self.socket = None
        self.running = False
        self.error = None
        self.streams, self.nodes = {}, {}
        self.events = deque(maxlen=80)
        self.counters = dict(artDmx=0, pollReplies=0, other=0, malformed=0, ignored=0, evictedStreams=0)
        self.started = None
        self.stop_event = threading.Event()
        self.last_poll = float('-inf')
        self.logger=logger
        self.subscriptions={}
        self.input_universe=1
        self.tool_tx_by_ip={}
        self.blackout_status={}
        self.manual_overrides={}
        self.tx_muted=False
        self.tx_permission=None
        self.test_cancelled=False
        self.tool_tx={}
        self.routing={'active':False,'status':'No targets','sentPackets':0}
        self.route_owned=set();self.route_baseline=[];self.route_ip=None;self.route_universe=None;self.route_sequence=1
        self.output_test={'active':False,'status':'Not run','sentPackets':0}
        self.test_done=threading.Event()
        self.test_done.set()

    def configure(self, subnet, host_ip):
        with self.lock:
            self.subnet, self.host_ip = subnet, host_ip

    def start(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            # Exclusive binding: never steal a unicast stream from TouchDesigner / another listener.
            sock.bind(('0.0.0.0', self.port))
            sock.settimeout(.5)
            self.socket = sock
            self.port = sock.getsockname()[1]
            self.running, self.started = True, timestamp()
            threading.Thread(target=self.receive_loop, daemon=True).start()
            return True
        except OSError as exc:
            if 'sock' in locals():
                sock.close()
            self.error = 'Cannot bind UDP %s: %s. Another app may already own this port.' % (self.port, exc)
            return False

    def close(self):
        self.stop_event.set()
        self.test_done.wait(timeout=1.2)
        self.running = False
        if self.socket:
            self.socket.close()

    def receive_loop(self):
        while not self.stop_event.is_set():
            try:
                data, sender = self.socket.recvfrom(2048)
                self.ingest(data, sender)
            except socket.timeout:
                continue
            except OSError:
                if not self.stop_event.is_set():
                    self.error = 'UDP receiver stopped unexpectedly.'
                self.running = False
                break

    def ingest(self, data, sender):
        ip, source_port = sender
        with self.lock:
            if ipaddress.ip_address(ip) not in ipaddress.ip_network(self.subnet):
                self.counters['ignored'] += 1
                return
            try:
                packet = parse_packet(data)
            except ValueError:
                self.counters['malformed'] += 1
                return
            t, stamp = time.monotonic(), timestamp()
            if self.logger:
                self.logger.submit(dict(packet,receivedAt=stamp,sourceIp=ip,sourcePort=source_port,
                                        observation='received_by_mac',
                                        direction='node_to_network' if ip in self.nodes or packet['type']=='ArtPollReply' else 'observed_network',
                                        physicalOutputMeasured=False))
            if packet['type'] == 'ArtPollReply':
                self.counters['pollReplies'] += 1
                if ip not in self.nodes and len(self.nodes) >= 32:
                    del self.nodes[min(self.nodes, key=lambda k: self.nodes[k]['_last'])]
                self.nodes[ip] = dict(packet, sourceIp=ip, sourcePort=source_port, lastSeen=stamp, _last=t)
                return
            if packet['type'] != 'ArtDmx':
                self.counters['other'] += 1
                if packet.get('opcode')=='0x2000' and self.running and self.socket:
                    self.advertise(ip)
                return
            self.counters['artDmx'] += 1
            key = '%s/%s/%s' % (ip, packet['universe'], packet['physical'])
            previous = self.streams.get(key)
            if previous is None and len(self.streams) >= 64:
                del self.streams[min(self.streams, key=lambda k: self.streams[k]['_last'])]
                self.counters['evictedStreams'] += 1
            for override_key,override in list(self.manual_overrides.items()):
                if override['sourceIp']==ip and override['universe']==packet['universe'] and (override['channel']>len(packet['values']) or packet['values'][override['channel']-1]!=override['anchor']):
                    del self.manual_overrides[override_key]
            old_values = previous['values'] if previous else []
            changes = [i + 1 for i, v in enumerate(packet['values'])
                       if previous and (i >= len(old_values) or old_values[i] != v)]
            recent = previous['_times'] if previous else deque(maxlen=1000)
            recent.append(t)
            changed_at = previous['_changed'] if previous else {}
            for channel in changes:
                changed_at[channel] = t
            lost = previous.get('sequenceGaps', 0) if previous else 0
            reordered = previous.get('sequenceDiscontinuities', 0) if previous else 0
            if previous and packet['sequence'] and previous['sequence']:
                delta = (packet['sequence'] - previous['sequence']) % 255
                if 1 < delta <= 127:
                    lost += delta - 1
                elif delta != 1:
                    reordered += 1
            stream = dict(packet, id=key, sourceIp=ip, sourcePort=source_port,
                          firstSeen=previous['firstSeen'] if previous else stamp,
                          lastSeen=stamp, packets=(previous['packets'] if previous else 0) + 1,
                          sequenceGaps=lost, sequenceDiscontinuities=reordered,
                          lastChanged=stamp if changes else previous.get('lastChanged') if previous else None,
                          _last=t, _times=recent, _changed=changed_at)
            self.streams[key] = stream
            if previous is None:
                self.events.appendleft(dict(time=stamp, message='New ArtDmx stream: %s · U%s · Physical %s' % (ip, packet['universe'], packet['physical'])))
            if changes:
                self.events.appendleft(dict(time=stamp, sourceIp=ip, universe=packet['universe'],
                                           physical=packet['physical'], changedCount=len(changes),
                                           changes=[dict(channel=i, value=packet['values'][i - 1]) for i in changes[:32]]))

    def poll(self, ip):
        with self.lock:
            if self.tx_blocked():raise ValueError('Blackout · TX stopped')
            if not self.running or not self.socket:
                raise ValueError(self.error or 'Art-Net receiver is not running')
            if ipaddress.ip_address(ip) not in ipaddress.ip_network(self.subnet):
                raise ValueError('Target outside the connected local subnet')
            if time.monotonic() - self.last_poll < 2:
                raise ValueError('Wait two seconds between discovery requests')
            self.socket.sendto(ARTPOLL, (ip, 6454))
            self.advertise(ip)
            self.last_poll = time.monotonic()
            self.events.appendleft(dict(time=timestamp(), message='Read-only ArtPoll sent to ' + ip))

    def advertise(self,ip):
        if self.tx_blocked():return
        universes=[self.input_universe]
        for p in self.nodes.get(ip,{}).get('ports',[]):
            if p['input']:universes.append(p['inputUniverse'])
            if p['output']:universes.append(p['outputUniverse'])
        universes=sorted(set(universes)) or [0,1]
        self.socket.sendto(receiver_reply(self.host_ip,universes),(ip,6454))
        self.subscriptions[ip]=dict(universes=universes[:4],announcedAt=timestamp(),role='Virtual visualizer; no physical DMX output')

    def start_fixture_test(self,ip,fixtures,presets=None):
        with self.lock:
            if not self.running or not self.socket or self.stop_event.is_set():raise ValueError('Receiver unavailable')
            if self.tx_blocked():raise ValueError('Blackout · TX stopped')
            if self.output_test['active']:raise ValueError('A test is already running')
            if ipaddress.ip_address(ip) not in ipaddress.ip_network(self.subnet):raise ValueError('Target outside local subnet')
            node=self.nodes.get(ip)
            if not node or time.monotonic()-node['_last']>15:raise ValueError('Refresh the gateway configuration first')
            outputs=[p for p in node['ports'] if p['output']]
            if len(outputs)!=1:raise ValueError('Exactly one output is required')
            universe=outputs[0]['outputUniverse']
            plan=fixture_test_plan(fixtures,universe,presets)
            baseline=self._optional_baseline(ip,self.input_universe)
            self.output_test=dict(active=True,kind='fixtures',status='Starting',destinationIp=ip,universe=universe,
                                  totalFixtures=len(plan),skippedFixtures=len(fixtures)-len(plan),sentPackets=0,
                                  startedAt=timestamp(),physicalOutputMeasured=False)
            self.test_cancelled=False
            self.test_done.clear()
            threading.Thread(target=self._fixture_test_loop,args=(ip,universe,plan,baseline),daemon=True).start()
            return dict(self.output_test)

    def _fixture_baseline(self,ip,universe):
        # This firmware may emit unchanged DMX frames irregularly. Keep a recent
        # complete frame through short gaps instead of dropping mapped output.
        streams=[s for s in self.streams.values() if s['sourceIp']==ip and s['universe']==universe and time.monotonic()-s['_last']<=15]
        if not streams:raise ValueError('Live desk input required to preserve other channels')
        if len(streams)!=1:raise ValueError('Ambiguous input streams; cannot preserve desk channels')
        return list(streams[0]['values'])

    def _optional_baseline(self,ip,universe):
        try:return self._fixture_baseline(ip,universe)
        except ValueError:
            streams=[s for s in self.streams.values() if s['sourceIp']==ip and s['universe']==universe]
            if len(streams)>1:raise ValueError('Ambiguous input streams')
            return list(streams[0]['values']) if streams else [0]*192

    def _fixture_send(self,ip,universe,values,sequence):
        with self.lock:
            if self.tx_blocked() or self.test_cancelled:raise ValueError('Blackout · test cancelled')
            packet=ARTNET_ID+struct.pack('<H',0x5000)+b'\x00\x0e'+bytes([sequence,0,universe&255,universe>>8])+struct.pack('>H',len(values))+values
            self.socket.sendto(packet,(ip,6454))
            stamp=timestamp()
            with self.lock:
                self.output_test.update(values=list(values),lastSentAt=stamp,sequence=sequence)
                self.output_test['sentPackets']+=1
                self.tool_tx=dict(values=list(values),lastSentAt=stamp,source='Fixture test',universe=universe,destinationIp=ip)
            self.tool_tx_by_ip[ip]=dict(self.tool_tx)
            if self.logger:self.logger.submit(dict(type='ToolArtDmxTX',sentAt=stamp,destinationIp=ip,destinationPort=6454,universe=universe,sequence=sequence,values=list(values),physicalOutputMeasured=False))

    def _fixture_test_loop(self,ip,universe,plan,baseline,phase_seconds=1.0,off_seconds=.25):
        sequence=1;error=None
        try:
            for index,fixture in enumerate(plan):
                for color,duration in [('white',phase_seconds),('red',phase_seconds),('off',off_seconds)]:
                    with self.lock:self.output_test.update(fixtureName=fixture['name'],fixtureIndex=index+1,phase=color,status='Sending')
                    deadline=time.monotonic()+duration
                    while time.monotonic()<deadline:
                        if self.stop_event.is_set():raise ValueError('Server stopping')
                        with self.lock:baseline=self._optional_baseline(ip,self.input_universe)
                        self._fixture_send(ip,universe,fixture_test_frame(plan,baseline,fixture,color),sequence)
                        sequence=sequence%255+1
                        self.stop_event.wait(min(.04,max(0,deadline-time.monotonic())))
        except (OSError,ValueError) as exc:error=str(exc)
        finally:
            # Keep the last known desk values if input disappears; turn tested profiles off.
            try:
                self._fixture_send(ip,universe,fixture_test_frame(plan,baseline),sequence)
                with self.lock:self.output_test.update(zeroSent=True,status='Failed · fixtures off' if error else 'Completed · fixtures off',phase='off')
            except (OSError,ValueError) as exc:
                error=str(exc)
                with self.lock:self.output_test.update(status='Blackout · TX stopped' if self.tx_muted else 'Failed · final off not confirmed',zeroSent=False)
            with self.lock:
                self.output_test.update(active=False,finishedAt=timestamp())
                if error:self.output_test['error']=error
            self.test_done.set()

    def route_tick(self,ip,control,fixtures):
        with self.lock:
            for key,override in list(self.manual_overrides.items()):
                if override['anchor'] is None:continue
                try:self._fixture_baseline(override['sourceIp'],override['universe'])
                except ValueError:self.manual_overrides.pop(key,None)
            if self.tx_blocked():
                self.routing.update(active=False,status='Blackout · TX stopped');return
            if self.output_test['active']:
                self.routing.update(active=False,status='Paused for fixture test');return
            if not self.running or not self.socket:return
            error=None;owned=set()
            try:
                controls=control if isinstance(control,list) else [control]
                controls=[c for c in controls if c and c.get('enabled')]
                direct_controls=[c for c in controls if routing_mode(c)=='direct']
                mapped_controls=[c for c in controls if routing_mode(c)=='mapped']
                active_controls=direct_controls+mapped_controls
                if not active_controls:raise ValueError('Waiting for enabled direct or mapped inputs')
                if ipaddress.ip_address(ip) not in ipaddress.ip_network(self.subnet):raise ValueError('Target outside subnet')
                node=self.nodes.get(ip)
                outputs=[p for p in node.get('ports',[]) if p['output']] if node else []
                if not node or time.monotonic()-node['_last']>15 or len(outputs)!=1:raise ValueError('Fresh output configuration required')
                universe=outputs[0]['outputUniverse']
                input_universes={c['universe'] for c in active_controls}
                if len(input_universes)!=1:raise ValueError('Enabled input controls must share one source universe')
                input_universe=next(iter(input_universes))
                target_ids=set(fid for c in mapped_controls for fid in c['targetFixtures'])
                targets=[f for f in fixtures if f['id'] in target_ids]
                if len(targets)!=len(target_ids) or any(f['universe']!=universe for f in targets):raise ValueError('Targets must exist in the configured output universe')
                manual_mode=False
                try:baseline=self._fixture_baseline(ip,input_universe)
                except ValueError:
                    if not any(o['sourceIp']==ip and o['universe']==input_universe for o in self.manual_overrides.values()):raise
                    baseline=self._optional_baseline(ip,input_universe);manual_mode=True
                # Nothing passes implicitly. Each DIRECT card explicitly owns
                # and copies its own 1:1 range; FUNCTION REMAP cards only own
                # semantic destination channels.
                frame=[0]*len(baseline)
                owned=set()
                # A direct input is a true 1:1 desk bank. Its temporary UI
                # overrides replace the same output channels and are cancelled
                # as soon as the corresponding physical desk channel moves.
                for c in direct_controls:
                    for index in range(1,c['count']+1):
                        source_channel=c['start']+index-1
                        frame[source_channel-1]=baseline[source_channel-1]
                        owned.add(source_channel-1)
                        override=self.manual_overrides.get((c['id'],index))
                        if not override:continue
                        if override['channel']!=source_channel or (override['anchor'] is not None and override['anchor']!=baseline[source_channel-1]):
                            self.manual_overrides.pop((c['id'],index),None);continue
                        frame[source_channel-1]=override['value']
                # Function-remap banks do not also leak their source channels
                # to identically numbered output addresses.
                for c in mapped_controls:
                    count=c.get('count',len(c.get('faderFunctions',[])) or 16)
                    frame[c['start']-1:c['start']-1+count]=[0]*count
                assignments={}
                for c in mapped_controls:
                    rules=semantic_rules(c['targetFixtures'],fixtures,c.get('faderFunctions'))
                    if c['start']+max((r['input'] for r in rules),default=1)-1>len(baseline):raise ValueError('Input frame does not contain all mapped faders')
                    for rule in rules:
                        f=next(f for f in targets if f['id']==rule['fixtureId'])
                        destination=f['start']+rule['channel']-2
                        if destination in assignments:raise ValueError('Conflicting input assignments for '+f['name']+' / '+f['channels'][rule['channel']-1]['function'])
                        override=self.manual_overrides.get((c['id'],rule['input']))
                        source_channel=c['start']+rule['input']-1
                        if override and (override['channel']!=source_channel or (override['anchor'] is not None and override['anchor']!=baseline[source_channel-1])):
                            self.manual_overrides.pop((c['id'],rule['input']),None);override=None
                        assignments[destination]=override['value'] if override else baseline[source_channel-1]
                if any(c.get('targetFixtures') for c in mapped_controls) and not assignments:raise ValueError('Selected fixtures do not support assigned functions')
                owned.update(assignments)
                prior=self.route_owned if self.route_ip==ip and self.route_universe==universe else set()
                end=max(len(baseline),max(owned|prior,default=-1)+1);end+=end%2
                frame += [0]*(end-len(frame))
                # Only explicit function assignments override the incoming desk frame.
                # Removed assignments within the received frame return to the desk.
                for channel in prior-owned:
                    if channel>=len(baseline):frame[channel]=0
                for destination,value in assignments.items():frame[destination]=value
                self._route_send(ip,universe,bytes(frame))
                self.route_owned=owned;self.route_baseline=baseline;self.route_ip=ip;self.route_universe=universe
                route_kind='Direct 1:1 + function remap' if direct_controls and mapped_controls else 'Direct 1:1' if direct_controls else 'Function remap'
                self.routing.update(active=True,status='Manual · desk offline' if manual_mode else route_kind,
                                    controlIds=[c['id'] for c in active_controls],targets=len(targets),universe=universe,
                                    inputUniverse=input_universe,mergePolicy=route_kind,error=None)
                return
            except (ValueError,OSError) as exc:error=str(exc)
            if self.route_owned:
                end=max(len(self.route_baseline),max(self.route_owned)+1);end+=end%2;frame=[0]*end
                try:
                    self._route_send(self.route_ip,self.route_universe,bytes(frame));self.route_owned=set()
                except OSError as exc:error+='; final off failed: '+str(exc)
            self.routing.update(active=False,status=error,error=error)

    def _route_send(self,ip,universe,values,source='Input forwarding'):
        if self.tx_blocked():raise ValueError('Blackout · TX stopped')
        sequence=self.route_sequence
        packet=ARTNET_ID+struct.pack('<H',0x5000)+b'\x00\x0e'+bytes([sequence,0,universe&255,universe>>8])+struct.pack('>H',len(values))+values
        self.socket.sendto(packet,(ip,6454));stamp=timestamp();self.route_sequence=sequence%255+1
        self.tool_tx=dict(values=list(values),lastSentAt=stamp,source=source,universe=universe,destinationIp=ip)
        self.tool_tx_by_ip[ip]=dict(self.tool_tx)
        self.routing['sentPackets']+=1
        if self.logger:self.logger.submit(dict(type='ToolArtDmxTX',sentAt=stamp,destinationIp=ip,destinationPort=6454,universe=universe,sequence=sequence,values=list(values),source=source,physicalOutputMeasured=False))

    def set_manual_fader(self,ip,control,index,value,expected_input=None,expected_offline=False):
        with self.lock:
            if self.tx_blocked():raise ValueError('Blackout · TX stopped')
            if type(index) is not int or not 1<=index<=control['count'] or type(value) is not int or not 0<=value<=255:raise ValueError('Invalid fader or value')
            online=True
            try:baseline=self._fixture_baseline(ip,control['universe'])
            except ValueError:baseline=self._optional_baseline(ip,control['universe']);online=False
            if expected_offline and online:raise ValueError('Desk input resumed; manual override cancelled')
            channel=control['start']+index-1
            if channel>len(baseline):raise ValueError('Channel not present in input frame')
            if online and expected_input is not None and (type(expected_input) is not int or expected_input!=baseline[channel-1]):raise ValueError('Desk channel moved; manual override cancelled')
            self.manual_overrides[(control['id'],index)]=dict(controlId=control['id'],fader=index,value=value,sourceIp=ip,universe=control['universe'],channel=channel,anchor=baseline[channel-1] if online else None)
            return dict(self.manual_overrides[(control['id'],index)])

    def send_pixel_frames(self,frames,sequence):
        with self.lock:
            if self.tx_blocked():return False
            for (ip,universe),values in sorted(frames.items()):
                if ipaddress.ip_address(ip) not in ipaddress.ip_network(self.subnet):raise ValueError('LED controller outside subnet')
                packet=ARTNET_ID+struct.pack('<H',0x5000)+b'\x00\x0e'+bytes([sequence,0,universe&255,universe>>8])+struct.pack('>H',512)+bytes(values)
                self.socket.sendto(packet,(ip,6454))
                self.tool_tx_by_ip[ip]=dict(values=list(values),universe=universe,destinationIp=ip,source='LED virtual DMX',lastSentAt=timestamp())
                if self.logger:self.logger.submit(dict(type='ToolArtDmxTX',sentAt=timestamp(),destinationIp=ip,destinationPort=6454,universe=universe,values=list(values),source='LED virtual DMX',physicalOutputMeasured=False))
            for ip in sorted({ip for ip,u in frames}):self.socket.sendto(ARTNET_ID+struct.pack('<H',0x5200)+b'\x00\x0e\x00\x00',(ip,6454))
            return True

    def blackout(self,ip,extra_frames=None):
        with self.lock:
            result=dict(txMuted=True,zeroSent=False)
            try:
                if self.tx_blocked():return dict(result,error='TX already stopped; no additional packet sent')
                if not self.running or not self.socket:raise ValueError('Output socket unavailable')
                if ipaddress.ip_address(ip) not in ipaddress.ip_network(self.subnet):raise ValueError('Target outside subnet')
                node=self.nodes.get(ip)
                ports=[p for p in node.get('ports',[]) if p['output']] if node else []
                if len(ports)!=1:raise ValueError('Output universe unverified')
                universe=ports[0]['outputUniverse']
                self._route_send(ip,universe,bytes(512),source='Blackout')
                for (led_ip,led_universe),frame in (extra_frames or {}).items():self._route_send(led_ip,led_universe,bytes(512),source='Blackout')
                self.tool_tx_by_ip[ip]['source']='Blackout · 512 zeros'
                self.tool_tx['source']='Blackout · 512 zeros'
                self.route_owned.clear()
                result.update(zeroSent=True,universe=universe,channels=512)
            except (OSError,ValueError) as exc:result['error']='TX stopped, but zero frame failed: '+str(exc)
            finally:self.set_tx_muted(True)
            self.blackout_status=result
            return result

    def tx_blocked(self):
        return self.tx_muted or (self.tx_permission is not None and not self.tx_permission())

    def set_tx_muted(self,enabled):
        with self.lock:
            if not enabled and self.tx_permission is not None and not self.tx_permission():
                raise ValueError('TX requires fresh, confirmed SOFTWARE CONTROL')
            self.tx_muted=enabled
            if enabled:
                self.manual_overrides.clear()
                if self.output_test['active']:self.test_cancelled=True
                self.routing.update(active=False,status='Blackout · TX stopped')
            return enabled

    def snapshot(self):
        with self.lock:
            t = time.monotonic()
            streams = []
            for s in self.streams.values():
                clean = {k: v for k, v in s.items() if not k.startswith('_')}
                clean.update(ageSeconds=round(t - s['_last'], 2), live=t - s['_last'] <= 15,
                             packetsPerSecond=sum(x >= t - 1 for x in s['_times']),
                             changedChannels=[ch for ch, when in s['_changed'].items() if t - when <= 1.5],
                             nonzeroChannels=[i + 1 for i, v in enumerate(s['values']) if v])
                streams.append(clean)
            nodes = []
            for n in self.nodes.values():
                clean = {k: v for k, v in n.items() if not k.startswith('_')}
                clean['ageSeconds'] = round(t - n['_last'], 2)
                nodes.append(clean)
            return dict(listening=self.running, error=self.error, udpPort=self.port,
                        subnet=self.subnet, hostIp=self.host_ip, startedAt=self.started,
                        receivedAt=timestamp(), counters=dict(self.counters), streams=streams,
                        nodes=nodes, events=list(self.events), inputUniverse=self.input_universe,toolTxByIp=dict(self.tool_tx_by_ip),blackout=dict(self.blackout_status),manualOverrides=list(self.manual_overrides.values()),outputEnabled=not self.tx_blocked(),txMuted=self.tx_blocked(),
                        outputTest=dict(self.output_test),toolTx=dict(self.tool_tx),routing=dict(self.routing),physicalOutputFeedback=None,
                        logging=self.logger.status() if self.logger else None,
                        subscriptions=dict(self.subscriptions),
                        visibility='Only Art-Net packets delivered to this Mac are visible. Route the input stream to this Mac; other unicast traffic is not captured.')
