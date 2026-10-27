"""Exercise competing live desk/MIDI sources with a fake Art-Net socket."""
import time
import unittest
from unittest.mock import Mock, patch

from artnet_monitor import ArtNetMonitor, parse_packet
from sequence_profiles import make_virtual_fixtures
from input_controls import validate_control
from midi_inputs import validate_section


class FXInputRoutingTests(unittest.TestCase):
    def setUp(self):
        self.ip = '192.168.18.80'
        self.monitor = ArtNetMonitor()
        self.monitor.running = True
        self.monitor.socket = Mock()
        self.fx = [dict(id='fx'+str(i), name='FX '+str(i+1), universe=1,
                        start=289+4*i, mappingPreset='fx-activate-effect-4ch',
                        channels=[dict(function=role) for role in
                                  ('Activate', 'Effect', 'Unknown', 'Unknown')])
                   for i in range(6)]
        sequence = dict(id='walk', name='FX Walk Test', kind='fx-walk', armed=True,
                        steps=[f['id'] for f in self.fx], effectSeconds=.1,
                        intervalSeconds=.1, triggerMode='oneshot')
        self.target = make_virtual_fixtures([sequence], 1)[0]
        self.fixtures = self.fx+[self.target]
        self.values = [0]*192
        self.monitor._fixture_baseline = Mock(return_value=self.values)
        self.desk = dict(id='12', enabled=True, universe=0, start=177, count=16,
                         routingMode='mapped', targetFixtures=[self.target['id']],
                         faderFunctions=['FX Activate','Sequence Trigger']+[None]*14)
        self.midi = dict(id='FX!', enabled=True, targetFixtures=[self.target['id']],
                         faderFunctions=self.desk['faderFunctions'], values=[0]*16,
                         changed=[0]*16)

    def route(self):
        self.monitor.route_tick(self.ip, [self.desk], self.fixtures, 1,
                                midi_controls=[self.midi])
        self.assertIsNone(self.monitor.routing.get('error'))
        return parse_packet(self.monitor.socket.sendto.call_args.args[0])['values']

    def test_midi_activation_survives_idle_desk_and_does_not_self_wake(self):
        self.midi['values'][0]=255
        self.midi['changed'][0]=time.monotonic()
        frame=self.route()
        self.assertEqual([frame[f['start']-1] for f in self.fx], [128]*6)
        revision=self.monitor.route_revision
        for _ in range(20):self.route()
        self.assertEqual(len(self.monitor.fx_activations),6)
        self.assertEqual(self.monitor.route_revision,revision)
        self.midi['values'][0]=0
        frame=self.route()
        self.assertFalse(self.monitor.fx_activations)
        self.assertEqual(frame[288:310], [0]*22)
        self.assertEqual(self.route()[288:310], [0]*22)

    def test_desk_activation_survives_idle_midi_and_manual_off_stays_off(self):
        self.midi['changed'][0]=time.monotonic()
        self.route()
        self.values[176]=255
        frame=self.route()
        self.assertEqual([frame[f['start']-1] for f in self.fx],[128]*6)
        self.monitor._set_sequence_fx_group(self.ip,self.target,self.fixtures,1,False)
        for _ in range(5):
            self.assertEqual(self.route()[288:310],[0]*22)
        self.values[176]=0
        self.route()
        self.values[176]=255
        self.route()
        self.assertEqual(len(self.monitor.fx_activations),6)

    def test_each_source_can_trigger_after_activation_without_restarting(self):
        for source in ('desk','midi'):
            with self.subTest(source=source):
                self.setUp()
                values=self.values if source=='desk' else self.midi['values']
                start=176 if source=='desk' else 0
                if source=='midi':self.midi['changed'][:2]=[time.monotonic()]*2
                values[start:start+2]=[255,100]
                # Keep the real start checks, but drive its worker offline.
                with patch('artnet_monitor.threading.Thread') as worker:
                    frame=self.route()
                    self.assertTrue(self.monitor.fx_sequence['active'])
                    self.assertEqual(frame[288:290],[128,0])
                    for _ in range(5):self.route()
                    self.assertEqual(worker.call_count,1)
                    self.monitor.fx_activations['fx0']['effect']=True
                    frame=self.route()
                    self.assertEqual(frame[288:290],[128,255])
                    self.assertEqual(frame[:2],[0,0])
                    values[start]=0
                    frame=self.route()
                    self.assertFalse(self.monitor.fx_sequence['active'])
                    self.assertEqual(frame[288:310],[0]*22)

    def test_sequence_worker_sends_each_absolute_effect_slot_and_releases(self):
        self.values[176]=255
        self.route()
        m=self.monitor
        with patch('artnet_monitor.threading.Thread'):
            m.start_fx_sequence(self.ip,self.fixtures,[f['id'] for f in self.fx],1,.1,.1)
        clock=[time.monotonic()]
        def wait(seconds):
            self.route()
            clock[0]+=max(seconds,.001)
        with patch('artnet_monitor.time.monotonic',side_effect=lambda:clock[0]), \
             patch.object(m.stop_event,'wait',side_effect=wait):
            m._fx_sequence_loop(self.fx,.1,.1)
        final=self.route()
        frames=[parse_packet(c.args[0])['values'] for c in m.socket.sendto.call_args_list]
        for fixture in self.fx:
            self.assertTrue(any(f[fixture['start']]==255 for f in frames))
            self.assertEqual(final[fixture['start']-1:fixture['start']+1],[128,0])
        self.assertFalse(m.fx_sequence['active'])

    def test_playback_mode_saved_per_target_for_desk_and_midi(self):
        for mode in ('once','loop'):
            desk=dict(self.desk,name='12',targetModes={self.target['id']:mode})
            self.assertEqual(validate_control(desk,[desk],self.fixtures)['targetModes'],desk['targetModes'])
            midi=dict(self.midi,id=None,name='FX!',slots=[None]*16,targetModes=desk['targetModes'])
            self.assertEqual(validate_section(midi,[],[],self.fixtures,'midi')['targetModes'],desk['targetModes'])
        with self.assertRaises(ValueError):
            validate_control(dict(self.desk,name='12',targetFixtures=['fx0'],targetModes={'fx0':'loop'}),[self.desk],self.fixtures)

    def test_target_playback_overrides_sample_for_both_sources(self):
        for source in ('desk','midi'):
            for mode in ('once','loop'):
                with self.subTest(source=source,mode=mode):
                    self.setUp()
                    self.target['sequenceDefinition']['triggerMode']='oneshot' if mode=='loop' else 'loop'
                    control=self.desk if source=='desk' else self.midi
                    control['targetModes']={self.target['id']:mode}
                    values=self.values if source=='desk' else self.midi['values']
                    start=176 if source=='desk' else 0
                    values[start:start+2]=[255,100]
                    # Held MIDI is still held after 10 seconds without motion.
                    self.midi['changed'][:2]=[time.monotonic()-20]*2 if source=='midi' else [0,0]
                    with patch('artnet_monitor.threading.Thread') as worker:
                        self.route()
                        self.assertEqual(worker.call_args.kwargs['args'][3],mode=='loop')
                        for _ in range(4):self.route()
                        self.assertEqual(worker.call_count,1)
                        values[start+1]=99
                        self.route()
                        self.assertFalse(any(self.monitor.sequence_trigger_high.values()))

    def test_loop_finishes_passes_without_parallel_restarts_and_stops_on_release(self):
        self.midi['targetModes']={self.target['id']:'loop'}
        self.midi['values'][:2]=[255,100]
        self.midi['changed'][:2]=[time.monotonic()]*2
        m=self.monitor
        with patch('artnet_monitor.threading.Thread') as worker:
            self.route()
            args=worker.call_args.kwargs['args']
            clock=[time.monotonic()]
            def wait(seconds):
                if m.fx_sequence.get('cycleCount',1)>=2 and m.fx_sequence['fixtureIndex']>=2:
                    self.midi['values'][1]=0
                self.route()
                clock[0]+=max(seconds,.001)
            with patch('artnet_monitor.time.monotonic',side_effect=lambda:clock[0]), \
                 patch.object(m.stop_event,'wait',side_effect=wait):
                m._fx_sequence_loop(*args)
            frame=self.route()
            self.assertEqual(worker.call_count,1)
            self.assertEqual(m.fx_sequence['cycleCount'],2)
            self.assertEqual(m.fx_sequence['status'],'Stopped · trigger released')
            for fixture in self.fx:self.assertEqual(frame[fixture['start']-1:fixture['start']+1],[128,0])
