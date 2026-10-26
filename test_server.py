"""Behavioral checks for local API boundaries, persistence and device-read safety."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import server


class NodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        server.STATE_FILE = Path(cls.tmp.name) / 'state.json'
        server.load()
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        server.PORT = cls.httpd.server_port
        cls.worker = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', server.PORT, timeout=3)
        conn.request(method, path, json.dumps(body) if body is not None else None, headers or {})
        r = conn.getresponse()
        status, raw = r.status, r.read()
        conn.close()
        return status, json.loads(raw)

    def auth(self):
        return {'Origin': 'http://127.0.0.1:' + str(server.PORT),
                'X-SiteFlow-Token': server.TOKEN, 'Content-Type': 'application/json'}

    def test_external_origin_and_missing_token_cannot_mutate(self):
        status, _ = self.request('POST', '/api/scan', {'mode': 'scan'})
        self.assertEqual(status, 403)
        h = self.auth(); h['Origin'] = 'https://untrusted.example'
        status, _ = self.request('POST', '/api/scan', {'mode': 'scan'}, h)
        self.assertEqual(status, 403)

    def test_fixture_map_persists_and_rejects_overlap(self):
        did='00:00:c8:d8:a2:71'
        status,result=self.request('GET','/api/fixtures?device='+did)
        self.assertEqual(status,200)
        self.assertEqual(len(result['fixtures']),8)
        f={'name':'New fixture','universe':1,'start':281,'channels':[{'function':'Dimmer','default':0}]}
        status,result=self.request('POST','/api/fixtures',{'deviceId':did,'fixture':f},self.auth())
        self.assertEqual(status,200)
        self.assertEqual(len(result['fixtures']),9)
        self.assertEqual(len(json.loads(server.STATE_FILE.read_text())['fixtureMaps'][did]),9)
        f['start']=280
        status,_=self.request('POST','/api/fixtures',{'deviceId':did,'fixture':f},self.auth())
        self.assertEqual(status,400)
        f['start']=513
        status,_=self.request('POST','/api/fixtures',{'deviceId':did,'fixture':f},self.auth())
        self.assertEqual(status,400)
        del server.STATE['fixtureMaps'][did]

    def test_rebinding_host_is_rejected(self):
        status, _ = self.request('GET', '/api/state', headers={'Host': 'untrusted.example'})
        self.assertEqual(status, 403)

    def test_virtual_input_rules_persist_without_transmission(self):
        did='00:00:c8:d8:a2:71'
        status,result=self.request('GET','/api/input-controls?device='+did)
        self.assertEqual(status,200)
        c=result['controls'][0]
        self.assertEqual((c['start'],c['count']),(81,16))
        c['rules']=[dict(input=1,fixtureId='jellyfish-1',channel=2,min=0,max=255)]
        with patch.object(server.MONITOR,'_fixture_send') as send:
            status,result=self.request('POST','/api/input-controls',{'deviceId':did,'control':c},self.auth())
            self.assertEqual(status,200)
            send.assert_not_called()
        self.assertTrue(result['transmissionEnabled'])
        self.assertEqual(json.loads(server.STATE_FILE.read_text())['inputControls'][did][0]['rules'],c['rules'])
        c['start']=1
        status,_=self.request('POST','/api/input-controls',{'deviceId':did,'control':c},self.auth())
        # Seed registry has no fixture at 1; a source outside the 192 desk channels must still fail.
        c['start']=193
        status,_=self.request('POST','/api/input-controls',{'deviceId':did,'control':c},self.auth())
        self.assertEqual(status,400)
        del server.STATE['inputControls'][did]

    def test_arbitrary_fader_function_persists_as_semantic_rule(self):
        did='00:00:c8:d8:a2:71'
        status,result=self.request('GET','/api/input-controls?device='+did)
        c=result['controls'][0]
        c.update(targetFixtures=['jellyfish-1'],faderFunctions=['Red']+[None]*15,enabled=False)
        status,result=self.request('POST','/api/input-controls',{'deviceId':did,'control':c},self.auth())
        self.assertEqual(status,200)
        saved=result['controls'][0]
        self.assertEqual(saved['rules'],[dict(input=1,fixtureId='jellyfish-1',channel=2,min=0,max=255)])
        self.assertEqual(json.loads(server.STATE_FILE.read_text())['inputControls'][did][0]['faderFunctions'][0],'Red')
        del server.STATE['inputControls'][did]

    def test_old_blanket_test_endpoint_removed(self):
        status,_=self.request('POST','/api/artnet/test',{'deviceId':'00:00:c8:d8:a2:71','expectedUniverse':1},self.auth())
        self.assertEqual(status,404)

    def test_no_device_output_endpoint(self):
        status, _ = self.request('POST', '/api/control', {'action': 'setPilot'}, self.auth())
        self.assertEqual(status, 404)
        status, _ = self.request('POST', '/api/device',
                                 {'id': server.STATE['devices'][0]['id'], 'changes': {'controlEnabled': True}}, self.auth())
        self.assertEqual(status, 400)

    def test_labels_persist_but_device_address_is_read_only(self):
        d = server.STATE['devices'][0]
        old_ip = d['ip']
        status, _ = self.request('POST', '/api/device',
                                 {'id': d['id'], 'changes': {'name': 'Test label', 'zone': 'Test zone'}}, self.auth())
        self.assertEqual(status, 200)
        disk = json.loads(server.STATE_FILE.read_text())
        self.assertEqual(disk['devices'][0]['name'], 'Test label')
        status, _ = self.request('POST', '/api/device',
                                 {'id': d['id'], 'changes': {'ip': '8.8.8.8'}}, self.auth())
        self.assertEqual(status, 400)
        self.assertEqual(d['ip'], old_ip)

    def test_old_observation_becomes_stale(self):
        d = server.STATE['devices'][0]
        old = copy.deepcopy(d)
        try:
            d.update(reachability='Online', lastSeen='2020-01-01T00:00:00+00:00')
            status, result = self.request('GET', '/api/state')
            self.assertEqual(status, 200)
            self.assertEqual(result['devices'][0]['reachability'], 'Stale')
        finally:
            d.clear(); d.update(old)

    def test_outside_current_subnet_never_connects(self):
        d = copy.deepcopy(server.STATE['devices'][0]); d['ip'] = '8.8.8.8'
        with patch('server.connect') as tcp, patch('server.wiz_read') as udp:
            result = server.probe(d, '192.168.18.0/24', '192.168.18.6')
        tcp.assert_not_called(); udp.assert_not_called()
        self.assertEqual(result['access'], 'Outside network')

    def test_wiz_api_read_and_identity_mismatch(self):
        d = next(copy.deepcopy(d) for d in server.STATE['devices'] if d['kind'] == 'wiz')
        with patch('server.connect', return_value=False), patch('server.wiz_read', return_value={
            'getSystemConfig': {'mac': 'ffffffffffff'}, 'getPilot': {'state': True}}):
            result = server.probe(d, '192.168.18.0/24', '192.168.18.6')
        self.assertEqual(result['access'], 'Identity conflict')
        self.assertIsNone(result['webUrl'])

    def test_scan_single_flight_and_no_network_on_duplicate(self):
        was = server.STATE['scan']['running']
        try:
            server.STATE['scan']['running'] = True
            with patch('server.scan') as worker:
                self.assertFalse(server.start_scan('scan'))
                worker.assert_not_called()
        finally:
            server.STATE['scan']['running'] = was


if __name__ == '__main__':
    unittest.main(verbosity=2)
