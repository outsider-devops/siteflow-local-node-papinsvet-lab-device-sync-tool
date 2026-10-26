import struct,time,unittest
from unittest.mock import Mock
from artnet_monitor import ArtNetMonitor,ARTNET_ID,parse_packet
from fixtures import initial_patch

def packet(values):return ARTNET_ID+struct.pack('<H',0x5000)+b'\x00\x0e'+bytes([1,1,1,0])+struct.pack('>H',len(values))+bytes(values)
class ManualFaderTests(unittest.TestCase):
 def setup_m(self):
  m=ArtNetMonitor();m.running=True;m.socket=Mock();m.nodes['192.168.18.80']={'_last':time.monotonic(),'ports':[{'output':True,'outputUniverse':1}]}
  raw=[0]*192;raw[80]=20;m.ingest(packet(raw),('192.168.18.80',2048))
  c=dict(id='c',enabled=True,start=81,count=16,universe=1,targetFixtures=['jellyfish-1'],faderFunctions=['Master']+[None]*15)
  return m,c,raw
 def test_manual_then_desk_wins_on_change(self):
  m,c,raw=self.setup_m();fixtures=initial_patch('00:00:c8:d8:a2:71')
  m.set_manual_fader('192.168.18.80',c,1,149,20);m.route_tick('192.168.18.80',c,fixtures)
  self.assertEqual(parse_packet(m.socket.sendto.call_args.args[0])['values'][192],149)
  raw[81]=33;m.ingest(packet(raw),('192.168.18.80',2048));self.assertIn(('c',1),m.manual_overrides)
  raw[80]=21;m.ingest(packet(raw),('192.168.18.80',2048));self.assertNotIn(('c',1),m.manual_overrides)
  m.route_tick('192.168.18.80',c,fixtures);self.assertEqual(parse_packet(m.socket.sendto.call_args.args[0])['values'][192],21)
  with self.assertRaises(ValueError):m.set_manual_fader('192.168.18.80',c,1,150,20)
 def test_stale_and_blackout_clear_manual(self):
  m,c,raw=self.setup_m();m.set_manual_fader('192.168.18.80',c,1,149)
  m.set_tx_muted(True);self.assertFalse(m.manual_overrides)
  with self.assertRaises(ValueError):m.set_manual_fader('192.168.18.80',c,1,149)
  m.set_tx_muted(False);m.set_manual_fader('192.168.18.80',c,1,149)
  next(iter(m.streams.values()))['_last']-=3
  m.route_tick('192.168.18.80',c,initial_patch('00:00:c8:d8:a2:71'));self.assertFalse(m.manual_overrides)

 def test_standalone_manual_routes_and_first_input_reclaims(self):
  m,c,raw=self.setup_m();m.streams.clear()
  m.set_manual_fader('192.168.18.80',c,1,117,expected_offline=True)
  self.assertIsNone(m.manual_overrides[('c',1)]['anchor'])
  m.route_tick('192.168.18.80',c,initial_patch('00:00:c8:d8:a2:71'))
  self.assertEqual(parse_packet(m.socket.sendto.call_args.args[0])['values'][192],117)
  self.assertEqual(m.routing['status'],'Manual · desk offline')
  raw[80]=40;m.ingest(packet(raw),('192.168.18.80',2048))
  self.assertFalse(m.manual_overrides)
  m.route_tick('192.168.18.80',c,initial_patch('00:00:c8:d8:a2:71'))
  self.assertEqual(parse_packet(m.socket.sendto.call_args.args[0])['values'][192],40)
  with self.assertRaises(ValueError):m.set_manual_fader('192.168.18.80',c,1,118,expected_offline=True)

 def test_offline_manual_zero_not_held_by_stale_direct_value(self):
  m,c,raw=self.setup_m();raw[0]=200;m.ingest(packet(raw),('192.168.18.80',2048));next(iter(m.streams.values()))['_last']-=3
  fixture=dict(id='p',name='Lamp',start=1,universe=1,channels=[dict(function='Master',default=255)])
  c['targetFixtures']=['p'];m.set_manual_fader('192.168.18.80',c,1,0,expected_offline=True)
  m.route_tick('192.168.18.80',c,[fixture]);self.assertEqual(parse_packet(m.socket.sendto.call_args.args[0])['values'][0],0)
