"""The imported controller map must match real NI identities and preserve user routing."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from traktor_template import load_template, merge_template

ROOT = Path(__file__).resolve().parent
SOURCE = 'Traktor Kontrol S2 MK2 Input'


class TraktorTemplateTests(unittest.TestCase):
    def setUp(self):
        self.records, self.profile = load_template(ROOT/'profiles/traktor-s2-mk2.ncs', SOURCE,
                                                   json.loads((ROOT/'profiles/traktor-surface-labels.json').read_text()))

    def test_all_input_layers_and_no_output_only_mappings(self):
        self.assertEqual(len(self.records), 111)
        self.assertEqual(self.profile['layers'], {'Common':43, 'Basic':34, 'Shift':34})
        self.assertFalse(any(c['templateControlId'].startswith('Ledchain') for c in self.records))
        identities = {(c['type'], c['channel'], c['number']) for c in self.records}
        self.assertEqual(len(identities), len(self.records))

    def test_actual_observed_left_deck_messages(self):
        controls = {c['number']:c for c in self.records if c['templateLayer']=='Basic' and c['channel']==1}
        self.assertEqual(controls[4]['surfaceControlId'], 'deck-a-tempo')
        self.assertEqual(controls[2]['surfaceControlId'], 'deck-a-jog-top')
        self.assertEqual(controls[19]['surfaceControlId'], 'deck-a-loop-move')
        self.assertEqual(controls[5]['surfaceControlId'], 'deck-a-flux')
        self.assertEqual(controls[2]['surfaceAliases'], ['deck-a-jog-rim'])
        self.assertIn('Relative', controls[2]['interpretation'])

    def test_shift_and_common_channels_are_converted_to_one_based_midi(self):
        tempo = [c for c in self.records if c['surfaceControlId']=='deck-a-tempo']
        self.assertEqual({(c['templateLayer'],c['channel'],c['number']) for c in tempo},
                         {('Basic',1,4),('Shift',1,34)})
        browser = next(c for c in self.records if c['surfaceControlId']=='mixer-browser')
        self.assertEqual((browser['channel'],browser['number']), (5,2))

    def test_reimport_is_idempotent_and_preserves_assigned_functions(self):
        tempo = deepcopy(next(c for c in self.records if c['surfaceControlId']=='deck-a-tempo' and c['templateLayer']=='Basic'))
        tempo.update(functionRole='Master',target='artnet',mappingName='Lab master')
        custom = dict(source='Other input',type='Note',channel=10,number=36,name='Custom pad')
        device = {'midiControls':[tempo,custom]}
        for _ in range(2):
            merge_template(device,self.records,self.profile)
        self.assertEqual(len(device['midiControls']),112)
        actual = next(c for c in device['midiControls'] if c['source']==SOURCE and c['channel']==1 and c['number']==4)
        self.assertEqual((actual['functionRole'],actual['target'],actual['mappingName']),('Master','artnet','Lab master'))
        self.assertIn(custom,device['midiControls'])


if __name__ == '__main__':
    unittest.main()
