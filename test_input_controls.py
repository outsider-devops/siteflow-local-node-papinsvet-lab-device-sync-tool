import unittest
from fixtures import initial_patch
from input_controls import initial_controls,validate_control
class InputControlTests(unittest.TestCase):
    def test_reserved_ranges_and_destinations(self):
        fixtures=initial_patch('00:00:c8:d8:a2:71')+[dict(id='table',name='Table',universe=1,start=1,channels=[{}]*16)]
        controls=initial_controls('00:00:c8:d8:a2:71');c=dict(controls[0]);c['rules']=[dict(input=1,fixtureId='jellyfish-1',channel=2,min=255,max=0)]
        self.assertEqual(validate_control(c,controls,fixtures)['rules'][0]['min'],255)
        for changes in [dict(start=1),dict(start=190),dict(rules=[dict(input=1,fixtureId='bad',channel=1,min=0,max=255)])]:
            with self.assertRaises(ValueError):validate_control(dict(c,**changes),controls,fixtures)

    def test_one_input_can_target_multiple_fixtures(self):
        fixtures=initial_patch('00:00:c8:d8:a2:71');controls=initial_controls('00:00:c8:d8:a2:71');c=dict(controls[0])
        c['rules']=[dict(input=1,fixtureId=f['id'],channel=2,min=0,max=255) for f in fixtures[:3]]
        self.assertEqual(len(validate_control(c,controls,fixtures)['rules']),3)
        c['rules'].append(dict(c['rules'][0]))
        with self.assertRaises(ValueError):validate_control(c,controls,fixtures)

    def test_semantic_faders_remap_strobe_and_skip_missing_functions(self):
        from input_controls import semantic_rules
        from fixtures import PRESETS
        fixtures=[dict(id='j',name='Jellyfish',start=193,universe=1,channels=PRESETS[0]['channels']),dict(id='p',name='Parlight',start=1,universe=1,channels=PRESETS[1]['channels'])]
        rules=semantic_rules(['j','p'],fixtures)
        self.assertEqual(len(rules),14)
        self.assertEqual(next(r['channel'] for r in rules if r['fixtureId']=='j' and r['input']==8),6)
        self.assertEqual(next(r['channel'] for r in rules if r['fixtureId']=='p' and r['input']==8),8)
        self.assertFalse(any(r['fixtureId']=='j' and r['input'] in [6,7] for r in rules))

    def test_direct_and_mapped_modes_are_explicit(self):
        fixtures=initial_patch('00:00:c8:d8:a2:71');controls=initial_controls('00:00:c8:d8:a2:71')
        direct=dict(controls[0],routingMode='direct',targetFixtures=[],faderFunctions=['Master']+[None]*15,directFixtureId='jellyfish-1')
        saved=validate_control(direct,controls,fixtures)
        self.assertEqual(saved['routingMode'],'direct');self.assertEqual(saved['rules'],[]);self.assertEqual(saved['directFixtureId'],'jellyfish-1')
        mapped=dict(direct,routingMode='mapped',targetFixtures=['jellyfish-1'],directFixtureId=None)
        saved=validate_control(mapped,controls,fixtures)
        self.assertEqual(saved['routingMode'],'mapped');self.assertTrue(saved['rules'])
        with self.assertRaises(ValueError):validate_control(dict(direct,routingMode='other'),controls,fixtures)
