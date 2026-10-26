import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock
from controller_modes import ControllerModes, parse_config, SOFTWARE, BYPASS, VERIFY_TTL
from artnet_monitor import ArtNetMonitor


def form(output=0, **changes):
    fields=dict(M1='1',U1=str(output),MG1='1',F1='35',M2='0',U2='1',MG2='1',F2='35',uc1='2.255.255.255',uc2='2.255.255.255',bc='192.168.18.255')
    fields.update(changes)
    html='<form name=dmxconf action=/cgi/dmx1.htm method=GET>'
    for k,v in fields.items():html+='<input name="%s" value="%s">'%(k,v)
    return html+'</form>'


class ModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.monitor=ArtNetMonitor();self.monitor.running=True;self.monitor.socket=Mock()
        self.output=0;self.requests=[];self.readback=True
        def request(path):
            self.requests.append(path)
            if path=='/':return 'ArtNet-1024 00:00:c8:d8:a2:71 ArtDmx2-V3.4'
            if '?' in path:
                from urllib.parse import parse_qs,urlsplit
                self.assertTrue(self.monitor.tx_muted)
                if self.readback:self.output=int(parse_qs(urlsplit(path).query)['U1'][0])
            return form(self.output)
        self.manager=ControllerModes(self.monitor,self.tmp.name,stopped=False,request=request)
        self.monitor.tx_permission=self.manager.tx_allowed

    def test_startup_requires_read_and_manual_changes_stop_tx(self):
        with self.assertRaises(ValueError):self.monitor.set_tx_muted(False)
        self.manager.refresh();self.assertFalse(self.monitor.tx_muted)
        self.output=1;self.manager.refresh();self.assertTrue(self.monitor.tx_muted)
        self.assertEqual(self.manager.snapshot()['mode'],BYPASS)
        with self.assertRaises(ValueError):self.manager.resume()
        self.monitor.socket.sendto.assert_not_called()

    def test_switch_both_directions_preserves_fields_and_backs_up(self):
        self.manager.refresh();self.manager.switch(BYPASS)
        self.assertTrue(self.monitor.tx_muted)
        self.manager.switch(SOFTWARE);self.assertFalse(self.monitor.tx_muted)
        backups=list(Path(self.tmp.name).glob('*.json'));self.assertEqual(len(backups),2)
        self.assertEqual(json.loads(backups[0].read_text())['fields']['F1'],'35')
        self.assertTrue(all('MG1=1' in p and 'MG2=1' in p and 'F1=35' in p and 'F2=35' in p for p in self.requests if '?' in p))
        self.monitor.socket.sendto.assert_not_called()

    def test_failed_readback_never_reports_success_or_resumes(self):
        self.manager.refresh();self.readback=False
        with self.assertRaisesRegex(ValueError,'readback'):self.manager.switch(BYPASS)
        self.assertTrue(self.monitor.tx_muted);self.assertEqual(self.manager.state['mode'],'UNKNOWN')

    def test_offline_and_expired_verification_gate_every_send(self):
        self.manager.refresh();self.manager.checked=time.monotonic()-VERIFY_TTL-1
        with self.assertRaises(ValueError):self.monitor._route_send('192.168.18.80',0,bytes(512))
        self.assertFalse(self.monitor.send_pixel_frames({('192.168.18.3',1):[0]*512},1))
        self.monitor.advertise('192.168.18.80');self.monitor.socket.sendto.assert_not_called()
        self.manager.request=Mock(side_effect=OSError('offline'));self.manager.refresh()
        self.assertEqual(self.manager.snapshot()['connection'],'OFFLINE');self.assertTrue(self.monitor.tx_muted)

    def test_stop_is_silent_and_remains_stopped_after_poll(self):
        self.manager.refresh();self.manager.stop();self.manager.refresh()
        self.assertTrue(self.monitor.tx_muted);self.monitor.socket.sendto.assert_not_called()
        self.manager.resume();self.assertFalse(self.monitor.tx_muted)

    def test_wrong_identity_never_applies(self):
        self.manager.request=Mock(return_value='other device')
        with self.assertRaises(ValueError):self.manager.switch(BYPASS)
        self.manager.request.assert_called_once_with('/');self.assertTrue(self.monitor.tx_muted)

    def test_actual_selects_unchecked_unicast_and_unknown_config(self):
        html=form().replace('<input name="M1" value="1">','<select name=M1><option value=0>Input</option><option value=1 selected>Output</option></select>')
        html+='<input name=C2 type=checkbox><input name=I2 value=192.168.18.255 disabled>'
        config=parse_config(html);self.assertEqual(config['mode'],SOFTWARE);self.assertNotIn('C2',config['fields']);self.assertNotIn('I2',config['fields'])
        self.assertEqual(parse_config(form(7))['mode'],'UNKNOWN')
        with self.assertRaises(ValueError):parse_config('<html>login</html>')
