import time,unittest
from unittest.mock import Mock,patch
from fixtures import initial_patch,fixture_test_plan,fixture_test_frame,PRESETS
from artnet_monitor import ArtNetMonitor,parse_packet

class FixtureSequenceTests(unittest.TestCase):
    def test_frames_preserve_desk_and_only_test_one_fixture(self):
        plan=fixture_test_plan(initial_patch('00:00:c8:d8:a2:71'),1)
        base=[17]*192
        for color in ['white','red','off']:
            frame=fixture_test_frame(plan,base,plan[0],color)
            self.assertEqual(frame[:192],bytes(base))
            self.assertEqual(frame[203:],bytes(77))
            expected=[255,0,0,0,255,0,0,0,0,0,0] if color=='white' else [255,255,0,0,0,0,0,0,0,0,0] if color=='red' else [0]*11
            self.assertEqual(list(frame[192:203]),expected)
        self.assertEqual(fixture_test_frame(plan,base)[192:],bytes(88))

    def test_rgbwauv_profile_keeps_amber_uv_strobe_off(self):
        f=dict(name='Table',start=1,universe=1,channels=PRESETS[1]['channels'])
        plan=fixture_test_plan([f],1)
        white=fixture_test_frame(plan,[7]*192,f,'white')
        red=fixture_test_frame(plan,[7]*192,f,'red')
        self.assertEqual(list(white[:8]),[255,0,0,0,255,0,0,0])
        self.assertEqual(list(red[:8]),[255,255,0,0,0,0,0,0])
        self.assertEqual(white[16:],bytes([7]*176))

    def test_unknown_profiles_skipped(self):
        items=initial_patch('00:00:c8:d8:a2:71')
        unknown=dict(name='Unknown',universe=1,start=1,channels=[{'function':'Unknown','default':0}]*16)
        self.assertEqual(len(fixture_test_plan(items+[unknown],1)),8)
        with self.assertRaises(ValueError):fixture_test_plan([unknown],1)

    def test_sequence_final_off_and_tx_feedback(self):
        m=ArtNetMonitor();m.socket=Mock();m.running=True
        plan=initial_patch('00:00:c8:d8:a2:71')[:2]
        m.output_test={'active':True,'sentPackets':0}
        m._fixture_baseline=Mock(return_value=[17]*192)
        # Advance a controlled clock when waiting; OS timer resolution varies.
        clock=[0.0]
        def wait(seconds):clock[0]+=seconds
        with patch('artnet_monitor.time.monotonic',side_effect=lambda:clock[0]), patch.object(m.stop_event,'wait',side_effect=wait):
            m._fixture_test_loop('192.168.18.80',1,plan,[17]*192,.01,.01)
        frames=[parse_packet(c.args[0])['values'] for c in m.socket.sendto.call_args_list]
        self.assertEqual(frames[-1][:192],[17]*192)
        self.assertEqual(frames[-1][192:],[0]*22)
        self.assertTrue(m.output_test['zeroSent']);self.assertFalse(m.output_test['active'])
        self.assertEqual(m.output_test['values'],frames[-1])
        self.assertEqual(len(frames),7)

    def test_missing_input_allows_standalone_test(self):
        m=ArtNetMonitor();m.running=True;m.socket=Mock()
        m.nodes['192.168.18.80']={'_last':time.monotonic(),'ports':[{'output':True,'outputUniverse':1}]}
        with patch('artnet_monitor.threading.Thread') as worker:
            result=m.start_fixture_test('192.168.18.80',initial_patch('00:00:c8:d8:a2:71'))
            self.assertTrue(result['active'])
            self.assertEqual(worker.call_args.kwargs['args'][3],[0]*192)
        m.socket.sendto.assert_not_called()
