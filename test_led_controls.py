import unittest,time
from unittest.mock import Mock
from led_controls import defaults,controller_frames,LEDRuntime,virtual_fixture,validate_mapping
from artnet_monitor import ArtNetMonitor,parse_packet
class LEDTests(unittest.TestCase):
 def test_strips_order_capacity_and_complete_span(self):
  f=defaults()[0];frames=controller_frames([f],{f['id']:[255,255,0,0,0,0,0,0]},0)
  self.assertEqual(len(frames),8)
  self.assertEqual(frames[(f['controllerIp'],1)][:6],[0,32,0,0,32,0])
  self.assertEqual(frames[(f['controllerIp'],1)][432:],[0]*80)
  self.assertTrue(all(v==0 for u in range(2,9) for v in frames[(f['controllerIp'],u)]))
 def test_matrix_600_slots_mirrored(self):
  f=defaults()[-1];frames=controller_frames([f],{f['id']:[255,0,255,0,0,0,0,0]},0)
  for u in [16,18,20,22]:
   self.assertEqual(frames[(f['controllerIp'],u)][:6],[0,0,64,0,0,64])
   self.assertEqual(frames[(f['controllerIp'],u+1)][88:],[0]*424)
 def test_virtual_functions_and_editable_mapping(self):
  f=defaults()[0];f=validate_mapping({'mapping':['Red','Master','Strobe'],'limit':10},f)
  self.assertEqual(virtual_fixture(f)['channels'][0]['function'],'Red')
  with self.assertRaises(ValueError):validate_mapping({'mapping':['Red','Red']},f)
 def test_input_routes_to_led_by_function(self):
  f=defaults()[0];m=ArtNetMonitor();m._fixture_baseline=Mock(return_value=[0]*80+[100,255]+[0]*110)
  c=dict(id='input',universe=1,start=81,enabled=True,targetFixtures=[f['id']],faderFunctions=['Red','Master']+[None]*14)
  values=LEDRuntime().values([f],[c],m,'192.168.18.80')
  self.assertEqual(values[f['id']][:3],[255,100,0])
 def test_pixel_sender_and_global_mute(self):
  m=ArtNetMonitor();m.socket=Mock();f=defaults()[0];frames=controller_frames([f],{f['id']:[0]*8},0)
  self.assertTrue(m.send_pixel_frames(frames,12));self.assertEqual(m.socket.sendto.call_count,9)
  self.assertEqual(parse_packet(m.socket.sendto.call_args_list[0].args[0])['sequence'],12)
  m.set_tx_muted(True);self.assertFalse(m.send_pixel_frames(frames,13));self.assertEqual(m.socket.sendto.call_count,9)
