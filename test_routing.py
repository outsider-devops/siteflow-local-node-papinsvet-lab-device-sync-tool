import unittest,time
from unittest.mock import Mock
from artnet_monitor import ArtNetMonitor,parse_packet
from fixtures import initial_patch
class RoutingTests(unittest.TestCase):
 def setup_monitor(self):
  m=ArtNetMonitor();m.running=True;m.socket=Mock()
  m.nodes['192.168.18.80']={'_last':time.monotonic(),'ports':[{'output':True,'outputUniverse':1}]}
  base=[0]*192;base[0]=17;base[80:88]=[255,70,20,10,30,40,50,60]
  m._fixture_baseline=Mock(return_value=base)
  return m
 def test_semantic_forwarding_and_final_off(self):
  m=self.setup_monitor();fixtures=initial_patch('00:00:c8:d8:a2:71')
  c=dict(id='desk-spare-81',enabled=True,universe=1,start=81,targetFixtures=['jellyfish-1'])
  m.route_tick('192.168.18.80',c,fixtures)
  values=parse_packet(m.socket.sendto.call_args.args[0])['values']
  self.assertEqual(values[0],0)
  self.assertEqual(values[192:],[255,70,20,10,30,60])
  self.assertTrue(m.routing['active']);self.assertEqual(m.tool_tx['values'],values)
  m._fixture_baseline.side_effect=ValueError('Input stale')
  m.route_tick('192.168.18.80',c,fixtures)
  self.assertEqual(parse_packet(m.socket.sendto.call_args.args[0])['values'][192:],[0]*6)
  self.assertFalse(m.routing['active']);count=m.socket.sendto.call_count
  m.route_tick('192.168.18.80',c,fixtures);self.assertEqual(m.socket.sendto.call_count,count)
 def test_no_targets_and_test_exclusion(self):
  m=self.setup_monitor();m.route_tick('192.168.18.80',None,[]);m.socket.sendto.assert_not_called()
  m.output_test['active']=True;m.route_tick('192.168.18.80',{},[]);m.socket.sendto.assert_not_called()
 def test_dynamic_function_on_page_b_and_multiple_inputs(self):
  m=self.setup_monitor();base=[0]*192;base[88]=123;base[112]=77;m._fixture_baseline=Mock(return_value=base)
  fixtures=[dict(id='beam',name='Beam',universe=1,start=201,channels=[dict(function='Pan',default=0)]),dict(id='lamp',name='Lamp',universe=1,start=211,channels=[dict(function='Red',default=0)])]
  a=dict(id='a',enabled=True,universe=1,start=81,targetFixtures=['beam'],faderFunctions=[None]*8+['Pan']+[None]*7)
  b=dict(id='b',enabled=True,universe=1,start=113,targetFixtures=['lamp'],faderFunctions=['Red']+[None]*15)
  m.route_tick('192.168.18.80',[a,b],fixtures)
  frame=parse_packet(m.socket.sendto.call_args.args[0])['values'];self.assertEqual(frame[200],123);self.assertEqual(frame[210],77)
  self.assertTrue(m.routing['active'])

 def test_first_fader_red_routes_by_function_not_channel_number(self):
  m=self.setup_monitor();base=[0]*192;base[80]=149;m._fixture_baseline=Mock(return_value=base)
  fixtures=[dict(id='a',name='Red at CH4',universe=1,start=193,channels=[{'function':name,'default':0} for name in ['Master','Green','Blue','Red']]),dict(id='b',name='Red at CH2',universe=1,start=204,channels=[{'function':name,'default':0} for name in ['Master','Red']])]
  c=dict(id='test',enabled=True,universe=1,start=81,targetFixtures=['a','b'],faderFunctions=['Red']+[None]*15)
  m.route_tick('192.168.18.80',c,fixtures)
  frame=parse_packet(m.socket.sendto.call_args.args[0])['values']
  self.assertEqual(frame[195],149);self.assertEqual(frame[204],149)
  self.assertEqual(frame[192],0);self.assertEqual(frame[203],0)

 def test_direct_card_preserves_unassigned_fixture_channels(self):
  m=self.setup_monitor();base=[31]*192;base[80]=149;m._fixture_baseline=Mock(return_value=base)
  fixture=dict(id='p',name='Parlight',universe=1,start=1,channels=[{'function':name,'default':0} for name in ['Master','Red','Green','Blue','White','Amber','UV','Unknown','Unknown','Unknown','Unknown','Strobe']])
  c=dict(id='test',enabled=True,universe=1,start=81,targetFixtures=['p'],faderFunctions=['Red']+[None]*15)
  direct=dict(id='direct',routingMode='direct',enabled=True,universe=1,start=1,count=16,targetFixtures=[])
  m.route_tick('192.168.18.80',[direct,c],[fixture]);v=parse_packet(m.socket.sendto.call_args.args[0])['values']
  self.assertEqual(v[1],149);self.assertEqual(v[0],31);self.assertEqual(v[7],31);self.assertEqual(v[11],31)
 def test_blackout_stops_every_send_without_zero_frame(self):
  m=self.setup_monitor();fixtures=initial_patch('00:00:c8:d8:a2:71');c=dict(id='c',enabled=True,universe=1,start=81,targetFixtures=['jellyfish-1'])
  m.route_tick('192.168.18.80',c,fixtures);count=m.socket.sendto.call_count
  m.set_tx_muted(True)
  m.route_tick('192.168.18.80',c,fixtures)
  m.advertise('192.168.18.80')
  with self.assertRaises(ValueError):m.poll('192.168.18.80')
  with self.assertRaises(ValueError):m._fixture_send('192.168.18.80',1,bytes(2),1)
  self.assertEqual(m.socket.sendto.call_count,count)
  m.set_tx_muted(False);m.route_tick('192.168.18.80',c,fixtures)
  self.assertEqual(m.socket.sendto.call_count,count+1)

 def test_blackout_sends_all_512_zeros_then_stops(self):
  m=self.setup_monitor();fixtures=initial_patch('00:00:c8:d8:a2:71');c=dict(id='c',enabled=True,universe=1,start=81,targetFixtures=['jellyfish-1'])
  m.route_tick('192.168.18.80',c,fixtures)
  m.output_test['active']=True
  result=m.blackout('192.168.18.80');count=m.socket.sendto.call_count
  self.assertTrue(result['zeroSent']);self.assertTrue(m.tx_muted);self.assertTrue(m.test_cancelled)
  frame=parse_packet(m.socket.sendto.call_args.args[0]);self.assertEqual(frame['values'],[0]*512);self.assertEqual(frame['universe'],1)
  m.route_tick('192.168.18.80',c,fixtures)
  with self.assertRaises(ValueError):m._fixture_send('192.168.18.80',1,bytes(512),1)
  self.assertEqual(m.socket.sendto.call_count,count)
 def test_blackout_send_failure_still_stops_tx(self):
  m=self.setup_monitor();m.socket.sendto.side_effect=OSError('Send failed')
  result=m.blackout('192.168.18.80');self.assertFalse(result['zeroSent']);self.assertTrue(m.tx_muted);self.assertIn('failed',result['error'])

 def test_explicit_remap_can_replace_direct_value_with_zero(self):
  m=self.setup_monitor();base=[0]*192;base[0]=90;base[1]=0;base[80]=0;base[81]=130;m._fixture_baseline=Mock(return_value=base)
  fixture=dict(id='p',name='Direct desk fixture',universe=1,start=1,channels=[{'function':'Master','default':255},{'function':'Red','default':0}])
  c=dict(id='test',enabled=True,universe=1,start=81,targetFixtures=['p'],faderFunctions=['Master','Red']+[None]*14)
  m.route_tick('192.168.18.80',c,[fixture]);v=parse_packet(m.socket.sendto.call_args.args[0])['values']
  self.assertEqual(v[:2],[0,130])
  base[0]=0;base[81]=0;m.route_tick('192.168.18.80',c,[fixture]);v=parse_packet(m.socket.sendto.call_args.args[0])['values'];self.assertEqual(v[:2],[0,0])

 def test_input_universe_one_routes_to_output_universe_zero(self):
  m=self.setup_monitor();m.nodes['192.168.18.80']['ports'][0]['outputUniverse']=0
  fixture=dict(id='p',name='Lamp',universe=0,start=1,channels=[dict(function='Master',default=0)])
  c=dict(id='test',enabled=True,universe=1,start=81,targetFixtures=['p'],faderFunctions=['Master']+[None]*15)
  m.route_tick('192.168.18.80',c,[fixture]);packet=parse_packet(m.socket.sendto.call_args.args[0])
  self.assertEqual(packet['universe'],0);self.assertEqual(packet['values'][0],255);self.assertEqual(m.routing['inputUniverse'],1)
 def test_mapped_bank_does_not_leak_its_source_range_to_output(self):
  m=self.setup_monitor();base=[31]*192;base[80]=123;m._fixture_baseline=Mock(return_value=base)
  fixture=dict(id='high',name='High',universe=1,start=193,channels=[dict(function='Master',default=0)])
  c=dict(id='mapped',routingMode='mapped',enabled=True,universe=1,start=81,count=16,targetFixtures=['high'],faderFunctions=['Master']+[None]*15)
  m.route_tick('192.168.18.80',c,[fixture]);v=parse_packet(m.socket.sendto.call_args.args[0])['values']
  self.assertEqual(v[80:96],[0]*16);self.assertEqual(v[192],123)

 def test_mapped_bank_without_targets_has_no_output_even_with_other_direct_cards(self):
  m=self.setup_monitor();base=[0]*192;base[144]=211;m._fixture_baseline=Mock(return_value=base)
  direct=dict(id='direct',routingMode='direct',enabled=True,universe=1,start=1,count=16,targetFixtures=[])
  empty=dict(id='input10',routingMode='mapped',enabled=True,universe=1,start=145,count=16,targetFixtures=[],faderFunctions=['Master']+[None]*15)
  m.route_tick('192.168.18.80',[direct,empty],[]);v=parse_packet(m.socket.sendto.call_args.args[0])['values']
  self.assertEqual(v[144],0)

 def test_lamp_state_does_not_override_input_faders(self):
  fixture=dict(id='beam',name='Beam',universe=1,start=145,mappingPreset='beam230-16ch',channels=[dict(function='Unknown',default=0) for _ in range(16)])
  control=dict(id='ten',routingMode='direct',enabled=True,universe=1,start=145,count=16,targetFixtures=[])
  for phase in ('off-pulse','cooling','on-pulse','ignition-wait'):
   with self.subTest(phase=phase):
    m=self.setup_monitor();base=[0]*192;base[150]=255;base[151]=137;m._fixture_baseline=Mock(return_value=base)
    m.beam_lamp_states['beam']=dict(phase=phase)
    m.route_tick('192.168.18.80',[control],[fixture])
    frame=parse_packet(m.socket.sendto.call_args.args[0])['values']
    self.assertEqual(frame[150:152],[255,137])

 def test_empty_fixture_deadlines_do_not_stop_routing(self):
  m=self.setup_monitor();m.fixture_controls['beam']=dict(destinationIp='192.168.18.80',manualUntil=None,actionDeadline=None)
  fixture=dict(id='p',name='Lamp',universe=1,start=193,channels=[dict(function='Red',default=0)])
  c=dict(id='desk',enabled=True,universe=1,start=81,targetFixtures=['p'],faderFunctions=['Red']+[None]*15)
  m.route_tick('192.168.18.80',c,[fixture]);self.assertTrue(m.routing['active']);m.socket.sendto.assert_called_once()

 def test_fixture_mouse_is_temporary_with_direct_input(self):
  m=self.setup_monitor();base=[0]*192;base[150]=255;base[151]=99;m._fixture_baseline=Mock(return_value=base)
  fixture=dict(id='beam',name='Beam',universe=1,start=145,mappingPreset='beam230-16ch',channels=[dict(function='Unknown',default=0) for _ in range(16)])
  direct=dict(id='ten',routingMode='direct',enabled=True,universe=1,start=145,count=16,targetFixtures=[])
  native=[0]*16;native[6]=25;native[7]=150
  m.fixture_controls['beam']=dict(destinationIp='192.168.18.80',start=145,nativeValues=native,manualUntil=time.monotonic()+.75,actionDeadline=None)
  m.route_tick('192.168.18.80',[direct],[fixture]);self.assertEqual(parse_packet(m.socket.sendto.call_args.args[0])['values'][150:152],[25,150])
  m.fixture_controls['beam']['manualUntil']=0
  m.route_tick('192.168.18.80',[direct],[fixture]);self.assertEqual(parse_packet(m.socket.sendto.call_args.args[0])['values'][150:152],[255,99])
