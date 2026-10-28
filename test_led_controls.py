import unittest,time
from unittest.mock import Mock
from led_controls import defaults,controller_frames,LEDRuntime,virtual_fixture,validate_mapping
from artnet_monitor import ArtNetMonitor,parse_packet
class LEDTests(unittest.TestCase):
 def test_strobe_255_is_steady_in_parled_and_pixel_modes(self):
  from led_controls import pixel_payload
  strip=defaults()[0]
  matrix=defaults()[-1]
  steady=[255,255,0,0,0,0,0,255]
  for fixture in (strip,matrix):
   self.assertEqual(pixel_payload(fixture,steady,.01),pixel_payload(fixture,steady,.06))
   pulsing=steady[:-1]+[254]
   self.assertNotEqual(pixel_payload(fixture,pulsing,.01),pixel_payload(fixture,pulsing,.06))
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
 def test_canvas_routing_uses_controller_mac_and_its_color_order(self):
  f=defaults()[-1];values={f['id']:[255,255,0,0,0,0,0,0]}
  canvas=dict(id='single-pixel',controllerId=f['controllerId'],controllerIp=f['controllerIp'],port=1,
              startPixel=1,width=1,height=1,pixelCount=1,firstUniverse=40,universeCount=1,
              order='linear',startCorner='bottom-left',scanAxis='horizontal',colorOrder='GRB',enabled=True)
  frames=controller_frames([f],values,0,canvases=[canvas])
  self.assertEqual(frames[(f['controllerIp'],40)][:3],[0,64,0])
  wrong_controller=dict(canvas,controllerId='aa:bb:cc:dd:ee:ff',firstUniverse=41)
  frames=controller_frames([f],values,0,canvases=[wrong_controller])
  self.assertNotIn((f['controllerIp'],41),frames)
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
 def test_matrix_pixel_fill_height_edge_master_and_reverse(self):
  from led_controls import pixel_payload
  f=dict(defaults()[-1],count=10,order='RGB',limit=255)
  values=[255,128,0,0,0,0,0,0]
  red=pixel_payload(f,values,0)[::3]
  self.assertEqual(red[:5],[255]*5);self.assertAlmostEqual(red[5],5,delta=1);self.assertEqual(red[6:],[0]*4)
  self.assertEqual(pixel_payload(dict(f,reverse=True),values,0)[::3],list(reversed(red)))
  self.assertEqual(pixel_payload(f,[0,255,255,255,255,0,0,0],0),[0]*30)
  self.assertEqual(pixel_payload(f,[255,0,0,0,255,0,0,0],0),[255]*30)
  self.assertEqual(pixel_payload(f,[255,0,0,0,0,0,0,0],0),[0]*30)
 def test_pixel_fill_restricted_to_matrix(self):
  with self.assertRaises(ValueError):validate_mapping(dict(mapping=['Master','Red'],renderMode='pixel-fill'),defaults()[0])
 def test_matrix_target_mode_tracks_latest_source(self):
  f=defaults()[-1];m=ArtNetMonitor();t=time.monotonic();m._fixture_baseline=Mock(return_value=[255,128]+[0]*190)
  m.streams['desk']=dict(sourceIp='192.168.18.80',universe=0,_last=t,_changed={1:t,2:t})
  c=dict(id='eight',start=1,count=16,universe=0,enabled=True,targetFixtures=[f['id']],targetModes={f['id']:'parled'},faderFunctions=['Master','Red']+[None]*14)
  runtime=LEDRuntime();values=runtime.values([f],[c],m,'192.168.18.80')
  self.assertEqual(runtime.render_modes[f['id']],'parled')
  uniform=controller_frames([f],values,0,runtime.render_modes)[(f['controllerIp'],16)]
  self.assertEqual(uniform[:3],uniform[300:303])
  c['targetModes'][f['id']]='pixel-fill';values=runtime.values([f],[c],m,'192.168.18.80')
  fill=controller_frames([f],values,0,runtime.render_modes)[(f['controllerIp'],16)]
  self.assertEqual(runtime.render_modes[f['id']],'pixel-fill');self.assertNotEqual(fill[:3],fill[450:453])
 def test_matrix_parled_target_is_not_beam_conversion(self):
  from midi_inputs import mapped_values
  f=defaults()[-1];c=dict(id='midi',enabled=True,targetFixtures=[f['id']],targetModes={f['id']:'parled'},faderFunctions=['Red']+[None]*15,values=[128]+[0]*15,changed=[10]+[0]*15)
  self.assertEqual(mapped_values([c],[virtual_fixture(f)])[('virtual',f['id'],'Red')][0],128)
