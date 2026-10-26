import copy,unittest
from fixtures import initial_patch,PRESETS,prepare_fixture_update,fixture_test_plan
class SharedPresetTests(unittest.TestCase):
 def state(self):
  did='00:00:c8:d8:a2:71';patch=initial_patch(did)
  c=dict(id='input',name='Input',universe=1,start=81,count=16,enabled=False,targetFixtures=['jellyfish-1','jellyfish-2'],faderFunctions=['Red']+[None]*15,rules=[])
  return did,dict(fixtureMaps={did:patch},inputControls={did:[c]})
 def test_shared_channel_edit_updates_all_and_remapping(self):
  did,state=self.state();f=copy.deepcopy(state['fixtureMaps'][did][0]);f['channels'][1]['function']='Unknown';f['channels'][6]['function']='Red'
  maps,presets,controls,affected=prepare_fixture_update(state,did,f)
  self.assertEqual(affected,8)
  self.assertTrue(all(x['channels'][6]['function']=='Red' for x in maps[did]))
  self.assertTrue(all(x['start']==y['start'] and x['name']==y['name'] for x,y in zip(maps[did],state['fixtureMaps'][did])))
  self.assertEqual([r['channel'] for r in controls[did][0]['rules']],[7,7])
  self.assertEqual(next(p for p in presets if p['id']=='jellyfish-11ch')['channels'][6]['function'],'Red')
  self.assertEqual(len(fixture_test_plan(maps[did],1,presets)),8)
  self.assertEqual(state['fixtureMaps'][did][1]['channels'][6]['function'],'Unknown')
 def test_custom_edit_does_not_change_shared_profile(self):
  did,state=self.state();f=copy.deepcopy(state['fixtureMaps'][did][0]);f['mappingPreset']='custom';f['channels'][6]['function']='Pan'
  maps,presets,controls,affected=prepare_fixture_update(state,did,f)
  self.assertEqual(affected,1);self.assertEqual(presets,PRESETS)
  self.assertEqual(maps[did][1]['channels'][6]['function'],'Unknown')
 def test_invalid_expansion_leaves_original_state_unchanged(self):
  did,state=self.state();before=copy.deepcopy(state);f=copy.deepcopy(state['fixtureMaps'][did][0]);f['channels']+=[dict(function='Unknown',default=0)]
  with self.assertRaises(ValueError):prepare_fixture_update(state,did,f)
  self.assertEqual(state,before)
