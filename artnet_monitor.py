"""Bounded Art-Net receiver with discovery and explicit mapped-fixture sequence test."""
from collections import deque
import copy
import statistics
from fixtures import fixture_test_plan, fixture_test_frame, fx_channel_slots
from input_controls import semantic_rules, routing_mode
from midi_inputs import mapped_values as midi_mapped_values, LastChange
import datetime as dt
import ipaddress
import socket
import struct
import threading
import time

ARTNET_ID = b'Art-Net\x00'
ARTPOLL = ARTNET_ID + struct.pack('<H', 0x2000) + b'\x00\x0e\x00\x00'
DESK_HOLD_SECONDS = 10.0


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
        return dict(type='ArtPollReply', shortName=text(26, 44), longName=text(44, 108), bindIndex=data[211] if len(data)>211 else 0,
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
        self.route_condition = threading.Condition()
        self.route_revision = 0
        self.route_trigger = None
        self.route_latency_samples = deque(maxlen=256)
        self.semantic_rule_cache = {}
        self.last_poll = float('-inf')
        self.logger=logger
        self.subscriptions={}
        self.input_universe=1
        self.tool_tx_by_ip={}
        self.blackout_status={}
        self.manual_overrides={}
        self.fixture_controls={}
        self.fx_activations={}
        self.fx_sequence=dict(active=False,status='Not run',fixtureIndex=0,totalFixtures=0)
        self.sequence_trigger_high={}
        self.sequence_input_values={}
        self.beam_lamp_states={}
        self.wiz_targets={}
        self.wiz_routing_status={}
        self.tx_muted=False
        self.tx_permission=None
        self.test_cancelled=False
        self.tool_tx={}
        self.routing={'active':False,'status':'No targets','sentPackets':0}
        self.input_takeover=LastChange();self.route_wiz_owned=set();
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
        with self.route_condition:self.route_condition.notify_all()
        self.test_done.wait(timeout=1.2)
        self.running = False
        if self.socket:
            self.socket.close()

    def wake_routing(self):
        """Wake the latest-frame router after a control/configuration change."""
        with self.route_condition:
            self.route_revision += 1
            self.route_trigger = None
            self.route_condition.notify()

    def wait_for_route_frame(self, revision, timeout=None):
        """Wait for a received ArtDmx frame or an explicit routing change."""
        with self.route_condition:
            if self.route_revision == revision and not self.stop_event.is_set():
                self.route_condition.wait_for(
                    lambda: self.route_revision != revision or self.stop_event.is_set(), timeout)
            return self.route_revision, self.route_trigger

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
                if packet['shortName'].startswith('SP801E_'):
                    prior=self.nodes.get(ip,{})
                    pages={index:page for index,page in prior.get('_pages',{}).items() if t-page['_last']<30}
                    page_index=packet['bindIndex'] or min((p['outputUniverse'] for p in packet['ports'] if p['output']),default=0)
                    pages[page_index]=dict(packet,_last=t)
                    ports=[]
                    for page_no,page in sorted(pages.items()):
                        for port in page['ports']:
                            ports.append(dict(port,port=len(ports)+1))
                    self.nodes[ip] = dict(packet,ports=ports,bindPages=len(pages),sourceIp=ip,
                                          sourcePort=source_port,lastSeen=stamp,_last=t,_pages=pages)
                else:
                    self.nodes[ip] = dict(packet,bindPages=1,sourceIp=ip,sourcePort=source_port,lastSeen=stamp,_last=t)
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
            with self.route_condition:
                self.route_revision += 1
                self.route_trigger = (ip, packet['universe'], t)
                self.route_condition.notify()
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

    def start_fixture_test(self,ip,fixtures,presets=None,output_universe=None,input_universe=None):
        with self.lock:
            if not self.running or not self.socket or self.stop_event.is_set():raise ValueError('Receiver unavailable')
            if self.tx_blocked():raise ValueError('Blackout · TX stopped')
            if self.output_test['active']:raise ValueError('A test is already running')
            if ipaddress.ip_address(ip) not in ipaddress.ip_network(self.subnet):raise ValueError('Target outside local subnet')
            if output_universe is None:
                node=self.nodes.get(ip)
                if not node or time.monotonic()-node['_last']>15:raise ValueError('Refresh the gateway configuration first')
                outputs=[p for p in node['ports'] if p['output']]
                if len(outputs)!=1:raise ValueError('Exactly one output is required')
                universe=outputs[0]['outputUniverse']
            else:universe=output_universe
            plan=fixture_test_plan(fixtures,universe,presets)
            if self.fx_sequence.get('active'):raise ValueError('Stop the FX sequence before starting another fixture test')
            # The desk input is on another universe and is not the output
            # frame. Reuse only this tool's recent, already routed output.
            previous=self.tool_tx_by_ip.get(ip,{})
            try:recent=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(previous['lastSentAt'])).total_seconds()<DESK_HOLD_SECONDS
            except (KeyError,TypeError,ValueError):recent=False
            baseline=list(previous['values']) if recent and previous.get('universe')==universe else []
            self.output_test=dict(active=True,kind='fixtures',status='Starting',destinationIp=ip,universe=universe,
                                  totalFixtures=len(plan),skippedFixtures=len(fixtures)-len(plan),sentPackets=0,
                                  startedAt=timestamp(),physicalOutputMeasured=False)
            self.test_cancelled=False
            self.test_done.clear()
            threading.Thread(target=self._fixture_test_loop,args=(ip,universe,plan,baseline),daemon=True).start()
            return dict(self.output_test)

    def _fixture_baseline(self,ip,universe):
        # Hold the last desk frame through brief gaps, but never beyond ten seconds.
        streams=[s for s in self.streams.values() if s['sourceIp']==ip and s['universe']==universe and time.monotonic()-s['_last']<DESK_HOLD_SECONDS]
        if not streams:raise ValueError('Live desk input required to preserve other channels')
        if len(streams)!=1:raise ValueError('Ambiguous input streams; cannot preserve desk channels')
        return list(streams[0]['values'])

    def _optional_baseline(self,ip,universe):
        try:return self._fixture_baseline(ip,universe)
        except ValueError:
            streams=[s for s in self.streams.values() if s['sourceIp']==ip and s['universe']==universe]
            if len(streams)>1:raise ValueError('Ambiguous input streams')
            return list(streams[0]['values']) if streams and time.monotonic()-streams[0]['_last']<DESK_HOLD_SECONDS else [0]*192

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
        sequence=1;error=None;completed=[]
        try:
            for index,fixture in enumerate(plan):
                for color,duration in [('white',phase_seconds),('red',phase_seconds),('off',off_seconds)]:
                    with self.lock:self.output_test.update(fixtureName=fixture['name'],fixtureIndex=index+1,phase=color,status='Sending')
                    deadline=time.monotonic()+duration
                    while time.monotonic()<deadline:
                        if self.stop_event.is_set():raise ValueError('Server stopping')
                        self._fixture_send(ip,universe,fixture_test_frame(plan,baseline,fixture,color,completed),sequence)
                        sequence=sequence%255+1
                        self.stop_event.wait(min(.04,max(0,deadline-time.monotonic())))
                completed.append(fixture)
        except (OSError,ValueError) as exc:error=str(exc)
        finally:
            # Turn tested profiles off without changing unrelated output slots.
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
            self.wake_routing()

    def route_tick(self,ip,control,fixtures,output_universe=None,midi_controls=None):
        with self.lock:
            for key,override in list(self.manual_overrides.items()):
                if override['anchor'] is None:continue
                try:self._fixture_baseline(override['sourceIp'],override['universe'])
                except ValueError:self.manual_overrides.pop(key,None)
            if self.tx_blocked():
                status='Blackout · TX stopped' if self.tx_muted else 'Safety interlock · controller output not verified'
                self.routing.update(active=False,status=status);return
            if self.output_test['active']:
                self.routing.update(active=False,status='Paused for fixture test');return
            if not self.running or not self.socket:return
            error=None;owned=set()
            try:
                controls=control if isinstance(control,list) else [control]
                controls=[c for c in controls if c and c.get('enabled')]
                midi_controls=midi_controls or []
                midi_values=midi_mapped_values(midi_controls,fixtures,lambda fid,values:self._beam_native_values('parled',values,self.fixture_controls.get(fid,{}).get('colorSlots',{})))
                # Virtual LED fixtures are rendered by LEDRuntime and sent to
                # their own pixel controller. Never treat their virtual slots
                # as physical channels on the Art-Net/DMX2X output universe.
                virtual_led_ids={f['id'] for f in fixtures if f.get('virtualLED')}
                midi_values={key:value for key,value in midi_values.items()
                             if not (key[0]=='virtual' and key[1] in virtual_led_ids)}
                direct_controls=[c for c in controls if routing_mode(c)=='direct']
                mapped_controls=[c for c in controls if routing_mode(c)=='mapped']
                active_controls=direct_controls+mapped_controls
                now_mono=time.monotonic()
                active_fixture_controls={fid:item for fid,item in self.fixture_controls.items()
                    if item.get('destinationIp')==ip
                    and ((item.get('manualUntil') or 0)>now_mono or (item.get('actionDeadline') or 0)>now_mono)}
                has_sequence_target=any(next((f for f in fixtures if f.get('id')==fid and f.get('virtualSequence')),None)
                                        for c in mapped_controls+midi_controls if c.get('enabled',True)
                                        for fid in c.get('targetFixtures',[]))
                # Continue one final compose/send cycle after a source or FX
                # control is removed. This releases channels that the previous
                # frame owned instead of leaving their last values latched.
                has_owned_outputs=bool(self.route_owned or self.route_wiz_owned)
                if not active_controls and not active_fixture_controls and not self.fx_activations and not midi_values and not has_sequence_target and not has_owned_outputs:raise ValueError('Waiting for enabled direct or mapped inputs')
                if ipaddress.ip_address(ip) not in ipaddress.ip_network(self.subnet):raise ValueError('Target outside subnet')
                if output_universe is None:
                    node=self.nodes.get(ip)
                    outputs=[p for p in node.get('ports',[]) if p['output']] if node else []
                    if not node or time.monotonic()-node['_last']>15 or len(outputs)!=1:raise ValueError('Fresh output configuration required')
                    universe=outputs[0]['outputUniverse']
                else:universe=output_universe
                input_universes={c['universe'] for c in active_controls}
                if len(input_universes)>1 or active_controls and len(input_universes)!=1:raise ValueError('Enabled input controls must share one source universe')
                input_universe=next(iter(input_universes)) if input_universes else self.input_universe
                requested_target_ids=set(fid for c in mapped_controls for fid in c.get('targetFixtures',[]))
                requested_target_ids.update(fid for c in midi_controls for fid in c.get('targetFixtures',[]))
                by_fixture={f['id']:f for f in fixtures}
                valid_target_ids={fid for fid in requested_target_ids
                                  if fid in by_fixture and by_fixture[fid].get('universe')==universe}
                skipped_targets=sorted(requested_target_ids-valid_target_ids)
                # A missing/unavailable fixture is isolated to that target. It
                # must not stop valid MIDI, desk, or sequence routing.
                for control in mapped_controls+midi_controls:
                    control['targetFixtures']=[fid for fid in control.get('targetFixtures',[]) if fid in valid_target_ids]
                target_ids=set(fid for c in mapped_controls for fid in c['targetFixtures'])
                target_ids.update(fid for c in midi_controls for fid in c.get('targetFixtures',[]))
                targets=[by_fixture[fid] for fid in target_ids]
                manual_mode=False;desk_stale=False
                try:baseline=self._fixture_baseline(ip,input_universe)
                except ValueError:
                    # Expired desk values become zero; independent MIDI and FX
                    # values are still composed into the same output frame.
                    baseline=self._optional_baseline(ip,input_universe);manual_mode=True
                    desk_stale=not any(s['sourceIp']==ip and s['universe']==input_universe and
                                       time.monotonic()-s['_last']<DESK_HOLD_SECONDS for s in self.streams.values())
                streams=[v for v in self.streams.values() if v['sourceIp']==ip and v['universe']==input_universe]
                desk_changes=streams[0].get('_changed',{}) if len(streams)==1 else {}
                desk_times={}
                sequence_inputs=[]
                # Sequence virtual fixtures are edge-triggered: a full raise
                # starts one run; holding high cannot restart it. The source
                # must fall below the rearm threshold before another run.
                by_fixture={f['id']:f for f in fixtures}
                for c in midi_controls:
                    if not c.get('enabled'):continue
                    for fid in c.get('targetFixtures',[]):
                        target=by_fixture.get(fid)
                        if not target or not target.get('virtualSequence'):continue
                        for index,role in enumerate(c.get('faderFunctions',[])):
                            if index<len(c.get('values',[])):
                                stamp=c.get('changed',[0]*16)[index]
                                if not stamp:continue
                                value=c['values'][index]
                                # MIDI sends changes, not periodic frames: a held
                                # button is not stale merely because it is still.
                                sequence_inputs.append((stamp,target,c['id']+':midi:'+str(index),str(role).casefold(),value,c.get('targetModes',{}).get(fid)))
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
                        desk_times[('dmx',source_channel-1)]=desk_changes.get(source_channel,0)
                        owned.add(source_channel-1)
                        override=self.manual_overrides.get((c['id'],index))
                        if not override:continue
                        if override['channel']!=source_channel or (override['anchor'] is not None and override['anchor']!=baseline[source_channel-1]):
                            self.manual_overrides.pop((c['id'],index),None);continue
                        frame[source_channel-1]=override['value']
                        desk_times[('dmx',source_channel-1)]=override.get('changed',0)
                # Function-remap banks do not also leak their source channels
                # to identically numbered output addresses.
                for c in mapped_controls:
                    count=c.get('count',len(c.get('faderFunctions',[])) or 16)
                    frame[c['start']-1:c['start']-1+count]=[0]*count
                assignments={};assignment_times={};wiz_assignments={}
                def htp_assignment(destination,value,changed):
                    previous=assignments.get(destination)
                    if previous is None or value>previous:
                        assignments[destination]=value;assignment_times[destination]=changed
                    elif value==previous:
                        assignment_times[destination]=max(assignment_times[destination],changed)
                for c in mapped_controls:
                    target_modes=c.get('targetModes',{})
                    parled_ids={fid for fid in c.get('targetFixtures',[]) if target_modes.get(fid)=='parled'}
                    cache_key=(id(c),id(fixtures))
                    cached=self.semantic_rule_cache.get(cache_key)
                    rules=cached[2] if cached and cached[0] is c and cached[1] is fixtures else None
                    if rules is None:
                        rules=semantic_rules([fid for fid in c['targetFixtures'] if fid not in parled_ids],fixtures,c.get('faderFunctions'))
                        if len(self.semantic_rule_cache)>=256:self.semantic_rule_cache.clear()
                        self.semantic_rule_cache[cache_key]=(c,fixtures,rules)
                    if c['start']+max((r['input'] for r in rules),default=1)-1>len(baseline):raise ValueError('Input frame does not contain all mapped faders')
                    for rule in rules:
                        f=next(f for f in targets if f['id']==rule['fixtureId'])
                        source_channel=c['start']+rule['input']-1
                        override=self.manual_overrides.get((c['id'],rule['input']))
                        if override and (override['channel']!=source_channel or (override['anchor'] is not None and override['anchor']!=baseline[source_channel-1])):
                            self.manual_overrides.pop((c['id'],rule['input']),None);override=None
                        value=override['value'] if override else baseline[source_channel-1]
                        # Matrix pixel values are consumed by LEDRuntime's
                        # independent pixel-Art-Net sender, not the DMX2X.
                        if f.get('virtualLED'):continue
                        if f.get('virtualSequence'):
                            function=f['channels'][rule['channel']-1]['function'].casefold()
                            changed=override.get('changed',0) if override else desk_changes.get(source_channel,0)
                            sequence_inputs.append((changed,f,c['id']+':dmx:'+str(rule['input']),function,value,c.get('targetModes',{}).get(f['id'])))
                            continue
                        destination=f['start']+rule['channel']-2
                        k=('virtual',f['id'],f['channels'][rule['channel']-1]['function']) if f.get('virtualWiz') else ('dmx',destination)
                        changed=override.get('changed',0) if override else desk_changes.get(source_channel,0)
                        if f.get('virtualWiz'):
                            role=f['channels'][rule['channel']-1]['function']
                            prior=wiz_assignments.setdefault(f['id'],{}).get(role)
                            if prior is None or value>=prior:
                                wiz_assignments[f['id']][role]=value
                                desk_times[k]=max(desk_times.get(k,0),changed) if value==prior else changed
                        else:
                            htp_assignment(destination,value,changed)
                    if parled_ids:
                        roles=['Master','Red','Green','Blue','White','Amber','UV','Strobe']
                        functions=c.get('faderFunctions',[])
                        for fid in parled_ids:
                            f=next((item for item in targets if item['id']==fid),None)
                            if not f or f.get('mappingPreset')!='beam230-16ch':raise ValueError('ParLed emulation requires a Beam 230 target')
                            values=[0]*8;beam_changed=0
                            for index,function in enumerate(functions):
                                if function not in roles:continue
                                source_channel=c['start']+index
                                if source_channel>len(baseline):raise ValueError('Input frame does not contain all mapped faders')
                                override=self.manual_overrides.get((c['id'],index+1))
                                if override and (override['channel']!=source_channel or (override['anchor'] is not None and override['anchor']!=baseline[source_channel-1])):
                                    self.manual_overrides.pop((c['id'],index+1),None);override=None
                                values[roles.index(function)]=override['value'] if override else baseline[source_channel-1]
                                beam_changed=max(beam_changed,override.get('changed',0) if override else desk_changes.get(source_channel,0))
                            slots=self.fixture_controls.get(fid,{}).get('colorSlots',{})
                            native=self._beam_native_values('parled',values,slots)
                            for index,value in enumerate(native):
                                destination=f['start']+index-1
                                htp_assignment(destination,value,beam_changed)
                self._apply_sequence_inputs(ip,sequence_inputs,fixtures,universe)
                has_sequence_target=any(by_fixture.get(fid,{}).get('virtualSequence') for c in mapped_controls+midi_controls for fid in c.get('targetFixtures',[]))
                has_virtual_led_target=any(by_fixture.get(fid,{}).get('virtualLED')
                                           for c in mapped_controls+midi_controls for fid in c.get('targetFixtures',[]))
                if not midi_values and any(c.get('targetFixtures') for c in mapped_controls) and not assignments and not wiz_assignments and not has_sequence_target and not has_virtual_led_target:raise ValueError('Selected fixtures do not support assigned functions')
                direct_owned=set(owned)
                owned.update(assignments)
                # Keep known FX activation/effect slots in every routed frame,
                # including OFF. A short frame or a lost single release must
                # not leave the controller holding an old armed/effect value.
                for fixture in fixtures:
                    if fixture.get('mappingPreset')=='fx-activate-effect-4ch' and fixture.get('universe')==universe:
                        for offset in fx_channel_slots(fixture):owned.add(fixture['start']-1+offset)
                end=max(len(baseline),max(owned|self.route_owned,default=-1)+1);end+=end%2
                frame += [0]*(end-len(frame))
                for destination,value in assignments.items():
                    key=('dmx',destination)
                    if destination in direct_owned and frame[destination]>value:continue
                    if destination in direct_owned and frame[destination]==value:
                        desk_times[key]=max(desk_times.get(key,0),assignment_times[destination])
                    else:
                        frame[destination]=value;desk_times[key]=assignment_times[destination]
                if self.route_ip!=ip or self.route_universe!=universe:self.input_takeover=LastChange()
                # A stale desk must not seize last-movement ownership from MIDI
                # merely because its expired values are cleared to zero.
                desk_values={} if desk_stale else {('dmx',channel):frame[channel] for channel in owned}
                if not desk_stale:desk_values.update({('virtual',fid,role):value for fid,roles in wiz_assignments.items() for role,value in roles.items()})
                mixed=self.input_takeover.mix(desk_values,midi_values,desk_times)
                current_wiz=set()
                for k,value in mixed.items():
                    if k[0]=='dmx':
                        destination=k[1]
                        if destination>=len(frame):frame.extend([0]*(destination+1-len(frame)))
                        frame[destination]=value;owned.add(destination)
                    else:
                        _,fid,role=k;current_wiz.add((fid,role));self.wiz_targets.setdefault(fid,{})[role]=value
                for fid,role in self.route_wiz_owned-current_wiz:self.wiz_targets.setdefault(fid,{})[role]=0
                self.route_wiz_owned=current_wiz
                # No source writes implicit values into another source's channels.
                # Explicit mouse control owns only the selected fixture's 16
                # output channels; incoming desk values cannot overwrite them.
                # Read FX state after processing all input commands. Taking this
                # snapshot earlier emitted stale CH1/CH2 in the release frame.
                for fid,item in self.fx_activations.items():
                    if item.get('destinationIp')==ip:
                        active_fixture_controls[fid]=dict(destinationIp=ip,nativeSlots={
                            item['activateOffset']:item['activateValue'],
                            item['effectOffset']:255 if item.get('effect') else 0})
                for fid,item in active_fixture_controls.items():
                    fixture=next((f for f in fixtures if f['id']==fid),None)
                    if not fixture or fixture['universe']!=universe:continue
                    if 'nativeSlots' in item:
                        armed=self.fx_activations.get(fid)
                        try:current_slots=fx_channel_slots(fixture)
                        except ValueError:current_slots=None
                        if not armed or fixture['start']!=armed['start'] or fixture['universe']!=armed['universe'] or len(fixture['channels'])!=armed['channelCount'] or current_slots!=(armed['activateOffset'],armed['effectOffset']):
                            continue
                        slots=item['nativeSlots'].items()
                    else:slots=enumerate(item['nativeValues'])
                    for index,value in slots:
                        destination=fixture['start']-1+index
                        if destination>=len(frame):frame.extend([0]*(destination+1-len(frame)))
                        frame[destination]=value;owned.add(destination)
                input_streams=[s for s in self.streams.values()
                               if s['sourceIp']==ip and s['universe']==input_universe
                               and time.monotonic()-s['_last']<DESK_HOLD_SECONDS]
                received_monotonic=max((s['_last'] for s in input_streams),default=None)
                tx_source='Input forwarding + MIDI' if midi_values else 'Input forwarding + Beam mouse control' if active_fixture_controls else 'Input forwarding'
                if len(frame)%2:frame.append(0)
                if owned or self.route_owned or active_fixture_controls:
                    previous=self.tool_tx_by_ip.get(ip,{})
                    already_cleared=(desk_stale and not midi_values and not active_fixture_controls and
                                     previous.get('universe')==universe and previous.get('values')==frame)
                    if not already_cleared:
                        self._route_send(ip,universe,bytes(frame),source=tx_source,received_monotonic=received_monotonic)
                self.route_owned=owned;self.route_baseline=baseline;self.route_ip=ip;self.route_universe=universe
                route_kind='MIDI function remap' if midi_values else 'Direct 1:1 + function remap' if direct_controls and mapped_controls else 'Direct 1:1' if direct_controls else 'Function remap'
                if wiz_assignments and not owned:route_kind='WiZ local function remap'
                standalone_manual=bool(self.manual_overrides) or any((item.get('manualUntil') or 0)>now_mono for item in self.fixture_controls.values())
                route_status='Manual · desk offline' if manual_mode and standalone_manual else 'Input stale · desk channels off' if desk_stale else 'Manual · desk offline' if manual_mode else route_kind
                if skipped_targets:route_status+=' · skipped '+', '.join(skipped_targets)
                self.routing.update(active=not desk_stale or bool(midi_values or active_fixture_controls),
                                    status=route_status,
                                    controlIds=[c['id'] for c in active_controls],targets=len(targets),universe=universe,
                                    inputUniverse=input_universe,mergePolicy=route_kind,error=None)
                return
            except (ValueError,OSError) as exc:error=str(exc)
            for fid,role in self.route_wiz_owned:self.wiz_targets.setdefault(fid,{})[role]=0
            self.route_wiz_owned=set()
            # A transient routing error must not erase unrelated fixtures.
            # ArtDmx carries a full frame, so a blanket zero packet here
            # would also overwrite every desk-controlled channel. Retain the
            # owned slots; the next valid frame will clear released slots.
            self.routing.update(active=False,status=error,error=error)

    def _route_send(self,ip,universe,values,source='Input forwarding',received_monotonic=None):
        if self.tx_blocked():raise ValueError('Blackout · TX stopped')
        sequence=self.route_sequence
        packet=ARTNET_ID+struct.pack('<H',0x5000)+b'\x00\x0e'+bytes([sequence,0,universe&255,universe>>8])+struct.pack('>H',len(values))+values
        self.socket.sendto(packet,(ip,6454));stamp=timestamp();self.route_sequence=sequence%255+1
        self.tool_tx=dict(values=list(values),lastSentAt=stamp,source=source,universe=universe,destinationIp=ip)
        self.tool_tx_by_ip[ip]=dict(self.tool_tx)
        self.routing['sentPackets']+=1
        if source.startswith('Input forwarding') and received_monotonic is not None:
            latency_ms=max(0.0,(time.monotonic()-received_monotonic)*1000)
            self.route_latency_samples.append((time.monotonic(),latency_ms))
            self.routing['lastAppLatencyMs']=round(latency_ms,2)
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
            self.manual_overrides[(control['id'],index)]=dict(controlId=control['id'],fader=index,value=value,sourceIp=ip,universe=control['universe'],channel=channel,anchor=baseline[channel-1] if online else None,changed=time.monotonic())
            return dict(self.manual_overrides[(control['id'],index)])

    @staticmethod
    def _beam_native_values(mode,values,color_slots=None):
        if mode=='original':
            if not isinstance(values,list) or len(values)!=16 or any(type(v) is not int or not 0<=v<=255 for v in values):
                raise ValueError('Original mode requires 16 DMX values from 0 to 255')
            native=list(values);native[15]=0
            return native
        if mode!='parled':raise ValueError('Unknown Beam control mode')
        if not isinstance(values,list) or len(values)!=8 or any(type(v) is not int or not 0<=v<=255 for v in values):
            raise ValueError('ParLed emulation requires Master, RGBWAUV and Strobe values')
        roles=['Red','Green','Blue','White','Amber','UV']
        slots=color_slots if isinstance(color_slots,dict) else {}
        for role in roles:
            slot=slots.get(role)
            if slot is not None and (type(slot) is not int or not 1<=slot<=14):raise ValueError('Color wheel slots must be 1–14 or unassigned')
        master=values[0];colors=dict(zip(roles,values[1:7]));role=max(roles,key=lambda key:colors[key])
        peak=colors[role];dimmer=(master*peak+127)//255
        wheel=0
        if role!='White' and peak:
            slot=slots.get(role)
            if slot is not None:wheel=8+(slot-1)*8+4
        strobe=values[7]
        shutter=(51+(strobe*189+127)//255) if strobe else (25 if dimmer else 0)
        # CH8 dimmer and CH10 color adjustment are provisional in the source map.
        return [128,128,0,0,0,0,shutter,dimmer,wheel,0,0,0,96,0,0,0]

    def set_fixture_control(self,ip,fixture,mode,values,action=None,universe=None,color_slots=None):
        with self.lock:
            if self.tx_blocked():raise ValueError('SOFTWARE CONTROL TX must be on')
            if not self.running or not self.socket:raise ValueError('Art-Net transmitter is unavailable')
            if type(universe) is not int or not 0<=universe<=255:raise ValueError('Controller output universe is unknown')
            if fixture.get('mappingPreset')!='beam230-16ch' or len(fixture.get('channels',[]))!=16:raise ValueError('Beam 230 16CH profile required')
            key=fixture['id'];current=self.fixture_controls.get(key)
            if action=='lamp-release':
                if current:
                    current['nativeValues'][15]=0;current['action']=None;current['actionDeadline']=None
                self.wake_routing()
                return dict(accepted=True,action='released',fixtureId=key)
            if values is None:
                if current:values=current['values'];mode=current['mode'];color_slots=current.get('colorSlots')
                else:values=[ch['default'] for ch in fixture['channels']];mode='original'
            native=list(current['nativeValues']) if current and action in ('lamp-on','safe-off','lamp-reset') else self._beam_native_values(mode,values,color_slots)
            if action not in (None,'lamp-on','safe-off','lamp-reset'):raise ValueError('Unknown Beam action')
            if action in ('lamp-on','safe-off','lamp-reset'):
                # Commands are one-shot, manually initiated, and auto-neutral after 4 seconds.
                native[15]={'lamp-on':202,'safe-off':102,'lamp-reset':255}[action]
                if action in ('lamp-on','safe-off'):native[6]=0;native[7]=0
                deadline=time.monotonic()+4
            else:deadline=None
            self.fixture_controls[key]=dict(fixtureId=key,destinationIp=ip,universe=universe,start=fixture['start'],
                mode=mode,values=list(values),colorSlots=dict(color_slots or {}),nativeValues=native,
                action=action,actionDeadline=deadline,
                manualUntil=deadline if deadline is not None else time.monotonic()+.75,updatedAt=timestamp())
            if deadline is not None:
                timer=threading.Timer(4, self._expire_fixture_command, args=(key,));timer.daemon=True;timer.start()
            self.wake_routing()
            return dict(accepted=True,fixtureId=key,mode=mode,action=action,values=native,expiresIn=5 if deadline else None)

    def _expire_fixture_command(self,key):
        with self.lock:
            item=self.fixture_controls.get(key)
            if not item or not item.get('actionDeadline') or time.monotonic()<item['actionDeadline']:return
            item['nativeValues'][15]=0;item['action']=None;item['actionDeadline']=None
        self.wake_routing()

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

    def blackout(self,ip,extra_frames=None,output_universe=None):
        with self.lock:
            result=dict(txMuted=True,zeroSent=False)
            try:
                if self.tx_blocked():return dict(result,error='TX already stopped; no additional packet sent')
                if not self.running or not self.socket:raise ValueError('Output socket unavailable')
                if ipaddress.ip_address(ip) not in ipaddress.ip_network(self.subnet):raise ValueError('Target outside subnet')
                if output_universe is not None:
                    if type(output_universe) is not int or not 0<=output_universe<=255:raise ValueError('Output universe unverified')
                    universe=output_universe
                else:
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

    def set_tx_muted(self,enabled,preserve_runtime=False):
        with self.lock:
            if not enabled and self.tx_permission is not None and not self.tx_permission():
                raise ValueError('TX requires fresh, confirmed SOFTWARE CONTROL')
            self.tx_muted=enabled
            if enabled:
                if not preserve_runtime:
                    self.manual_overrides.clear()
                    self.fixture_controls.clear()
                    self.fx_activations.clear()
                else:
                    # A transient verification loss may retain static levels,
                    # but must never resume a timed effect or lamp command.
                    for item in self.fx_activations.values():item['effect']=False
                    for item in self.fixture_controls.values():
                        if item.get('actionDeadline'):
                            item['actionDeadline']=None;item['action']=None
                            if len(item.get('nativeValues',[]))>=16:item['nativeValues'][15]=0
                if self.fx_sequence.get('active'):
                    self.fx_sequence.update(active=False,status='Stopped · controller verification lost' if preserve_runtime else 'Stopped · BLACKOUT',finishedAt=timestamp())
                if self.output_test['active']:self.test_cancelled=True
                self.routing.update(active=False,status='Controller verification lost · TX stopped' if preserve_runtime else 'Blackout · TX stopped')
            return enabled

    def set_fx_activation(self,ip,fixture,enabled,universe):
        with self.lock:
            if type(enabled) is not bool:raise ValueError('Activation state must be on or off')
            activate_offset,effect_offset=fx_channel_slots(fixture)
            if type(universe) is not int or not 0<=universe<=255:raise ValueError('Controller output universe is unknown')
            key=fixture['id']
            if enabled:
                if self.tx_blocked():raise ValueError('Confirmed SOFTWARE CONTROL TX is required')
                if self.output_test.get('active'):raise ValueError('Wait for the current fixture test to finish')
                self.fx_activations[key]=dict(fixtureId=key,name=fixture['name'],destinationIp=ip,
                    universe=universe,start=fixture['start'],channelCount=len(fixture['channels']),
                    activateOffset=activate_offset,effectOffset=effect_offset,
                    effect=False,activateValue=128,updatedAt=timestamp())
            else:
                self.fx_activations.pop(key,None)
                if self.fx_sequence.get('active') and key in self.fx_sequence.get('fixtureIds',[]):
                    self.fx_sequence.update(active=False,status='Stopped · fixture deactivated',finishedAt=timestamp())
                    for item in self.fx_activations.values():item['effect']=False
            self.wake_routing()
            return dict(activated=enabled,fixtureId=key,activateChannel=fixture['start']+activate_offset,
                        effectChannel=fixture['start']+effect_offset,activateValue=128 if enabled else 0,
                        effectValue=(255 if self.fx_activations.get(key,{}).get('effect') else 0))

    def _apply_sequence_inputs(self,ip,events,fixtures,universe):
        # Activation is an operator command on a VALUE CHANGE, not a state
        # rewritten by every desk/MIDI frame. Resolve simultaneous edits by
        # movement timestamp; apply all activations before evaluating triggers.
        for stamp,target,source,role,value,mode in sorted(events,key=lambda event:event[0]):
            if role!='fx activate':continue
            key=(ip,universe,target['id'],source)
            previous=self.sequence_input_values.get(key)
            self.sequence_input_values[key]=value
            if previous==value or (previous is None and value==0):continue
            enabled=True if value==255 else False if value<=245 else None
            self._set_sequence_fx_group(ip,target,fixtures,universe,enabled)
        for stamp,target,source,role,value,mode in events:
            if role in ('sequence trigger','master') and target.get('sequenceArmed'):
                self._sequence_trigger(ip,target,source,value,fixtures,universe,
                                       threshold=100 if role=='sequence trigger' else 255,trigger_mode=mode)

    def _set_sequence_fx_group(self,ip,target,fixtures,universe,enabled):
        """Drive a sequence's FX Activate fader as a gated group, without per-frame rewrites."""
        if enabled is None:return
        definition=target.get('sequenceDefinition') or {}
        by_id={fixture['id']:fixture for fixture in fixtures}
        selected=[by_id.get(fid) for fid in definition.get('steps',[])]
        if not selected or any(not fixture or fixture.get('mappingPreset')!='fx-activate-effect-4ch' for fixture in selected):
            return
        if enabled and self.tx_blocked():return
        if enabled and self.output_test.get('active'):return
        for fixture in selected:
            current=self.fx_activations.get(fixture['id'])
            matches=bool(current and current.get('destinationIp')==ip and current.get('universe')==universe
                         and current.get('start')==fixture['start'])
            if enabled and not matches:self.set_fx_activation(ip,fixture,True,universe)
            elif not enabled and current and current.get('destinationIp')==ip:self.set_fx_activation(ip,fixture,False,universe)

    def start_fx_sequence(self,ip,fixtures,fixture_ids,universe,effect_seconds,interval_seconds,
                          loop_while_held=False,trigger_state_key=None):
        with self.lock:
            if not self.running or not self.socket:raise ValueError('Art-Net transmitter is unavailable')
            if self.tx_blocked():raise ValueError('BLACKOUT is pressed or SOFTWARE CONTROL TX is unavailable')
            if self.output_test.get('active'):raise ValueError('Wait for the current fixture test to finish')
            if self.fx_sequence.get('active'):raise ValueError('An FX sequence is already running')
            if not isinstance(fixture_ids,list) or not 1<=len(fixture_ids)<=32:
                raise ValueError('Select 1–32 FX fixture steps')
            if type(effect_seconds) not in (int,float) or not .1<=effect_seconds<=30:raise ValueError('Effect duration must be 0.1–30 seconds')
            if type(interval_seconds) not in (int,float) or not 0<=interval_seconds<=30:raise ValueError('Start interval must be 0–30 seconds')
            by_id={f['id']:f for f in fixtures}
            selected=[by_id.get(fid) for fid in fixture_ids]
            if any(not f or f.get('mappingPreset')!='fx-activate-effect-4ch' or f['universe']!=universe for f in selected):
                raise ValueError('Sequence targets must use the FX profile in the confirmed output universe')
            for fixture in selected:
                activate_offset,effect_offset=fx_channel_slots(fixture)
                armed=self.fx_activations.get(fixture['id'])
                if armed and (armed['start']!=fixture['start'] or armed['universe']!=universe or
                              armed['activateOffset']!=activate_offset or armed['effectOffset']!=effect_offset):
                    raise ValueError('FX channel map changed; switch Activate off and on for '+fixture['name'])
            inactive=[f['name'] for f in selected if f['id'] not in self.fx_activations or self.fx_activations[f['id']].get('destinationIp')!=ip]
            if inactive:raise ValueError('Activate every selected FX fixture first: '+', '.join(inactive))
            self.fx_sequence=dict(active=True,status='Starting',destinationIp=ip,universe=universe,
                                  fixtureIds=list(fixture_ids),fixtureIndex=0,totalFixtures=len(selected),
                                  effectSeconds=float(effect_seconds),intervalSeconds=float(interval_seconds),
                                  triggerMode='loop' if loop_while_held else 'oneshot',
                                  activeFixtureIds=[],cycleCount=1,startedAt=timestamp(),error=None)
            thread=threading.Thread(target=self._fx_sequence_loop,
                                    args=(selected,float(effect_seconds),float(interval_seconds),loop_while_held,trigger_state_key),daemon=True)
            thread.start()
            return copy.deepcopy(self.fx_sequence)

    def _sequence_trigger(self,ip,target,source_key,value,fixtures,universe,threshold=255,trigger_mode=None):
        """Start an armed virtual FX sequence on a rising edge; held input never retriggers a one-shot."""
        state_key=(ip,target.get('sequenceId'),source_key)
        high=self.sequence_trigger_high.get(state_key,False)
        if value<threshold:
            self.sequence_trigger_high[state_key]=False
            return
        if high:
            return
        self.sequence_trigger_high[state_key]=True
        if not target.get('sequenceArmed'):
            return
        definition=target.get('sequenceDefinition') or {}
        if not definition.get('steps'):
            return
        try:
            self.start_fx_sequence(ip,fixtures,definition['steps'],universe,
                                   definition.get('effectSeconds',1),definition.get('intervalSeconds',1),
                                   loop_while_held=(trigger_mode=='loop' if trigger_mode in ('once','loop') else definition.get('triggerMode')=='loop'),
                                   trigger_state_key=state_key)
        except ValueError as exc:
            # A blocked/overlapping run consumes this edge; release and raise
            # again to deliberately retry after the condition is cleared.
            self.fx_sequence.update(triggerStatus='Trigger ignored · '+str(exc))

    def stop_fx_sequence(self):
        with self.lock:
            if self.fx_sequence.get('active'):
                self.fx_sequence.update(active=False,status='Stopped by operator · all FX channels 2 off',phase='Off',finishedAt=timestamp())
            for item in self.fx_activations.values():item['effect']=False
            self.wake_routing()
            return copy.deepcopy(self.fx_sequence)

    def _fx_sequence_loop(self,fixtures,effect_seconds,interval_seconds,loop_while_held=False,trigger_state_key=None):
        error=None
        try:
            started_at=time.monotonic();next_index=0;active_until={}
            trigger_released=False
            while True:
                with self.lock:
                    if not self.fx_sequence.get('active') or self.tx_blocked():break
                    if loop_while_held and not self.sequence_trigger_high.get(trigger_state_key,False):
                        trigger_released=True;break
                now=time.monotonic()
                while next_index<len(fixtures) and now>=started_at+next_index*interval_seconds:
                    fixture=fixtures[next_index]
                    with self.lock:
                        if not self.fx_sequence.get('active') or self.tx_blocked():break
                        current=self.fx_activations.get(fixture['id'])
                        if not current:raise ValueError('Fixture was deactivated during the sequence')
                        current['effect']=True
                        active_until[fixture['id']]=now+effect_seconds
                        next_index+=1
                        active_ids=[fid for fid in active_until if self.fx_activations.get(fid,{}).get('effect')]
                        self.fx_sequence.update(fixtureIndex=next_index,fixtureName=fixture['name'],
                            activeFixtureIds=active_ids,phase='Effect ON',status='Running')
                        self.wake_routing()
                    now=time.monotonic()
                with self.lock:
                    if not self.fx_sequence.get('active') or self.tx_blocked():break
                    if loop_while_held and not self.sequence_trigger_high.get(trigger_state_key,False):
                        trigger_released=True;break
                    expired=[fid for fid,deadline in active_until.items() if now>=deadline]
                    for fid in expired:
                        item=self.fx_activations.get(fid)
                        if item:item['effect']=False
                        active_until.pop(fid,None)
                    active_ids=[fid for fid in active_until if self.fx_activations.get(fid,{}).get('effect')]
                    if expired:
                        self.fx_sequence.update(activeFixtureIds=active_ids,phase='Effect ON' if active_ids else 'Waiting')
                        self.wake_routing()
                if next_index>=len(fixtures) and not active_until:
                    if loop_while_held:
                        with self.lock:
                            if not self.sequence_trigger_high.get(trigger_state_key,False):
                                trigger_released=True;break
                            started_at=time.monotonic()+interval_seconds
                            next_index=0
                            self.fx_sequence.update(fixtureIndex=0,cycleCount=self.fx_sequence.get('cycleCount',1)+1,
                                                    phase='Next cycle')
                    else:break
                next_start=started_at+next_index*interval_seconds if next_index<len(fixtures) else float('inf')
                next_end=min(active_until.values(),default=float('inf'))
                self.stop_event.wait(min(.05,max(0,min(next_start,next_end)-time.monotonic())))
            with self.lock:
                for item in self.fx_activations.values():item['effect']=False
                if self.fx_sequence.get('active'):
                    status='Stopped · trigger released' if trigger_released else 'Complete · all FX channels 2 off'
                    self.fx_sequence.update(active=False,status=status,phase='Off',activeFixtureIds=[],finishedAt=timestamp())
                elif self.tx_blocked():
                    self.fx_sequence.update(active=False,status='Stopped · BLACKOUT',phase='Off',finishedAt=timestamp())
                self.wake_routing()
        except (ValueError,OSError) as exc:
            error=str(exc)
            with self.lock:
                for item in self.fx_activations.values():item['effect']=False
                self.fx_sequence.update(active=False,status='Failed · all FX channels 2 off',phase='Off',activeFixtureIds=[],error=error,finishedAt=timestamp())
                self.wake_routing()

    def snapshot(self):
        with self.lock:
            t = time.monotonic()
            streams = []
            for s in self.streams.values():
                clean = {k: v for k, v in s.items() if not k.startswith('_')}
                clean.update(ageSeconds=round(t - s['_last'], 2), live=t - s['_last'] < DESK_HOLD_SECONDS,
                             packetsPerSecond=sum(x >= t - 1 for x in s['_times']),
                             changedChannels=[ch for ch, when in s['_changed'].items() if t - when <= 1.5],
                             nonzeroChannels=[i + 1 for i, v in enumerate(s['values']) if v])
                streams.append(clean)
            nodes = []
            for n in self.nodes.values():
                clean = {k: v for k, v in n.items() if not k.startswith('_')}
                clean['ageSeconds'] = round(t - n['_last'], 2)
                nodes.append(clean)
            routing=dict(self.routing)
            if self.route_latency_samples:
                ordered=sorted(value for _,value in self.route_latency_samples)
                routing['appLatencyMs']={
                    'samples':len(ordered),
                    'min':round(ordered[0],2),
                    'median':round(statistics.median(ordered),2),
                    'p95':round(ordered[min(len(ordered)-1,int((len(ordered)-1)*.95))],2),
                    'max':round(ordered[-1],2),
                    'windowSeconds':round(min(60,self.route_latency_samples[-1][0]-self.route_latency_samples[0][0]),1),
                }
            else:routing['appLatencyMs']=None
            return dict(listening=self.running, error=self.error, udpPort=self.port,
                        subnet=self.subnet, hostIp=self.host_ip, startedAt=self.started,
                        receivedAt=timestamp(), counters=dict(self.counters), streams=streams,
                        nodes=nodes, events=list(self.events), inputUniverse=self.input_universe,toolTxByIp=dict(self.tool_tx_by_ip),blackout=dict(self.blackout_status),manualOverrides=list(self.manual_overrides.values()),outputEnabled=not self.tx_blocked(),txMuted=self.tx_blocked(),
                        outputTest=dict(self.output_test),toolTx=dict(self.tool_tx),routing=routing,physicalOutputFeedback=None,
                        fxActivations=copy.deepcopy(self.fx_activations),fxSequence=copy.deepcopy(self.fx_sequence),
                        logging=self.logger.status() if self.logger else None,
                        subscriptions=dict(self.subscriptions),
                        wizRouting=dict(self.wiz_routing_status),
                        visibility='Only Art-Net packets delivered to this Mac are visible. Route the input stream to this Mac; other unicast traffic is not captured.')
