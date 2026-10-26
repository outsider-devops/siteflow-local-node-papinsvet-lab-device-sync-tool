"""Removal and recovery must preserve assignments and never touch another input."""
from copy import deepcopy
import io
import json
import unittest
from unittest.mock import patch

from midi_controls import remove_control, restore_control


class MidiControlRemovalTests(unittest.TestCase):
    def setUp(self):
        self.control = dict(name='Tempo A', source='Traktor', type='Control Change', channel=1,
                            number=4, min=0, max=127, surfaceControlId='deck-a-tempo',
                            functionRole='Master', target='artnet')
        self.other = dict(self.control, source='Another MIDI source')
        self.device = dict(id='test-midi', kind='midi', midiControls=[deepcopy(self.control), self.other])

    def test_remove_and_restore_preserves_the_complete_assignment(self):
        removed = remove_control(self.device, self.control)
        self.assertEqual(self.device['midiControls'], [self.other])
        self.assertEqual(restore_control(self.device, removed['id']), self.control)
        self.assertEqual(self.device['midiRemovedControls'], [])

    def test_restore_does_not_overwrite_a_newly_learned_mapping(self):
        removed = remove_control(self.device, self.control)
        self.device['midiControls'].append(dict(self.control, name='New mapping'))
        with self.assertRaisesRegex(ValueError, 'already been learned'):
            restore_control(self.device, removed['id'])
        self.assertEqual(self.device['midiControls'][-1]['name'], 'New mapping')

    def test_remove_requires_all_identity_fields(self):
        with self.assertRaises(ValueError):
            remove_control(self.device, dict(self.control, channel=2))
        self.assertEqual(len(self.device['midiControls']), 2)

    def test_routes_remove_restore_and_require_request_token(self):
        import server

        def request(path, data, token=None):
            handler = object.__new__(server.Handler)
            raw = json.dumps(data).encode()
            handler.path = path
            handler.headers = {'Host':'127.0.0.1:8765', 'Origin':'http://127.0.0.1:8765',
                               'X-SiteFlow-Token':server.TOKEN if token is None else token,
                               'Content-Length':str(len(raw))}
            handler.rfile = io.BytesIO(raw)
            handler.reply = lambda data, status=200, **kwargs: (data, status)
            return handler.do_POST()

        with patch.object(server, 'STATE', {'devices':[self.device]}), patch.object(server, 'save') as save:
            payload = dict(deviceId='test-midi', control=self.control)
            _, status = request('/api/midi/controls/remove', payload, token='invalid')
            self.assertEqual(status, 403)
            self.assertEqual(len(self.device['midiControls']), 2)
            result, status = request('/api/midi/controls/remove', payload)
            self.assertEqual(status, 200)
            self.assertEqual(result['controls'], [self.other])
            restored, status = request('/api/midi/controls/restore',
                                       dict(deviceId='test-midi', removalId=result['changed']['id']))
            self.assertEqual(status, 200)
            self.assertIn(self.control, restored['controls'])
            self.assertEqual(save.call_count, 2)


if __name__ == '__main__':
    unittest.main()
