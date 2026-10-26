import unittest,time
from unittest.mock import Mock
from midi_inputs import MIDIInputs,LastChange,validate_section,mapped_values,identity
from artnet_monitor import ArtNetMonitor,parse_packet

class MIDIInputTests(unittest.TestCase):
 def setUp(self):
  self.binding=dict(source='Traktor',type='Control Change',channel=1,number=2,name='Left jog',interpretation="Relative encoder / two's complement")
  self.fixture=dict(id='beam',name='Beam',universe=0,start=145,channels=[dict(function='Pan',default=128),dict(function='Tilt',default=128)])
  self.section=validate_section(dict(name='Jog Pan Tilt',enabled=True,slots=[dict(binding=identity(self.binding),step=1)]+[None]*15,faderFunctions=['Pan']+[None]*15,targetFixtures=['beam'],targetModes={'beam':'original'}),[],[self.binding],[self.fixture],'midi')
  self.runtime=MIDIInputs();self.runtime.configure({'node':[self.section]},[dict(id='midi',midiControls=[self.binding])])
 def test_relative_jog_midpoint_clamp_manual_takeover(self):
  self.assertEqual(mapped_values(self.runtime.routing_controls('node'),[self.fixture]),{})
  self.runtime.ingest([dict(source='Traktor',bytes=[176,2,1],monotonic=10)])
  self.assertEqual(self.runtime.snapshot()['sections'][self.section['id']]['values'][0],129)
  self.runtime.manual(self.section['id'],1,20)
  self.runtime.ingest([dict(source='Traktor',bytes=[176,2,127],monotonic=time.monotonic()+1)])
  self.assertEqual(self.runtime.snapshot()['sections'][self.section['id']]['values'][0],19)
 def test_last_movement_zero_and_unchanged_frames(self):
  mix=LastChange();key=('dmx',144)
  self.assertEqual(mix.mix({key:30},{key:(80,2,'midi')},{key:1})[key],80)
  self.assertEqual(mix.mix({key:30},{key:(80,2,'midi')},{key:1})[key],80)
  self.assertEqual(mix.mix({key:0},{key:(80,2,'midi')},{key:3})[key],0)
  self.assertEqual(mix.mix({key:0},{key:(0,4,'midi')},{key:3})[key],0)
 def test_midi_routes_without_live_desk(self):
  m=ArtNetMonitor();m.running=True;m.socket=Mock();m._fixture_baseline=Mock(side_effect=ValueError('No desk'))
  self.runtime.ingest([dict(source='Traktor',bytes=[176,2,1],monotonic=10)])
  m.route_tick('192.168.18.80',[],[self.fixture],output_universe=0,midi_controls=self.runtime.routing_controls('node'))
  self.assertTrue(m.routing['active'],m.routing)
  packet=parse_packet(m.socket.sendto.call_args.args[0]);self.assertEqual(packet['universe'],0);self.assertEqual(packet['values'][144],129)
 def test_mapping_removed_releases_output(self):
  self.runtime.ingest([dict(source='Traktor',bytes=[176,2,1])])
  self.runtime.configure({'node':[self.section]},[dict(id='midi',midiControls=[])])
  self.assertFalse(self.runtime.snapshot()['sections'][self.section['id']]['active'][0])
 def test_slot_validation_rejects_missing_mapping(self):
  with self.assertRaises(ValueError):validate_section(self.section,[self.section],[],[self.fixture],'midi')
